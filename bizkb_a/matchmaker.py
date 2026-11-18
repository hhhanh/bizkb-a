"""Bước 3 - Matchmaking ngữ nghĩa đa tiêu chí. Chính sách là CỔNG CỨNG, không phải trọng số."""
from __future__ import annotations

from dataclasses import dataclass, field

from rdflib import URIRef

from .kb import BKA, KnowledgeBase

DEGREE_VALUE = {"exact": 1.0, "plugin": 0.8, "subsumes": 0.5, "fail": 0.0}
ACCEPTED_DEGREES = {"exact", "plugin"}          # valid(pi, beta) đòi hỏi deg >= plugin


def match_degree(kb: KnowledgeBase, requested: URIRef, advertised: URIRef) -> str:
    if requested == advertised:
        return "exact"
    if kb.is_subclass(requested, advertised):     # năng lực quảng bá tổng quát hơn yêu cầu
        return "plugin"
    if kb.is_subclass(advertised, requested):     # năng lực hẹp hơn yêu cầu
        return "subsumes"
    return "fail"


@dataclass
class Candidate:
    agent: URIRef
    label: str
    degree: str
    score: float = 0.0
    gate_ok: bool = True
    reasons: list[str] = field(default_factory=list)
    codes: list[dict] = field(default_factory=list)   # machine-readable gate failures (for the API/UI)


class Matchmaker:
    def __init__(self, kb: KnowledgeBase, w=(0.4, 0.3, 0.1, 0.2)):
        self.kb, self.w = kb, w

    def _gate(self, task: URIRef, info: dict, degree: str, codes: list | None = None) -> list[str]:
        reasons = []
        codes = codes if codes is not None else []
        if info["status"] == "draft":
            reasons.append("descriptor còn ở trạng thái nháp, chưa được chuyên gia xác nhận")
            codes.append({"k": "r_draft"})
        if not info["attested"]:
            reasons.append("descriptor chưa được tổ chức tin cậy chứng thực")
            codes.append({"k": "r_attest"})
        if info.get("suspended"):
            reasons.append("tác tử đang bị đình chỉ do hành vi không phù hợp lúc chạy")
            codes.append({"k": "r_suspended"})
        if degree not in ACCEPTED_DEGREES:
            reasons.append(f"mức khớp '{degree}' < plugin")
            codes.append({"k": "r_deg"})
        auth = self.kb.g.value(task, BKA.requiresAuthority)
        if auth is not None and auth not in info["authorities"]:
            reasons.append(f"thiếu thẩm quyền {self.kb.qname(auth)}")
            codes.append({"k": "r_auth", "x": str(auth).split("#")[-1]})
        dc = self.kb.g.value(task, BKA.dataClass)
        if dc is not None and dc not in info["data_classes"]:
            reasons.append(f"không được phép xử lý {self.kb.qname(dc)}")
            codes.append({"k": "r_dc", "x": str(dc).split("#")[-1]})
        return reasons

    def candidates(self, task: URIRef) -> list[Candidate]:
        out = []
        task_dom = self.kb.domain_of(task)
        for a in self.kb.agents():
            info = self.kb.agent_info(a)
            degree = max((match_degree(self.kb, task, adv) for adv in info["provides"]),
                         key=lambda d: DEGREE_VALUE[d], default="fail")
            if degree == "fail":
                continue
            c = Candidate(a, info["label"], degree)
            c.reasons = self._gate(task, info, degree, c.codes)
            c.gate_ok = not c.reasons
            dom = 1.0 if info["domain"] == task_dom else 0.5
            w1, w2, w3, w4 = self.w
            c.score = round(w1 * DEGREE_VALUE[degree] + w2 * info["trust"] + w3 * (1 - info["cost"]) + w4 * dom, 4)
            out.append(c)
        # ứng viên qua cổng xếp trước, sau đó theo điểm
        return sorted(out, key=lambda c: (not c.gate_ok, -c.score))

    def bind(self, task: URIRef) -> tuple[Candidate | None, list[Candidate]]:
        cands = self.candidates(task)
        best = next((c for c in cands if c.gate_ok), None)
        return best, [c for c in cands if not c.gate_ok]
