"""Run the large-scale benchmark (paper §7.5).

  python -m bench.run            # writes bench/results.json and bench/results.md

Systems
  bizkba      full BizKB-A (closed-set grounding, HTN + verified LLM fallback, hard gates)
  b1_lex      description-similarity router (TF-IDF char n-grams), no gates   [stands in for an
              embedding router; the LLM-supervisor baseline needs API access and is not run here]
  b0_blind    policy-blind router: highest trust - cost, no gates
  b3_nofb     ablation: BizKB-A without the LLM fallback
  b4_weights  ablation: compliance as an additive weight instead of a hard gate

Every system's bound plan is validated by the same SHACL norms.
"""
from __future__ import annotations

import json
import statistics
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rdflib import Literal, URIRef
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from bizkb_a.kb import BKA, COM, GOV, KnowledgeBase
from bizkb_a.matchmaker import DEGREE_VALUE, Matchmaker, match_degree
from bizkb_a.planner import HTNPlanner, PlanningError, ScriptedProposer
from bizkb_a.router import LabelGrounder
from bizkb_a.verifier import Mandate, PlanContext, Step, Verifier

HERE = Path(__file__).resolve().parent
W5 = 0.25                      # compliance weight for the b4_weights ablation


# ---------------------------------------------------------------- setup
def load_kb() -> KnowledgeBase:
    kb = KnowledgeBase()
    kb.g.parse(HERE / "bench_agents.ttl", format="turtle")
    return kb


kb = load_kb()
verifier = Verifier(kb)
matchmaker = Matchmaker(kb)
grounder = LabelGrounder(kb)
SCRIPT = {COM.ReorderLastPurchase: [
    [COM.RetrieveOrderHistory, COM.QuickPay, COM.ArrangeDelivery],
    [COM.RetrieveOrderHistory, COM.ExecutePayment, COM.ArrangeDelivery],
    [COM.RetrieveOrderHistory, COM.VerifyMerchant, COM.ObtainPaymentMandate, COM.ExecutePayment,
     COM.ArrangeDelivery, COM.IssueEInvoice]]}
AGENTS = [(a, kb.agent_info(a)) for a in kb.agents()]
LLM_CALLS = {"bizkba": 0, "b1_lex": 0, "b0_blind": 0, "b3_nofb": 0, "b4_weights": 0}


def reset_fallback(planner: HTNPlanner) -> None:
    for m, _s, status in kb.methods_for(COM.ReorderLastPurchase):
        if status != "curated":
            kb.g.remove((m, None, None))
    if planner.proposer is not None:
        planner.proposer = ScriptedProposer({k: [list(v) for v in vs] for k, vs in SCRIPT.items()})


def uri(name: str):
    for ns in (COM, GOV, BKA):
        u = ns[name]
        if (u, None, None) in kb.g or (None, None, u) in kb.g:
            return u
    return URIRef(name)


def make_ctx(c: dict | None) -> PlanContext | None:
    if not c:
        return None
    m = None
    if c.get("mandate"):
        md = c["mandate"]
        m = Mandate(md["cap"], [uri(x) for x in md["cats"]],
                    datetime.now(timezone.utc) + timedelta(days=md["days"]))
    return PlanContext(c["amount"], uri(c["merchant"]), uri(c["category"]), m)


# ---------------------------------------------------------------- gold admissibility
def gold_admissible(task: URIRef) -> set:
    """Derived declaratively from descriptors: capability match, authority, data class, attestation."""
    need_auth = kb.g.value(task, BKA.requiresAuthority)
    need_dc = kb.g.value(task, BKA.dataClass)
    out = set()
    for a, info in AGENTS:
        deg = max((match_degree(kb, task, p) for p in info["provides"]),
                  key=lambda d: DEGREE_VALUE[d], default="fail")
        if deg not in ("exact", "plugin"):
            continue
        if need_auth is not None and need_auth not in info["authorities"]:
            continue
        if need_dc is not None and need_dc not in info["data_classes"]:
            continue
        if not info["attested"]:
            continue
        out.add(a)
    return out


# ---------------------------------------------------------------- grounding
GOALS = kb.goals()
GOAL_TEXT = [" ".join(kb.labels(g)) for g in GOALS]
VEC_G = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5)).fit(GOAL_TEXT + ["placeholder"])
GOAL_M = VEC_G.transform(GOAL_TEXT)


