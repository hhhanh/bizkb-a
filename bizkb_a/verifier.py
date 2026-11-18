"""Bước 4 - Kiểm chứng kế hoạch bằng SHACL (bka-norm, cấp bước và cấp kế hoạch)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from pyshacl import validate
from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS, SH, XSD

from .kb import BKA, KnowledgeBase

PLAN = Namespace("https://ndta.gov.vn/plan/")


@dataclass
class Mandate:
    """Ủy quyền do người dùng cấp trước (IntentMandate, human-not-present)."""
    max_amount: float
    categories: list[URIRef] = field(default_factory=list)
    expires: datetime | None = None
    issued_by: URIRef | None = None
    kind: URIRef = BKA.IntentMandate


@dataclass
class PlanContext:
    """Tham số giao dịch mà chuẩn tắc cấp kế hoạch cần kiểm tra."""
    amount: float | None = None
    merchant: URIRef | None = None
    category: URIRef | None = None
    mandate: Mandate | None = None


@dataclass
class Step:
    order: int
    task: URIRef
    agent: URIRef | None = None
    degree: str | None = None
    hitl: bool = False


@dataclass
class Violation:
    order: int | None
    message: str


class Verifier:
    def __init__(self, kb: KnowledgeBase):
        self.kb = kb

    @staticmethod
    def step_uri(plan_id: str, order: int) -> URIRef:
        return PLAN[f"{plan_id}/step-{order}"]

    def plan_graph(self, plan_id: str, steps: list[Step], ctx: PlanContext | None = None,
                   goal: URIRef | None = None) -> Graph:
        g = Graph()
        p = PLAN[plan_id]
        g.add((p, RDF.type, BKA.Plan))
        if goal is not None:
            g.add((p, BKA.taskType, goal))
        if ctx is not None:
            if ctx.amount is not None:
                g.add((p, BKA.amount, Literal(ctx.amount, datatype=XSD.decimal)))
            if ctx.merchant is not None:
                g.add((p, BKA.merchant, ctx.merchant))
            if ctx.category is not None:
                g.add((p, BKA.goodsCategory, ctx.category))
            if ctx.mandate is not None:
                m = PLAN[f"{plan_id}/mandate"]
                g.add((p, BKA.hasMandate, m))
                g.add((m, RDF.type, ctx.mandate.kind))
                g.add((m, BKA.maxAmount, Literal(ctx.mandate.max_amount, datatype=XSD.decimal)))
                for c in ctx.mandate.categories:
                    g.add((m, BKA.allowedCategory, c))
                if ctx.mandate.expires is not None:
                    g.add((m, BKA.expires, Literal(ctx.mandate.expires, datatype=XSD.dateTime)))
        for s in steps:
            n = self.step_uri(plan_id, s.order)
            g.add((n, RDF.type, BKA.PlanStep))
            g.add((n, BKA.inPlan, p))
            g.add((n, BKA.order, Literal(s.order, datatype=XSD.integer)))
            g.add((n, BKA.taskType, s.task))
            if s.hitl:
                g.add((n, BKA.requiresHumanApproval, Literal(True)))
            if s.agent is not None:
                g.add((n, BKA.boundAgent, s.agent))
                g.add((n, BKA.matchDegree, Literal(s.degree)))
        return g

    def _scope(self, pg: Graph, steps: list[Step], ctx: PlanContext | None, goal: URIRef | None) -> Graph:
        """Only the KB triples the shapes can reach: tasks (with their superclass chain),
        bound agents, mandate/merchant/category. Keeps validation cost independent of KB size."""
        g = Graph()
        g += pg
        seeds = set()
        for s in steps:
            t = s.task
            while t is not None and t not in seeds:
                seeds.add(t)
                t = self.kb.g.value(t, RDFS.subClassOf)
            if s.agent is not None:
                seeds.add(s.agent)
        if goal is not None:
            seeds.add(goal)
        if ctx is not None:
            for x in (ctx.merchant, ctx.category):
                if x is not None:
                    seeds.add(x)
        for s in list(seeds):
            for p, o in self.kb.g.predicate_objects(s):
                g.add((s, p, o))
                if isinstance(o, URIRef):
                    for p2, o2 in self.kb.g.predicate_objects(o):
                        g.add((o, p2, o2))
        return g

    def validate(self, plan_id: str, steps: list[Step], ctx: PlanContext | None = None,
                 goal: URIRef | None = None, scoped: bool = False) -> tuple[bool, list[Violation]]:
        pg = self.plan_graph(plan_id, steps, ctx, goal)
        if scoped:
            data = self._scope(pg, steps, ctx, goal)
        else:
            data = Graph()
            data += self.kb.g
            data += pg
        conforms, report, _ = validate(data, shacl_graph=self.kb.shapes, advanced=True, inference="none")
        order_of = {self.step_uri(plan_id, s.order): s.order for s in steps}
        violations = [Violation(order_of.get(report.value(r, SH.focusNode)), str(report.value(r, SH.resultMessage)))
                      for r in report.subjects(RDF.type, SH.ValidationResult)]
        violations.sort(key=lambda v: (v.order or 0, v.message))
        return conforms, violations
