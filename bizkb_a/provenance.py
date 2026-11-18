"""Truy vết trách nhiệm theo W3C PROV-O (C4)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import PROV, RDF, RDFS, XSD

from .kb import BKA
from .verifier import PLAN, Step

ORCH = URIRef("https://ndta.gov.vn/agent/bizkb-a-orchestrator")


def _now() -> Literal:
    return Literal(datetime.now(timezone.utc).isoformat(), datatype=XSD.dateTime)


class ProvenanceRecorder:
    def __init__(self):
        self.g = Graph()
        self.g.bind("prov", PROV)
        self.g.bind("bka", BKA)

    def start_plan(self, plan_id: str, goal: URIRef, principal: URIRef, method: str | None = None) -> URIRef:
        p = PLAN[plan_id]
        self.g.add((p, RDF.type, PROV.Plan))
        self.g.add((p, BKA.taskType, goal))
        act = PLAN[f"{plan_id}/orchestration"]
        self.g.add((act, RDF.type, PROV.Activity))
        self.g.add((act, PROV.startedAtTime, _now()))
        self.g.add((act, PROV.wasAssociatedWith, ORCH))
        self.g.add((ORCH, RDF.type, PROV.SoftwareAgent))
        self.g.add((ORCH, PROV.actedOnBehalfOf, principal))
        self.g.add((principal, RDF.type, PROV.Person))
        if method:
            self.g.add((act, RDFS.comment, Literal(f"method={method}")))
        return act

    def record_step(self, plan_id: str, step: Step, principal: URIRef, status: str,
                    output: dict | None = None, approver: URIRef | None = None,
                    disclosed: list[str] | None = None) -> URIRef:
        a = PLAN[f"{plan_id}/step-{step.order}/run"]
        self.g.add((a, RDF.type, PROV.Activity))
        self.g.add((a, BKA.taskType, step.task))
        self.g.add((a, PROV.startedAtTime, _now()))
        self.g.add((a, PROV.wasInformedBy, PLAN[f"{plan_id}/orchestration"]))
        self.g.add((a, RDFS.comment, Literal(status)))
        if step.agent is not None:
            self.g.add((a, PROV.wasAssociatedWith, step.agent))
            self.g.add((step.agent, RDF.type, PROV.SoftwareAgent))
            self.g.add((step.agent, PROV.actedOnBehalfOf, principal))
        if approver is not None:
            self.g.add((a, BKA.approvedBy, approver))
        for f in disclosed or []:
            self.g.add((a, BKA.disclosedField, Literal(f)))
        if output is not None:
            e = PLAN[f"{plan_id}/step-{step.order}/output"]
            self.g.add((e, RDF.type, PROV.Entity))
            self.g.add((e, PROV.wasGeneratedBy, a))
            self.g.add((e, PROV.value, Literal(json.dumps(output, ensure_ascii=False))))
        self.g.add((a, PROV.endedAtTime, _now()))
        return a

    def record_mandate(self, plan_id: str, mandate, principal: URIRef) -> URIRef:
        m = PLAN[f"{plan_id}/mandate"]
        self.g.add((m, RDF.type, PROV.Entity))
        self.g.add((m, RDF.type, mandate.kind))
        self.g.add((m, PROV.wasAttributedTo, principal))
        self.g.add((m, BKA.maxAmount, Literal(mandate.max_amount)))
        self.g.add((PLAN[f"{plan_id}/orchestration"], PROV.used, m))
        return m

    def turtle(self) -> str:
        return self.g.serialize(format="turtle")
