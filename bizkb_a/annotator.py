"""Chú giải bán tự động: A2A Agent Card (JSON) -> descriptor ngữ nghĩa NHÁP (giảm chi phí kỹ nghệ ontology).

Nguyên tắc phân vai:
- Máy tự làm: ánh xạ skill -> khái niệm tác vụ (theo nhãn SKOS; tùy chọn LLM trên tập đóng), I/O cơ bản.
- Con người/issuer bắt buộc làm: thẩm quyền pháp lý, lớp dữ liệu được phép, độ tin cậy, chứng thực.
Descriptor sinh ra mang bka:descriptorStatus "draft" -> matchmaker loại cho tới khi được xác nhận.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS

from .kb import BKA, KnowledgeBase


def _norm(s: str) -> str:
    return " ".join(unicodedata.normalize("NFC", s).lower().split())


@dataclass
class AnnotationResult:
    agent: URIRef
    turtle: str
    mapped: dict[str, str] = field(default_factory=dict)      # skill id -> task concept
    unmapped: list[str] = field(default_factory=list)
    needs_human: list[str] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        n = len(self.mapped) + len(self.unmapped)
        return len(self.mapped) / n if n else 0.0


def annotate_agent_card(kb: KnowledgeBase, card: dict, domain: URIRef, base: str) -> AnnotationResult:
    slug = re.sub(r"[^a-z0-9]+", "_", _norm(card.get("name", "agent"))).strip("_")
    agent = URIRef(f"{base}{slug}")
    tasks = [t for t in kb.tasks_in_domain(domain)]
    g = Graph()
    g.bind("bka", BKA)
    g.add((agent, RDF.type, BKA.Agent))
    g.add((agent, RDFS.label, Literal(card.get("name", slug))))
    g.add((agent, BKA.inDomain, domain))
    g.add((agent, BKA.protocol, Literal("a2a")))
    g.add((agent, BKA.descriptorStatus, Literal("draft")))
    if card.get("url"):
        g.add((agent, BKA.endpoint, Literal(card["url"])))
    res = AnnotationResult(agent, "")
    for sk in card.get("skills", []):
        text = _norm(" ".join([sk.get("name", ""), sk.get("description", ""), *sk.get("tags", [])]))
        best = None
        for t in tasks:
            for lab in kb.labels(t):
                if _norm(lab) in text and (best is None or len(lab) > len(best[1])):
                    best = (t, lab)
        if best:
            g.add((agent, BKA.provides, best[0]))
            res.mapped[sk.get("id", sk.get("name"))] = kb.qname(best[0])
        else:
            res.unmapped.append(sk.get("id", sk.get("name")))
    res.needs_human = ["bka:hasAuthority (thẩm quyền pháp lý)", "bka:handlesDataClass (lớp dữ liệu được phép)",
                       "bka:trust (do issuer đánh giá)", "chứng thực bởi tổ chức tin cậy"]
    if res.unmapped:
        res.needs_human.insert(0, f"ánh xạ thủ công {len(res.unmapped)} skill: {res.unmapped}")
    res.turtle = g.serialize(format="turtle")
    return res
