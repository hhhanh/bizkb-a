"""BizKB-A orchestrator: định tuyến → HTN → matchmaking → kiểm chứng SHACL → thực thi + PROV-O."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from rdflib import URIRef

from .executor import Approver, Executor, auto_approve
from .kb import KnowledgeBase
from .matchmaker import Matchmaker
from .planner import HTNPlanner, PlanningError, Proposer
from .provenance import ProvenanceRecorder
from .router import LabelGrounder
from .verifier import PlanContext, Step, Verifier


@dataclass
class OrchestrationReport:
    plan_id: str
    request: str
    status: str
    goal: str | None = None
    steps: list[dict] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    provenance_ttl: str = ""


class Orchestrator:
    def __init__(self, kb: KnowledgeBase | None = None, grounder=None, proposer: Proposer | None = None,
                 monitor=None):
        self.kb = kb or KnowledgeBase()
        self.monitor = monitor
        self.grounder = grounder or LabelGrounder(self.kb)
        self.verifier = Verifier(self.kb)
        self.planner = HTNPlanner(self.kb, self.verifier, proposer)
        self.matchmaker = Matchmaker(self.kb)

    def orchestrate(self, text: str, principal: URIRef, approver: Approver = auto_approve,
                    execute: bool = True, context: PlanContext | None = None,
                    payload: dict | None = None) -> OrchestrationReport:
        pid = uuid.uuid4().hex[:8]
        rep = OrchestrationReport(pid, text, "started")
        log = rep.log
        kb = self.kb

        # 1. Định tuyến miền
        intent = self.grounder.ground(text)
        if intent.goal is None:
            rep.status = "rejected-ungrounded"
            log.append("Định tuyến: không neo được vào khái niệm Goal nào → TỪ CHỐI (không đoán)")
            return rep
        rep.goal = kb.qname(intent.goal)
        log.append(f"Định tuyến: '{intent.matched_label}' → {rep.goal} @ {kb.qname(intent.domain)} [{intent.grounder}]")

        # 2. Phân rã HTN (lai LLM)
        try:
            steps: list[Step] = self.planner.plan(intent.goal, pid, log, context)
        except PlanningError as e:
            rep.status = "rejected-unplannable"
            log.append(f"Lập kế hoạch thất bại: {e}")
            return rep

        # 3. Matchmaking với cổng chính sách
        for s in steps:
            best, rejected = self.matchmaker.bind(s.task)
            for r in rejected:
                log.append(f"  Loại {r.label} cho {kb.qname(s.task)}: " + "; ".join(r.reasons))
            if best is None:
                rep.status = "rejected-unbindable"
                log.append(f"Không có tác tử hợp lệ cho {kb.qname(s.task)}")
                return rep
            s.agent, s.degree = best.agent, best.degree
            log.append(f"  Gán {kb.qname(s.task)} → {best.label} [{best.degree}, S={best.score}]"
                       + (" [HITL]" if s.hitl else ""))

        # 4. Kiểm chứng toàn kế hoạch
        ok, violations = self.verifier.validate(pid, steps, context, intent.goal)
        rep.steps = [{"order": s.order, "task": kb.qname(s.task), "agent": kb.qname(s.agent),
                      "degree": s.degree, "hitl": s.hitl} for s in steps]
        if not ok:
            rep.status = "rejected-policy"
            rep.violations = [f"[{'bước ' + str(v.order) if v.order else 'kế hoạch'}] {v.message}" for v in violations]
            log.append("Kiểm chứng SHACL: VI PHẠM → " + "; ".join(rep.violations))
            return rep
        log.append("Kiểm chứng SHACL: kế hoạch hợp lệ (N1–N6, C1–C7)")

        # 5. Thực thi + truy vết
        prov = ProvenanceRecorder()
        prov.start_plan(pid, intent.goal, principal)
        if context is not None and context.mandate is not None:
            prov.record_mandate(pid, context.mandate, principal)
        payload = dict(payload or {})
        if context is not None:
            if context.amount is not None:
                payload["amount"] = context.amount
            if context.merchant is not None:
                payload["merchant"] = kb.label(context.merchant)
        if execute:
            cap = context.mandate.max_amount if context is not None and context.mandate is not None else None
            result = Executor(kb, prov, self.monitor, cap).run(pid, steps, principal, approver, log, payload)
            rep.status = result["status"]
        else:
            rep.status = "planned"
        rep.provenance_ttl = prov.turtle()
        return rep