def ground_similarity(text: str):
    """Nearest-label grounding without a closed-set check: always returns a goal."""
    sims = cosine_similarity(VEC_G.transform([text]), GOAL_M)[0]
    return GOALS[int(sims.argmax())]


AGENT_TEXT = [" ".join([info["label"]] + [kb.label(p) for p in info["provides"]]
                       + [kb.label(p, "en") for p in info["provides"]]) for _a, info in AGENTS]
VEC_A = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5)).fit(AGENT_TEXT)
AGENT_M = VEC_A.transform(AGENT_TEXT)


def bind_lex(task: URIRef):
    text = kb.label(task) + " " + kb.label(task, "en")
    sims = cosine_similarity(VEC_A.transform([text]), AGENT_M)[0]
    return AGENTS[int(sims.argmax())][0]


def bind_blind(task: URIRef):
    best, bv = None, None
    for a, info in AGENTS:
        if max((match_degree(kb, task, p) for p in info["provides"]),
               key=lambda d: DEGREE_VALUE[d], default="fail") == "fail":
            continue
        v = info["trust"] - info["cost"]
        if bv is None or v > bv:
            best, bv = a, v
    return best


def bind_weighted(task: URIRef):
    """Compliance as an additive term (Proposition 1, w5 below the compensation bound)."""
    best, bs = None, None
    for c in matchmaker.candidates(task):
        s = c.score + (W5 if c.gate_ok else 0.0)
        if bs is None or s > bs:
            best, bs = c.agent, s
    return best


# ---------------------------------------------------------------- one system, one request
VAL_CACHE: dict[tuple, tuple] = {}


def validate_cached(goal, steps, ctx, ctx_key):
    key = (str(goal), tuple((str(s.task), str(s.agent), s.hitl) for s in steps), ctx_key)
    if key not in VAL_CACHE:
        VAL_CACHE[key] = verifier.validate("bench", steps, ctx, goal, scoped=True)
    ok, v = VAL_CACHE[key]
    return ok, sorted({x.message.split(":", 1)[0].strip() for x in v})


def ctx_signature(c: dict | None) -> tuple:
    if not c:
        return ()
    m = c.get("mandate")
    return (c["amount"], c["merchant"], c["category"],
            (m["cap"], m["days"] >= 0, tuple(m["cats"])) if m else None)


def run_one(system: str, req: dict, planner: HTNPlanner) -> dict:
    ctx = make_ctx(req["ctx"])
    ctx_key = ctx_signature(req["ctx"])
    if system in ("bizkba", "b3_nofb", "b4_weights"):
        intent = grounder.ground(req["text"])
        goal = intent.goal
        if goal is None:
            return {"decision": "refuse", "goal": None, "codes": [], "steps": []}
    else:
        goal = ground_similarity(req["text"])
    if kb.domain_of(goal) != COM.CommerceDomain:
        ctx, ctx_key = None, ()
    reset_fallback(planner)
    try:
        steps = planner.plan(goal, "bench", [], ctx)
    except PlanningError as e:
        codes = sorted({v.message.split(":", 1)[0].strip() for v in e.violations}) or ["unplannable"]
        return {"decision": "reject", "goal": goal, "codes": codes, "steps": []}
    LLM_CALLS[system] += len(planner.attempts)
    binder = {"bizkba": None, "b3_nofb": None, "b4_weights": bind_weighted,
              "b1_lex": bind_lex, "b0_blind": bind_blind}[system]
    for s in steps:
        if binder is None:
            best, _ = matchmaker.bind(s.task)
            if best is None:
                return {"decision": "reject", "goal": goal, "codes": ["unbindable"], "steps": []}
            s.agent, s.degree = best.agent, best.degree
        else:
            s.agent, s.degree = binder(s.task), "n/a"
            if s.agent is None:
                return {"decision": "reject", "goal": goal, "codes": ["unbindable"], "steps": []}
    ok, codes = validate_cached(goal, steps, ctx, ctx_key)
    return {"decision": "accept" if ok else "reject", "goal": goal, "codes": codes,
            "steps": [(s.task, s.agent, s.hitl) for s in steps]}


