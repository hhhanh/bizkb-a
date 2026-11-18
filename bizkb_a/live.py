"""Live demo service: the web UI runs on the real engine (rdflib + pySHACL), not on a browser replica.

Run:  uvicorn bizkb_a.live:app --port 8080     then open http://localhost:8080
Real LLM (optional):  export ANTHROPIC_API_KEY=...  BKA_USE_LLM=1  [BKA_LLM_MODEL=...]
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel
from rdflib import URIRef

from .executor import FIELD_CLASS, Executor, minimise
from .kb import BKA, COM, GOV, KnowledgeBase
from .matchmaker import Matchmaker
from .planner import HTNPlanner, PlanningError, ScriptedProposer
from .provenance import ProvenanceRecorder
from .router import LabelGrounder
from .verifier import Mandate, PlanContext, Step, Verifier

UI = Path(__file__).resolve().parent.parent / "ui" / "index.html"
PRINCIPAL = URIRef("https://ndta.gov.vn/person/demo-user")
PROFILE = {"full_name": "Nguyễn Văn B", "address": "Hà Nội", "phone": "09xx xxx xxx",
           "national_id": "0xx…", "card_token": "tok_***", "tax_code": "01xx…", "order_ref": "DH-001"}

kb = KnowledgeBase()
verifier = Verifier(kb)
matchmaker = Matchmaker(kb)

USE_LLM = bool(os.environ.get("ANTHROPIC_API_KEY")) and os.environ.get("BKA_USE_LLM") == "1"
if USE_LLM:                                             # real LLM for grounding and fallback
    from .planner import LLMProposer
    from .router import LLMGrounder
    grounder, proposer = LLMGrounder(kb), LLMProposer(kb)
else:                                                   # replayed proposals: reproducible offline demo
    grounder = LabelGrounder(kb)
    proposer = ScriptedProposer({COM.ReorderLastPurchase: [
        [COM.RetrieveOrderHistory, COM.QuickPay, COM.ArrangeDelivery],
        [COM.RetrieveOrderHistory, COM.ExecutePayment, COM.ArrangeDelivery],
        [COM.RetrieveOrderHistory, COM.VerifyMerchant, COM.ObtainPaymentMandate, COM.ExecutePayment,
         COM.ArrangeDelivery, COM.IssueEInvoice]]})
planner = HTNPlanner(kb, verifier, proposer)
SESSIONS: dict[str, dict] = {}

app = FastAPI(title="BizKB-A live demo", version="0.2.0")


def reset_scripted_fallback() -> None:
    """Drop the method the scripted LLM produced earlier and reload the script."""
    for m, _subs, status in kb.methods_for(COM.ReorderLastPurchase):
        if status != "curated":
            kb.g.remove((m, None, None))
    planner.proposer = ScriptedProposer({COM.ReorderLastPurchase: [
        [COM.RetrieveOrderHistory, COM.QuickPay, COM.ArrangeDelivery],
        [COM.RetrieveOrderHistory, COM.ExecutePayment, COM.ArrangeDelivery],
        [COM.RetrieveOrderHistory, COM.VerifyMerchant, COM.ObtainPaymentMandate, COM.ExecutePayment,
         COM.ArrangeDelivery, COM.IssueEInvoice]]})


# ---------- helpers ----------
def frag(u) -> str:
    return str(u).rsplit("#", 1)[-1]


def resolve(name: str | None):
    """Map a short id sent by the UI (e.g. merchant_A, Groceries) to its ontology URI."""
    if not name:
        return None
    for ns in (COM, GOV, BKA):
        u = ns[name]
        if (u, None, None) in kb.g or (None, None, u) in kb.g:
            return u
    return URIRef(name)


def violation_codes(violations) -> list[dict]:
    return [{"c": v.message.split(":", 1)[0].strip(), "step": v.order} for v in violations]


# ---------- request models ----------
class MandateIn(BaseModel):
    cap: float = 0
    days: int = 30
    cats: list[str] = []


class RunIn(BaseModel):
    text: str
    amount: float | None = None
    merchant: str | None = None
    category: str | None = None
    mandate: MandateIn | None = None
    compare: bool = True
    product: str | None = None


class DecisionIn(BaseModel):
    approve: bool


# ---------- core ----------
def run_steps(sid: str) -> dict:
    """Execute until completion or until a step needs human approval."""
    s = SESSIONS[sid]
    rep, steps, executor = s["rep"], s["steps"], s["executor"]
    while s["i"] < len(steps):
        st = steps[s["i"]]
        if st.hitl and not s["approved"].get(st.order):
            rep["status"] = "waiting"
            return rep
        info = kb.agent_info(st.agent)
        needed = {str(x) for x in kb.g.objects(st.task, BKA.needsField)}
        payload = minimise(s["payload"], info["data_classes"], needed)
        out = executor.adapters[info["protocol"]].call(info, kb.label(st.task), payload)
        approver = PRINCIPAL if s["approved"].get(st.order) else None
        s["prov"].record_step(s["pid"], st, PRINCIPAL, "completed", out, approver, sorted(payload))
        rep["exec"].append({"task": frag(st.task), "agent": frag(st.agent), "status": "done",
                            "sent": sorted(payload),
                            "held": sorted(k for k in needed if k in s["payload"] and k not in payload),
                            "approved": bool(approver)})
        s["i"] += 1
    rep["status"] = "completed"
    rep["prov"] = s["prov"].turtle()
    return rep


@app.post("/api/orchestrate")
def orchestrate(req: RunIn) -> dict:
    pid = uuid.uuid4().hex[:8]
    rep: dict = {"pid": pid, "status": "running", "engine": "live", "exec": [],
                 "trace": {}, "bind": [], "steps": [], "viol": [], "prov": ""}

    if not USE_LLM:                       # demo hygiene: replay the LLM fallback on every run
        reset_scripted_fallback()
    intent = grounder.ground(req.text)
    if intent.goal is None:
        rep.update(status="rejected", rejectAt=1)
        return rep
    rep["g"] = {"goal": frag(intent.goal), "lab": intent.matched_label or req.text}

    ctx = None
    if kb.domain_of(intent.goal) == COM.CommerceDomain:
        mandate = None
        if req.mandate:
            mandate = Mandate(req.mandate.cap, [resolve(c) for c in req.mandate.cats],
                              datetime.now(timezone.utc) + timedelta(days=req.mandate.days))
        ctx = PlanContext(req.amount, resolve(req.merchant), resolve(req.category), mandate)
    rep["ctxUsed"] = ctx is not None

    log: list[str] = []
    try:
        steps: list[Step] = planner.plan(intent.goal, pid, log, ctx)
    except PlanningError as e:
        rep.update(status="rejected", rejectAt=2)
        rep["trace"]["llm"] = planner.attempts
        rep["viol"] = [{"c": v.message.split(":", 1)[0].strip(), "step": v.order} for v in e.violations]
        return rep

    if planner.attempts:
        rep["trace"]["llm"] = planner.attempts
    methods = kb.methods_for(intent.goal)
    if methods:
        rep["trace"]["method"] = {"goal": frag(intent.goal), "status":
                                  "curated" if methods[0][2] == "curated" else "proposed",
                                  "subs": [frag(x) for x in methods[0][1]]}

    for st in steps:
        cands = matchmaker.candidates(st.task)
        best = next((c for c in cands if c.gate_ok), None)
        entry = {"task": frag(st.task),
                 "rejected": [{"agent": frag(c.agent), "s": c.score, "reasons": c.codes}
                              for c in cands if not c.gate_ok]}
        if best is None:
            rep["bind"].append(entry | {"best": None})
            rep.update(status="rejected", rejectAt=3)
            return rep
        st.agent, st.degree = best.agent, best.degree
        entry["best"] = {"agent": frag(best.agent), "d": best.degree, "s": best.score}
        rep["bind"].append(entry)
    rep["steps"] = [{"task": frag(s.task), "hitl": s.hitl, "agent": frag(s.agent),
                     "deg": s.degree} for s in steps]

    ok, violations = verifier.validate(pid, steps, ctx, intent.goal)
    rep["viol"] = violation_codes(violations)
    if req.compare:
        from .matchmaker import match_degree
        bsteps = []
        for s in steps:
            best_a, best_v = None, None
            for a in kb.agents():
                info = kb.agent_info(a)
                if any(match_degree(kb, s.task, p) != "fail" for p in info["provides"]):
                    v = info["trust"] - info["cost"]
                    if best_v is None or v > best_v:
                        best_a, best_v = a, v
            bsteps.append(Step(s.order, s.task, best_a, "n/a", s.hitl))
        _, bviol = verifier.validate(pid + "-base", bsteps, ctx, intent.goal)
        rep["cmp"] = {"base": violation_codes(bviol)}
    if not ok:
        rep.update(status="rejected", rejectAt=4)
        return rep

    prov = ProvenanceRecorder()
    prov.start_plan(pid, intent.goal, PRINCIPAL)
    if ctx and ctx.mandate:
        prov.record_mandate(pid, ctx.mandate, PRINCIPAL)
    payload = dict(PROFILE)
    payload["product"] = req.product or req.text
    payload["quantity"] = 1
    payload["citizen_request"] = req.text
    if ctx:
        if ctx.amount is not None:
            payload["amount"] = ctx.amount
        if ctx.merchant is not None:
            payload["merchant"] = kb.label(ctx.merchant)
    SESSIONS[pid] = {"rep": rep, "steps": steps, "i": 0, "approved": {}, "payload": payload,
                     "prov": prov, "pid": pid, "executor": Executor(kb, prov)}
    return run_steps(pid)


@app.post("/api/plans/{pid}/decision")
def decision(pid: str, body: DecisionIn) -> dict:
    s = SESSIONS.get(pid)
    if not s:
        raise HTTPException(404, "plan not found")
    st = s["steps"][s["i"]]
    if body.approve:
        s["approved"][st.order] = True
        return run_steps(pid)
    s["prov"].record_step(pid, st, PRINCIPAL, "denied-by-human")
    s["rep"]["exec"].append({"task": frag(st.task), "agent": frag(st.agent), "status": "denied",
                             "sent": [], "held": [], "approved": False})
    s["rep"]["status"] = "halted"
    s["rep"]["prov"] = s["prov"].turtle()
    return s["rep"]


@app.get("/api/plans/{pid}/provenance", response_class=PlainTextResponse)
def provenance(pid: str) -> str:
    s = SESSIONS.get(pid)
    if not s:
        raise HTTPException(404, "plan not found")
    return s["prov"].turtle()


@app.get("/api/kb")
def knowledge_base() -> dict:
    import pyshacl
    import rdflib
    return {"engine": "live", "llm": "anthropic" if USE_LLM else "scripted",
            "rdflib": rdflib.__version__, "pyshacl": pyshacl.__version__,
            "triples": len(kb.g), "shapes": len(kb.shapes),
            "agents": [{"id": frag(a), "attested": kb.agent_info(a)["attested"],
                        "provides": [frag(p) for p in kb.agent_info(a)["provides"]]} for a in kb.agents()],
            "fields": {k: frag(v) for k, v in FIELD_CLASS.items()}}


@app.get("/")
def index():
    if not UI.exists():
        raise HTTPException(404, "ui/index.html not found")
    return FileResponse(UI)
