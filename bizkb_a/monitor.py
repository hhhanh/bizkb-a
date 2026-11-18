"""Bộ giám sát hành vi lúc chạy (runtime conformance monitor).

Chứng thực descriptor chỉ cho biết AI đứng sau một khai báo, không cho biết tác tử HÀNH XỬ đúng như
khai báo. Bộ giám sát đóng một phần khoảng trống đó bằng hai cơ chế:

1. Kiểm tra hiệu ứng sau mỗi bước: kết quả quan sát được được biểu diễn thành đồ thị bka:Outcome và
   kiểm bằng các shape E1–E4 (shapes/runtime/effects.ttl). Vi phạm -> đình chỉ tác tử (bka:suspended),
   ghi vào PROV-O, dừng kế hoạch. Cổng của bộ so khớp và chuẩn tắc N7 loại tác tử bị đình chỉ khỏi mọi
   kế hoạch sau đó.
2. Thăm dò (probe): chạy một tác vụ tổng hợp có kết quả mong đợi đã biết với tác tử TRƯỚC khi cho phép
   phục vụ giao dịch thật.

Giới hạn: (1) là kiểm soát phát hiện — hành vi sai (vd. thu thừa tiền) đã xảy ra một lần trước khi bị
bắt; (2) có thể bị qua mặt nếu tác tử cư xử đúng khi bị thăm dò và sai khi phục vụ thật.
"""
from __future__ import annotations

from pathlib import Path

from pyshacl import validate
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, SH, XSD

from .kb import BKA, KnowledgeBase
from .verifier import Step

OUT = Namespace("https://ndta.gov.vn/outcome/")
EFFECTS = Path(__file__).resolve().parent.parent / "shapes" / "runtime" / "effects.ttl"


class ConformanceMonitor:
    def __init__(self, kb: KnowledgeBase, shapes_file: Path = EFFECTS, enforce: bool = True):
        """enforce=False: observe and count only (no suspension, no halt) — used as an oracle."""
        self.kb, self.enforce = kb, enforce
        self.shapes = Graph().parse(shapes_file, format="turtle")
        self.suspensions: dict[URIRef, list[str]] = {}
        self.executions: dict[URIRef, int] = {}
        self.deviations: dict[URIRef, int] = {}

    def note(self, agent: URIRef, violations: list[str]) -> None:
        """Record one production execution and whether it deviated from declared effects."""
        self.executions[agent] = self.executions.get(agent, 0) + 1
        if violations:
            self.deviations[agent] = self.deviations.get(agent, 0) + 1

    def outcome_graph(self, plan_id: str, step: Step, payload: dict, out: dict,
                      mandate_cap: float | None) -> Graph:
        g = Graph()
        o = OUT[f"{plan_id}/step-{step.order}"]
        g.add((o, RDF.type, BKA.Outcome))
        g.add((o, BKA.taskType, step.task))
        if step.agent is not None:
            g.add((o, BKA.boundAgent, step.agent))
        g.add((o, BKA.outcomeStatus, Literal(str(out.get("status", "")))))
        if out.get("ref"):
            g.add((o, BKA.outcomeRef, Literal(str(out["ref"]))))
        if "charged_amount" in out and "amount" in payload:
            g.add((o, BKA.chargedAmount, Literal(float(out["charged_amount"]), datatype=XSD.decimal)))
            g.add((o, BKA.authorisedAmount, Literal(float(payload["amount"]), datatype=XSD.decimal)))
            if mandate_cap is not None:
                g.add((o, BKA.mandateCap, Literal(float(mandate_cap), datatype=XSD.decimal)))
        for f in payload:
            g.add((o, BKA.disclosedField, Literal(f)))
        for f in (out.get("data") or {}):
            g.add((o, BKA.returnedField, Literal(f)))
        return g

    def check(self, plan_id: str, step: Step, payload: dict, out: dict,
              mandate_cap: float | None = None) -> list[str]:
        g = self.outcome_graph(plan_id, step, payload, out, mandate_cap)
        conforms, report, _ = validate(g, shacl_graph=self.shapes, advanced=True, inference="none")
        if conforms:
            return []
        return sorted({str(report.value(r, SH.resultMessage))
                       for r in report.subjects(RDF.type, SH.ValidationResult)})

    def suspend(self, agent: URIRef, violations: list[str]) -> None:
        self.kb.g.add((agent, BKA.suspended, Literal(True)))
        self.suspensions.setdefault(agent, []).extend(violations)

    def probe(self, agent: URIRef, adapter, task: URIRef, probe_payload: dict) -> list[str]:
        """Run a synthetic task with a known expected outcome before the agent serves real requests."""
        info = self.kb.agent_info(agent)
        out = adapter.call(info, self.kb.label(task), probe_payload)
        viol = self.check("probe", Step(0, task, agent, "probe"), probe_payload, out)
        if viol:
            self.suspend(agent, viol)
        return viol
