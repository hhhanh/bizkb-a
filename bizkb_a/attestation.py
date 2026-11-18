"""Chứng thực descriptor tác tử bằng chữ ký Ed25519 của tổ chức tin cậy (giảm rủi ro 'descriptor tự khai').

Luồng: tổ chức cấp phép (vd. cơ quan quản lý TMĐT, NHNN) xác minh tác tử ngoài hệ thống -> ký descriptor ->
BizKB-A chỉ nạp descriptor có chữ ký hợp lệ của issuer có trong sổ đăng ký, rồi gắn bka:attestedBy.

v0.5: chữ ký được tính trên dạng chuẩn hóa của ĐỒ THỊ (RDF Dataset Canonicalization, RDFC-1.0 /
URDNA2015, N-Quads), không trên bytes của một cách tuần tự hóa cụ thể. Vì vậy cùng một descriptor ở
Turtle, JSON-LD, N-Triples hay thứ tự bộ ba khác đều xác minh được, còn mọi thay đổi về nội dung
(một bộ ba thêm, bớt hay sửa) đều làm chữ ký mất hiệu lực. Chế độ canonical=False giữ hành vi v0.4
(ký trên bytes Turtle) chỉ để so sánh trong thực nghiệm.

Chưa có: thu hồi khóa (revocation) và xoay vòng khóa (rotation) của issuer.
"""
from __future__ import annotations

import base64

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pyld import jsonld
from rdflib import Graph, URIRef
from rdflib.namespace import RDF

from .kb import BKA, KnowledgeBase


class AttestationError(Exception):
    pass


def canonical_nquads(data: str, fmt: str = "turtle") -> str:
    """RDFC-1.0 (URDNA2015) canonical N-Quads of the graph serialised in `data`."""
    nt = Graph().parse(data=data, format=fmt).serialize(format="nt")
    return jsonld.normalize(nt, {"algorithm": "URDNA2015", "inputFormat": "application/n-quads",
                                 "format": "application/n-quads"})


def _payload(data: str, fmt: str, canonical: bool) -> bytes:
    return (canonical_nquads(data, fmt) if canonical else data).encode("utf-8")


def sign(data: str, key: Ed25519PrivateKey, fmt: str = "turtle", canonical: bool = True) -> str:
    return base64.b64encode(key.sign(_payload(data, fmt, canonical))).decode()


class IssuerRegistry:
    def __init__(self):
        self._keys: dict[URIRef, Ed25519PublicKey] = {}

    def add(self, issuer: URIRef, key: Ed25519PublicKey):
        self._keys[issuer] = key

    def verify(self, issuer: URIRef, data: str, signature_b64: str, fmt: str = "turtle",
               canonical: bool = True) -> None:
        key = self._keys.get(issuer)
        if key is None:
            raise AttestationError(f"issuer không có trong sổ đăng ký: {issuer}")
        try:
            key.verify(base64.b64decode(signature_b64), _payload(data, fmt, canonical))
        except InvalidSignature as e:
            raise AttestationError("chữ ký không hợp lệ hoặc nội dung descriptor đã bị sửa") from e


def register_attested(kb: KnowledgeBase, registry: IssuerRegistry, data: str, issuer: URIRef,
                      signature_b64: str, fmt: str = "turtle", canonical: bool = True) -> list[URIRef]:
    registry.verify(issuer, data, signature_b64, fmt, canonical)
    g = Graph().parse(data=data, format=fmt)
    # descriptor không được tự khai attestedBy -> chỉ hệ thống gắn sau khi xác minh chữ ký
    g.remove((None, BKA.attestedBy, None))
    agents = list(g.subjects(RDF.type, BKA.Agent))
    kb.g += g
    kb.g.add((issuer, RDF.type, BKA.TrustedIssuer))
    for a in agents:
        kb.g.add((a, BKA.attestedBy, issuer))
    return agents
