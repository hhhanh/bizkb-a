"""Bước 2 - Phân rã HTN lai: thư viện phương thức trước, LLM dự phòng có kiểm chứng (C3)."""
from __future__ import annotations

import json
import os
from typing import Protocol

from rdflib import URIRef

from .kb import KnowledgeBase
from .matchmaker import ACCEPTED_DEGREES, match_degree
from .kb import BKA, COM
from .verifier import PlanContext, Step, Verifier, Violation


class PlanningError(Exception):
    """Carries the norms that blocked the last proposal, so the rejection can be explained."""

    def __init__(self, message: str, violations: list | None = None):
        super().__init__(message)
        self.violations = violations or []


class Proposer(Protocol):
    def propose(self, goal: URIRef, candidates: list[URIRef], feedback: list[Violation]) -> list[URIRef]: ...


class ScriptedProposer:
    """Mô phỏng LLM cho thực nghiệm tái lập được: trả lần lượt các đề xuất đã kịch bản hóa."""

    def __init__(self, scripts: dict[URIRef, list[list[URIRef]]]):
        self.scripts = {k: list(v) for k, v in scripts.items()}

    def propose(self, goal, candidates, feedback):
        queue = self.scripts.get(goal, [])
        return queue.pop(0) if queue else []


class LLMProposer:
    """Đề xuất phân rã bằng LLM trên tập khái niệm tác vụ của miền; phản hồi vi phạm SHACL được đưa lại."""

    def __init__(self, kb: KnowledgeBase, model: str | None = None):
        import anthropic
        self.kb, self.client = kb, anthropic.Anthropic()
        self.model = model or os.environ.get("BKA_LLM_MODEL", "claude-sonnet-4-6")

    def propose(self, goal, candidates, feedback):
        catalog = {self.kb.qname(t): self.kb.label(t) for t in candidates}
        fb = "; ".join(v.message for v in feedback) or "none"
        prompt = (f"Goal: {self.kb.qname(goal)} ({self.kb.label(goal)}).\n"
                  f"Available task concepts: {json.dumps(catalog, ensure_ascii=False)}\n"
                  f"Previous validation errors: {fb}\n"
                  "Return an ordered decomposition using ONLY the ids above. "
                  'Reply with JSON only: {"subtasks": ["id", ...]}')
        msg = self.client.messages.create(model=self.model, max_tokens=300,
                                          messages=[{"role": "user", "content": prompt}])
        raw = "".join(b.text for b in msg.content if b.type == "text").strip().strip("`").removeprefix("json")
        ids = json.loads(raw).get("subtasks", [])
        ns = self.kb.g.namespace_manager
        return [ns.expand_curie(i) if ":" in i and not i.startswith("http") else URIRef(i) for i in ids]


class HTNPlanner:
    def __init__(self, kb: KnowledgeBase, verifier: Verifier, proposer: Proposer | None = None,
                 max_attempts: int = 3, max_depth: int = 6):
        self.kb, self.verifier, self.proposer = kb, verifier, proposer
        self.max_attempts, self.max_depth = max_attempts, max_depth
        self.attempts: list[dict] = []          # structured record of LLM proposals (for the API/UI)

    def _provided(self, task: URIRef) -> bool:
        return any(match_degree(self.kb, task, adv) in ACCEPTED_DEGREES
                   for a in self.kb.agents() for adv in self.kb.agent_info(a)["provides"])

    def _needs_hitl(self, task: URIRef, ctx: PlanContext | None) -> bool:
        """HITL nếu rủi ro >= 3 và không có IntentMandate; luôn HITL với thanh toán xuyên biên giới."""
        if task == COM.ExecutePayment and ctx and ctx.merchant is not None \
                and self.kb.attr(ctx.merchant, COM.crossBorder) is True:
            return True
        covered = bool(ctx and ctx.mandate and ctx.mandate.kind == BKA.IntentMandate)
        return self.kb.risk_tier(task) >= 3 and not covered

    def _to_steps(self, tasks: list[URIRef], ctx: PlanContext | None = None) -> list[Step]:
        return [Step(i + 1, t, hitl=self._needs_hitl(t, ctx)) for i, t in enumerate(tasks)]

    def decompose(self, task: URIRef, plan_id: str, log: list[str], depth: int = 0,
                  ctx: PlanContext | None = None) -> list[URIRef]:
        if depth > self.max_depth:
            raise PlanningError("vượt độ sâu phân rã tối đa")
        methods = self.kb.methods_for(task)
        if methods:
            m, subs, status = methods[0]
            log.append(f"HTN: {self.kb.qname(task)} → {self.kb.qname(m)} [{status}] ({len(subs)} tác vụ con)")
            out: list[URIRef] = []
            for s in subs:
                out.extend(self.decompose(s, plan_id, log, depth + 1, ctx))
            return out
        if self._provided(task):
            return [task]
        return self._llm_fallback(task, plan_id, log, ctx)

    def _llm_fallback(self, goal: URIRef, plan_id: str, log: list[str], ctx: PlanContext | None = None) -> list[URIRef]:
        if self.proposer is None:
            raise PlanningError(f"không có phương thức cho {self.kb.qname(goal)} và không bật LLM dự phòng")
        domain = self.kb.domain_of(goal)
        candidates = self.kb.tasks_in_domain(domain) if domain is not None else []
        feedback: list[Violation] = []
        for attempt in range(1, self.max_attempts + 1):
            proposal = self.proposer.propose(goal, candidates, feedback)
            if not proposal:
                break
            shown = ", ".join(self.kb.qname(t) for t in proposal)
            ok, feedback = self.verifier.validate(f"{plan_id}-proposal{attempt}", self._to_steps(proposal, ctx), ctx, goal)
            self.attempts.append({"prop": [str(t).rsplit("#", 1)[-1] for t in proposal], "ok": ok,
                                  "v": sorted({v.message.split(":", 1)[0].strip() for v in feedback})})
            if ok:
                m = self.kb.add_method(goal, proposal, "proposed-by-llm")
                log.append(f"LLM đề xuất #{attempt} [{shown}] → CHẤP NHẬN; lưu {self.kb.qname(m)} chờ chuyên gia duyệt")
                return proposal
            log.append(f"LLM đề xuất #{attempt} [{shown}] → BÁC BỎ: " + "; ".join(v.message for v in feedback))
        raise PlanningError(f"LLM không đưa ra được phân rã hợp lệ cho {self.kb.qname(goal)}", feedback)

    def plan(self, goal: URIRef, plan_id: str, log: list[str], ctx: PlanContext | None = None) -> list[Step]:
        self.attempts = []
        return self._to_steps(self.decompose(goal, plan_id, log, ctx=ctx), ctx)