# ---------------------------------------------------------------- evaluation
def evaluate() -> dict:
    reqs = json.loads((HERE / "requests.json").read_text(encoding="utf-8"))
    planners = {"bizkba": HTNPlanner(kb, verifier, ScriptedProposer({})),
                "b3_nofb": HTNPlanner(kb, verifier, None),
                "b4_weights": HTNPlanner(kb, verifier, ScriptedProposer({})),
                "b1_lex": HTNPlanner(kb, verifier, ScriptedProposer({})),
                "b0_blind": HTNPlanner(kb, verifier, ScriptedProposer({}))}
    systems = list(planners)
    res = {s: {"goal_ok": 0, "goal_n": 0, "refuse_ok": 0, "refuse_n": 0, "bind_ok": 0, "bind_n": 0,
               "viol_plans": 0, "viol_total": 0, "plans": 0, "decision_ok": 0, "code_ok": 0,
               "code_n": 0, "hitl_ok": 0, "hitl_n": 0, "bind_viol": 0, "bind_viol_plans": 0,
               "errors": []} for s in systems}
    for req in reqs:
        for s in systems:
            r = run_one(s, req, planners[s])
            m = res[s]
            if req["goal"] is None:
                m["refuse_n"] += 1
                m["refuse_ok"] += int(r["decision"] == "refuse")
            else:
                m["goal_n"] += 1
                hit = r["goal"] is not None and str(r["goal"]).endswith("#" + req["goal"])
                m["goal_ok"] += int(hit)
                if not hit and len(m["errors"]) < 12:
                    m["errors"].append({"text": req["text"], "gold": req["goal"],
                                        "got": str(r["goal"]).rsplit("#", 1)[-1] if r["goal"] else None})
                m["decision_ok"] += int(r["decision"] == req["gold_decision"])
                if req["gold_codes"]:
                    m["code_n"] += 1
                    m["code_ok"] += int(set(req["gold_codes"]).issubset(set(r["codes"])))
            if r["steps"]:
                m["plans"] += 1
                bad = [c for c in r["codes"] if c.startswith(("N", "C"))]
                binding_bad = [c for c in bad if c in ("N2", "N3", "N6")]
                m["viol_total"] += len(bad)
                m["viol_plans"] += int(bool(bad))
                m["bind_viol"] += len(binding_bad)
                m["bind_viol_plans"] += int(bool(binding_bad))
                for task, agent, hitl in r["steps"]:
                    m["bind_n"] += 1
                    m["bind_ok"] += int(agent in gold_admissible(task))
                    if task == COM.ExecutePayment and req["case"] == "cross_border":
                        m["hitl_n"] += 1
                        m["hitl_ok"] += int(hitl)
    return {"n_requests": len(reqs), "systems": res, "llm_calls": LLM_CALLS}


def w5_sweep() -> dict:
    """Proposition 1 empirically: how small must the compliance weight be before traps win?"""
    global W5
    tasks = [COM.ExecutePayment, COM.IssueEInvoice, GOV.RegisterBirth, COM.ArrangeDelivery]
    out = {}
    for w in (0.0, 0.01, 0.02, 0.05, 0.10, 0.25):
        W5 = w
        ok = sum(1 for t in tasks if bind_weighted(t) in gold_admissible(t))
        out[f"{w:.2f}"] = {"admissible_bindings": ok, "of": len(tasks)}
    W5 = 0.25
    return out


def latency_study() -> dict:
    """Validation cost: full knowledge base vs scoped subgraph, at two KB sizes."""
    steps = [Step(1, COM.SearchOffers, COM.agent_shopping, "exact"),
             Step(2, COM.VerifyMerchant, COM.agent_registry, "exact"),
             Step(3, COM.ObtainPaymentMandate, COM.agent_wallet, "exact", hitl=True),
             Step(4, COM.ExecutePayment, COM.agent_wallet, "exact", hitl=True),
             Step(5, COM.ArrangeDelivery, COM.agent_logistics, "exact"),
             Step(6, COM.IssueEInvoice, COM.agent_einvoice, "exact")]
    ctx = PlanContext(350_000, COM.merchant_A, COM.Groceries, None)
    out = {}
    for label in ("base", "inflated"):
        if label == "inflated":                       # add ~50k unrelated triples
            for i in range(50_000):
                kb.g.add((URIRef(f"urn:filler:{i}"), BKA.trust, Literal(0.5)))
        out[label] = {"triples": len(kb.g)}
        for scoped in (False, True):
            ts = []
            for i in range(15):
                t0 = time.perf_counter()
                verifier.validate(f"lat{i}", steps, ctx, COM.PurchaseGoods, scoped=scoped)
                ts.append((time.perf_counter() - t0) * 1000)
            ts.sort()
            out[label]["scoped" if scoped else "full"] = {
                "p50": round(statistics.median(ts), 1), "p95": round(ts[int(.95 * len(ts)) - 1], 1)}
    return out


