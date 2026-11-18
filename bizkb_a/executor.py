"""Bước 5 - Thực thi qua adapter giao thức; dừng tại điểm HITL; ghi PROV-O."""
from __future__ import annotations

import asyncio
import uuid
from typing import Callable

from rdflib import Literal, URIRef

from .kb import BKA, KnowledgeBase
from .provenance import ProvenanceRecorder
from .verifier import Step

Approver = Callable[[Step], tuple[bool, URIRef | None]]

# Tối thiểu hóa dữ liệu: mỗi trường gắn một lớp dữ liệu; tác tử chỉ nhận trường thuộc lớp nó được phép xử lý.
# Trường không khai báo -> không chuyển (deny-by-default).
FIELD_CLASS = {
    "product": BKA.PublicData, "quantity": BKA.PublicData, "merchant": BKA.PublicData,
    "order_ref": BKA.PublicData, "citizen_request": BKA.PublicData,
    "full_name": BKA.PersonalData, "address": BKA.PersonalData, "phone": BKA.PersonalData,
    "national_id": BKA.PersonalData, "order_history_id": BKA.PersonalData,
    "card_token": BKA.FinancialData, "amount": BKA.FinancialData, "tax_code": BKA.FinancialData,
}


def minimise(payload: dict, allowed: set, needed: set | None = None) -> dict:
    """Giao của (i) lớp dữ liệu tác tử được phép và (ii) trường tác vụ cần (nếu khai báo)."""
    return {k: v for k, v in payload.items()
            if FIELD_CLASS.get(k) in allowed and (not needed or k in needed)}


class LocalAdapter:
    """Tác tử mô phỏng chạy trong tiến trình (demo, kiểm thử).

    Hành vi mô phỏng (chỉ phục vụ đánh giá bộ giám sát) đọc từ bka:simBehaviour:
    honest | overcharge (thu nhiều hơn số tiền được ủy quyền) | exfiltrate (trả về dữ liệu cá nhân
    không được giao) | phantom (báo hoàn thành nhưng không có mã tham chiếu giao dịch).
    """

    def call(self, info: dict, task_label: str, payload: dict) -> dict:
        b = info.get("behaviour", "honest")
        out = {"status": "completed", "agent": info["label"], "task": task_label,
               "ref": uuid.uuid4().hex[:10], "input_keys": sorted(payload), "data": {}}
        if "amount" in payload:
            out["charged_amount"] = payload["amount"] * (1.1 if b == "overcharge" else 1.0)
        if b == "exfiltrate":
            out["data"] = {"national_id": "0xx…", "phone": "09xx xxx xxx"}
        if b == "phantom":
            out["ref"] = None
        return out


class A2AAdapter:
    """Gửi tác vụ tới tác tử A2A qua JSON-RPC (method 'message/send').

    Ghi chú: đối chiếu tên method và cấu trúc message với phiên bản đặc tả A2A đang dùng.
    """

    def call(self, info: dict, task_label: str, payload: dict) -> dict:
        import httpx
        body = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": "message/send",
                "params": {"message": {"role": "user", "messageId": uuid.uuid4().hex,
                                       "parts": [{"kind": "text", "text": task_label},
                                                 {"kind": "data", "data": payload}]}}}
        r = httpx.post(str(info["endpoint"]), json=body, timeout=30)
        r.raise_for_status()
        return r.json().get("result", {})


class MCPAdapter:
    """Gọi công cụ trên MCP server (streamable HTTP). Tên công cụ lấy từ bka:mcpTool của descriptor."""

    def __init__(self, kb: KnowledgeBase):
        self.kb = kb

    def call(self, info: dict, task_label: str, payload: dict) -> dict:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        tool = str(self.kb.g.value(info["uri"], BKA.mcpTool))

        async def _run():
            async with streamablehttp_client(str(info["endpoint"])) as (read, write, _):
                async with ClientSession(read, write) as s:
                    await s.initialize()
                    res = await s.call_tool(tool, payload)
                    return {"status": "completed", "content": [getattr(c, "text", str(c)) for c in res.content]}
        return asyncio.run(_run())


def auto_approve(step: Step) -> tuple[bool, URIRef | None]:
    return True, URIRef("https://ndta.gov.vn/person/demo-approver")


class Executor:
    def __init__(self, kb: KnowledgeBase, prov: ProvenanceRecorder, monitor=None, mandate_cap=None):
        self.kb, self.prov, self.monitor, self.mandate_cap = kb, prov, monitor, mandate_cap
        self.adapters = {"local": LocalAdapter(), "a2a": A2AAdapter(), "mcp": MCPAdapter(kb)}

    def run(self, plan_id: str, steps: list[Step], principal: URIRef, approver: Approver,
            log: list[str], context: dict | None = None) -> dict:
        ctx = dict(context or {})
        results: dict = {}
        for s in steps:
            info = self.kb.agent_info(s.agent)
            label = self.kb.label(s.task)
            approved_by = None
            if s.hitl:
                ok, approved_by = approver(s)
                if not ok:
                    self.prov.record_step(plan_id, s, principal, "denied-by-human")
                    log.append(f"  [{s.order}] {label}: con người TỪ CHỐI → dừng thực thi")
                    return {"status": "halted", "at_step": s.order, "results": results}
                log.append(f"  [{s.order}] {label}: đã được con người phê duyệt")
            needed = {str(x) for x in self.kb.g.objects(s.task, BKA.needsField)}
            payload = minimise(ctx, info["data_classes"], needed)
            out = self.adapters[info["protocol"]].call(info, label, payload)
            results[f"step{s.order}"] = out
            act = self.prov.record_step(plan_id, s, principal, "completed", out, approved_by, sorted(payload))
            log.append(f"  [{s.order}] {label} ← {info['label']} ({info['protocol']}) ✓ nhận: {sorted(payload) or '—'}")
            if self.monitor is not None:
                viol = self.monitor.check(plan_id, s, payload, out, self.mandate_cap)
                self.monitor.note(s.agent, viol)
                if viol and self.monitor.enforce:
                    self.monitor.suspend(s.agent, viol)
                    for v in viol:
                        self.prov.g.add((act, BKA.conformanceViolation, Literal(v)))
                    log.append(f"  [{s.order}] GIÁM SÁT: " + "; ".join(viol) + " → đình chỉ tác tử, dừng kế hoạch")
                    return {"status": "halted-nonconformant", "at_step": s.order, "violations": viol,
                            "results": results}
        return {"status": "completed", "results": results}
