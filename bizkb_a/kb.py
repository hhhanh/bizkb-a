"""Domain Knowledge Base + Semantic Capability Registry (kế thừa BizKB-BP)."""
from __future__ import annotations

import uuid
from pathlib import Path

from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.collection import Collection
from rdflib.namespace import RDF, RDFS, SKOS

BKA = Namespace("https://ndta.gov.vn/ns/bizkb-a#")
GOV = Namespace("https://ndta.gov.vn/ns/govtech#")
COM = Namespace("https://ndta.gov.vn/ns/acommerce#")
ROOT = Path(__file__).resolve().parent.parent


class KnowledgeBase:
    def __init__(self, ontology_dir: Path | None = None, shapes_file: Path | None = None):
        self.g = Graph()
        for f in sorted((ontology_dir or ROOT / "ontology").glob("*.ttl")):
            self.g.parse(f, format="turtle")
        self.shapes = Graph()
        for f in ([shapes_file] if shapes_file else sorted((ROOT / "shapes").glob("*.ttl"))):
            self.shapes.parse(f, format="turtle")
        for p, ns in (("bka", BKA), ("gov", GOV), ("com", COM), ("skos", SKOS)):
            self.g.bind(p, ns)

    # ---------- generic ----------
    def qname(self, u: URIRef) -> str:
        return self.g.namespace_manager.normalizeUri(u)

    def is_subclass(self, sub: URIRef, sup: URIRef) -> bool:
        """Reflexive-transitive rdfs:subClassOf."""
        return sup in set(self.g.transitive_objects(sub, RDFS.subClassOf))

    def is_task_concept(self, c: URIRef) -> bool:
        return c != BKA.Task and self.is_subclass(c, BKA.Task)

    def labels(self, c: URIRef) -> list[str]:
        return [str(o) for p in (SKOS.prefLabel, SKOS.altLabel, RDFS.label) for o in self.g.objects(c, p)]

    def label(self, c: URIRef, lang: str | None = None) -> str:
        if lang:
            for p in (SKOS.prefLabel, RDFS.label):
                for o in self.g.objects(c, p):
                    if isinstance(o, Literal) and o.language == lang:
                        return str(o)
        for p in (SKOS.prefLabel, RDFS.label):
            v = self.g.value(c, p)
            if v is not None:
                return str(v)
        return self.qname(c)

    def attr(self, s: URIRef, p: URIRef):
        v = self.g.value(s, p)
        return v.toPython() if isinstance(v, Literal) else v

    # ---------- tasks & goals ----------
    def goals(self) -> list[URIRef]:
        return [c for c in set(self.g.subjects(RDFS.subClassOf, None))
                if c != BKA.Goal and self.is_subclass(c, BKA.Goal)]

    def domain_of(self, task: URIRef):
        return self.g.value(task, BKA.taskDomain)

    def tasks_in_domain(self, domain: URIRef) -> list[URIRef]:
        return [t for t in self.g.subjects(BKA.taskDomain, domain)
                if not self.is_subclass(t, BKA.Goal)]

    def risk_tier(self, task: URIRef) -> int:
        return int(self.attr(task, BKA.riskTier) or 0)

    # ---------- HTN methods ----------
    def methods_for(self, task: URIRef) -> list[tuple[URIRef, list[URIRef], str]]:
        out = []
        for m in self.g.subjects(BKA.decomposes, task):
            lst = self.g.value(m, BKA.subtasks)
            subs = list(Collection(self.g, lst)) if lst is not None else []
            out.append((m, subs, str(self.g.value(m, BKA.status) or "curated")))
        out.sort(key=lambda x: x[2] != "curated")   # curated first
        return out

    def add_method(self, goal: URIRef, subtasks: list[URIRef], status: str = "proposed-by-llm") -> URIRef:
        m = BKA[f"M_{uuid.uuid4().hex[:8]}"]
        head = BNode()
        Collection(self.g, head, list(subtasks))
        self.g.add((m, RDF.type, BKA.Method))
        self.g.add((m, BKA.decomposes, goal))
        self.g.add((m, BKA.subtasks, head))
        self.g.add((m, BKA.status, Literal(status)))
        return m

    # ---------- agents ----------
    def agents(self) -> list[URIRef]:
        return list(self.g.subjects(RDF.type, BKA.Agent))

    def agent_info(self, a: URIRef) -> dict:
        g = self.g
        return {
            "uri": a, "label": self.label(a),
            "provides": list(g.objects(a, BKA.provides)),
            "domain": g.value(a, BKA.inDomain),
            "authorities": set(g.objects(a, BKA.hasAuthority)),
            "data_classes": set(g.objects(a, BKA.handlesDataClass)),
            "trust": float(self.attr(a, BKA.trust) or 0.0),
            "cost": float(self.attr(a, BKA.cost) or 0.5),
            "protocol": str(self.attr(a, BKA.protocol) or "local"),
            "endpoint": self.attr(a, BKA.endpoint),
            "attested": any((i, RDF.type, BKA.TrustedIssuer) in g for i in g.objects(a, BKA.attestedBy)),
            "status": str(self.attr(a, BKA.descriptorStatus) or "attested"),
            "suspended": (a, BKA.suspended, Literal(True)) in g,
            # simulation hook for evaluation only: how a local (simulated) agent behaves at run time
            "behaviour": str(self.attr(a, BKA.simBehaviour) or "honest"),
        }

    def register_agent_turtle(self, ttl: str) -> list[URIRef]:
        """Nạp descriptor ngữ nghĩa của tác tử mới (sinh từ Agent Card/MCP schema)."""
        tmp = Graph().parse(data=ttl, format="turtle")
        self.g += tmp
        return list(tmp.subjects(RDF.type, BKA.Agent))