def pipeline_latency() -> dict:
    """End-to-end planning latency (grounding, HTN, matchmaking, scoped validation), no cache."""
    reqs = [r for r in json.loads((HERE / "requests.json").read_text(encoding="utf-8")) if r["goal"]][:40]
    planner = HTNPlanner(kb, verifier, ScriptedProposer({}))
    ts = []
    for req in reqs:
        ctx = make_ctx(req["ctx"])
        t0 = time.perf_counter()
        intent = grounder.ground(req["text"])
        if intent.goal is None:
            continue
        c = ctx if kb.domain_of(intent.goal) == COM.CommerceDomain else None
        try:
            steps = planner.plan(intent.goal, "lat", [], c)
        except PlanningError:
            continue
        for s in steps:
            b, _ = matchmaker.bind(s.task)
            if b is None:
                break
            s.agent, s.degree = b.agent, b.degree
        verifier.validate("lat", steps, c, intent.goal, scoped=True)
        ts.append((time.perf_counter() - t0) * 1000)
    ts.sort()
    return {"n": len(ts), "p50": round(statistics.median(ts), 1), "p95": round(ts[int(.95 * len(ts)) - 1], 1)}


NAMES = {"bizkba": "BizKB-A", "b1_lex": "B2′ similarity router", "b0_blind": "B0 policy-blind",
         "b3_nofb": "B3 no LLM fallback", "b4_weights": "B4 weighted policy"}


def report(ev: dict, lat: dict, pipe: dict) -> str:
    pct = lambda a, b: f"{100 * a / b:.1f}%" if b else "—"
    rows = []
    for s, m in ev["systems"].items():
        rows.append(f"| {NAMES[s]} | {pct(m['goal_ok'], m['goal_n'])} | {pct(m['refuse_ok'], m['refuse_n'])} | "
                    f"{pct(m['bind_ok'], m['bind_n'])} | {m['viol_total'] / max(m['plans'], 1):.2f} | "
                    f"{pct(m['viol_plans'], m['plans'])} | {pct(m['decision_ok'], m['goal_n'])} | "
                    f"{pct(m['code_ok'], m['code_n'])} | {m['bind_viol'] / max(m['plans'], 1):.2f} | "
                    f"{ev['llm_calls'][s]} |")
    return ("\n".join([
        f"# Benchmark results ({ev['n_requests']} requests, {len(AGENTS)} agents)", "",
        "| System | Goal accuracy | Correct refusal | Binding precision | Violations/plan | "
        "Plans with a violation | Decision agreement | Correct norm cited | Binding-induced viol./plan | Scripted fallback proposals |",
        "|---|---|---|---|---|---|---|---|---|---|", *rows, "",
        "w5 sweep (b4 ablation): " + json.dumps(ev.get("w5", {})), "",
        "Grounding errors (BizKB-A): " + json.dumps(ev["systems"]["bizkba"]["errors"], ensure_ascii=False), "",
        f"Pipeline latency (n={pipe['n']}): p50 {pipe['p50']} ms, p95 {pipe['p95']} ms.", "",
        "| Validation | KB triples | full p50/p95 (ms) | scoped p50/p95 (ms) |", "|---|---|---|---|",
        *[f"| {k} | {v['triples']} | {v['full']['p50']}/{v['full']['p95']} | "
          f"{v['scoped']['p50']}/{v['scoped']['p95']} |" for k, v in lat.items()]]) + "\n")


if __name__ == "__main__":
    t0 = time.time()
    ev = evaluate()
    ev["w5"] = w5_sweep()
    pipe = pipeline_latency()
    lat = latency_study()
    (HERE / "results.json").write_text(json.dumps({"eval": {k: v for k, v in ev.items()},
                                                   "latency": lat, "pipeline": pipe},
                                                  indent=1, default=str), encoding="utf-8")
    md = report(ev, lat, pipe)
    (HERE / "results.md").write_text(md, encoding="utf-8")
    print(md)
    print(f"elapsed {time.time() - t0:.0f}s")
