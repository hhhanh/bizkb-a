"""REST API (FastAPI) để tích hợp BizKB-A vào hạ tầng hiện có (cổng DVC, NDXP, nền tảng TMĐT).

Chạy:  uvicorn bizkb_a.api:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import base64
import json
import os
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from rdflib import URIRef

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .annotator import annotate_agent_card
from .attestation import AttestationError, IssuerRegistry, register_attested
from .executor import auto_approve
from .verifier import Mandate, PlanContext
from .orchestrator import Orchestrator

if os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("BKA_USE_LLM") == "1":
    from .kb import KnowledgeBase
    from .planner import LLMProposer
    from .router import LLMGrounder
    _kb = KnowledgeBase()
    orch = Orchestrator(_kb, LLMGrounder(_kb), LLMProposer(_kb))
else:
    orch = Orchestrator()

registry = IssuerRegistry()
# BKA_ISSUER_KEYS='{"https://.../issuer": "<base64 Ed25519 public key (raw 32 bytes)>"}'
for iss, k in json.loads(os.environ.get("BKA_ISSUER_KEYS", "{}")).items():
    registry.add(URIRef(iss), Ed25519PublicKey.from_public_bytes(base64.b64decode(k)))

app = FastAPI(title="BizKB-A Orchestration API", version="0.1.0")
_prov: dict[str, str] = {}


class MandateIn(BaseModel):
    max_amount: float
    categories: list[str] = []
    expires: datetime | None = None


class OrchestrateIn(BaseModel):
    text: str
    principal: str
    amount: float | None = None
    merchant: str | None = None
    category: str | None = None
    mandate: MandateIn | None = None
    payload: dict = {}
    auto_approve: bool = False     # mặc định KHÔNG tự phê duyệt tác vụ rủi ro cao
    execute: bool = True


@app.post("/orchestrate")
def orchestrate(req: OrchestrateIn):
    approver = auto_approve if req.auto_approve else (lambda s: (False, None))
    u = lambda x: URIRef(x) if x else None
    ctx = PlanContext(req.amount, u(req.merchant), u(req.category),
                      Mandate(req.mandate.max_amount, [URIRef(c) for c in req.mandate.categories],
                              req.mandate.expires) if req.mandate else None)
    rep = orch.orchestrate(req.text, URIRef(req.principal), approver, req.execute, ctx, req.payload)
    if rep.provenance_ttl:
        _prov[rep.plan_id] = rep.provenance_ttl
    return {k: v for k, v in rep.__dict__.items() if k != "provenance_ttl"}


@app.get("/plans/{plan_id}/provenance", response_class=PlainTextResponse)
def provenance(plan_id: str):
    if plan_id not in _prov:
        raise HTTPException(404, "plan not found")
    return _prov[plan_id]


@app.get("/agents")
def agents():
    kb = orch.kb
    return [{"id": kb.qname(a), "label": i["label"], "provides": [kb.qname(p) for p in i["provides"]],
             "authorities": [kb.qname(x) for x in i["authorities"]], "protocol": i["protocol"]}
            for a in kb.agents() for i in [kb.agent_info(a)]]


class AgentIn(BaseModel):
    turtle: str
    issuer: str
    signature: str


@app.post("/agents")
def register_agent(body: AgentIn):
    """Chỉ nhận descriptor có chữ ký hợp lệ của issuer trong sổ đăng ký."""
    try:
        added = register_attested(orch.kb, registry, body.turtle, URIRef(body.issuer), body.signature)
    except AttestationError as e:
        raise HTTPException(403, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"invalid descriptor: {e}")
    return {"registered": [orch.kb.qname(a) for a in added]}


class CardIn(BaseModel):
    card: dict
    domain: str
    base: str = "https://ndta.gov.vn/agent/"


@app.post("/agents/annotate")
def annotate(body: CardIn):
    """Sinh descriptor NHÁP từ A2A Agent Card; cần chuyên gia bổ sung và issuer ký trước khi đăng ký."""
    r = annotate_agent_card(orch.kb, body.card, URIRef(body.domain), body.base)
    return {"agent": str(r.agent), "coverage": r.coverage, "mapped": r.mapped,
            "unmapped": r.unmapped, "needs_human": r.needs_human, "draft_turtle": r.turtle}
