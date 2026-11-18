"""Bước 1 - Định tuyến miền: neo ý định vào khái niệm Goal của ontology (không đoán)."""
from __future__ import annotations

import json
import os
import unicodedata
from dataclasses import dataclass

from rdflib import URIRef

from .kb import KnowledgeBase


@dataclass
class Intent:
    text: str
    goal: URIRef | None
    domain: URIRef | None
    matched_label: str | None
    grounder: str


def _norm(s: str) -> str:
    return " ".join(unicodedata.normalize("NFC", s).lower().split())


class LabelGrounder:
    """Neo theo nhãn SKOS (prefLabel/altLabel); chọn nhãn khớp dài nhất."""

    name = "skos-label"

    def __init__(self, kb: KnowledgeBase):
        self.kb = kb

    def ground(self, text: str) -> Intent:
        t = _norm(text)
        best = None
        for goal in self.kb.goals():
            for lab in self.kb.labels(goal):
                if _norm(lab) in t and (best is None or len(lab) > len(best[1])):
                    best = (goal, lab)
        if best is None:
            return Intent(text, None, None, None, self.name)
        return Intent(text, best[0], self.kb.domain_of(best[0]), best[1], self.name)


class LLMGrounder:
    """Neo bằng LLM trên TẬP ĐÓNG các khái niệm Goal. ID trả về ngoài tập -> từ chối.

    Cần ANTHROPIC_API_KEY; model cấu hình qua BKA_LLM_MODEL. Có thể thay bằng LLM nội địa
    (on-prem) để bảo đảm chủ quyền dữ liệu.
    """

    name = "llm-closed-set"

    def __init__(self, kb: KnowledgeBase, model: str | None = None):
        import anthropic  # optional dependency
        self.kb = kb
        self.client = anthropic.Anthropic()
        self.model = model or os.environ.get("BKA_LLM_MODEL", "claude-sonnet-4-6")

    def ground(self, text: str) -> Intent:
        catalog = {self.kb.qname(g): self.kb.labels(g) for g in self.kb.goals()}
        prompt = ("Map the user request to exactly one goal id from the catalog, or null if none fits.\n"
                  f"Catalog: {json.dumps(catalog, ensure_ascii=False)}\nRequest: {text}\n"
                  'Reply with JSON only: {"goal_id": "<id or null>"}')
        msg = self.client.messages.create(model=self.model, max_tokens=100,
                                          messages=[{"role": "user", "content": prompt}])
        raw = "".join(b.text for b in msg.content if b.type == "text").strip().strip("`").removeprefix("json")
        gid = json.loads(raw).get("goal_id")
        goal = next((g for g in self.kb.goals() if self.kb.qname(g) == gid), None)  # closed-set check
        return Intent(text, goal, self.kb.domain_of(goal) if goal else None, gid if goal else None, self.name)
