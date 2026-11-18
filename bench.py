"""Thí nghiệm đồ chơi (toy): BizKB-A vs baseline 'mù chính sách' + đo độ trễ lập kế hoạch.

Baseline: mỗi tác vụ chọn tác tử có (trust - cost) cao nhất trong số tác tử quảng bá năng lực khớp,
không cổng chính sách, không kiểm tra ngữ cảnh giao dịch. Hai hệ được kiểm bằng cùng bộ SHACL.
Đây là minh chứng khả thi, KHÔNG phải kết quả thực nghiệm để công bố.
"""
import statistics
import time
from datetime import datetime, timedelta, timezone

from rdflib import URIRef

from bizkb_a import COM, GOV, KnowledgeBase, Orchestrator
from bizkb_a.matchmaker import Matchmaker, match_degree
from bizkb_a.planner import HTNPlanner
from bizkb_a.verifier import Mandate, PlanContext, Verifier

kb = KnowledgeBase()
ver, mm = Verifier(kb), Matchmaker(kb)
planner = HTNPlanner(kb, ver)
M = Mandate(500_000, [COM.Groceries], datetime.now(timezone.utc) + timedelta(days=30))

CASES = [
    (GOV.BirthLifeEvent, None),
    (COM.PurchaseGoods, PlanContext(350_000, COM.merchant_A, COM.Groceries)),
    (COM.DelegatedPurchase, PlanContext(420_000, COM.merchant_A, COM.Groceries, M)),
    (COM.ReturnAndRefund, PlanContext(350_000, COM.merchant_A, COM.Groceries)),
]


def baseline_bind(task):
    cands = [(i["trust"] - i["cost"], a) for a in kb.agents() for i in [kb.agent_info(a)]
             if any(match_degree(kb, task, p) != "fail" for p in i["provides"])]
    return max(cands)[1] if cands else None


print(f"{'Goal':24} {'Hệ':9} {'Vi phạm':>7}  Chi tiết")
for goal, ctx in CASES:
    for name in ("baseline", "BizKB-A"):
        steps = planner.plan(goal, f"b-{name}", [], ctx)
        for s in steps:
            if name == "baseline":
                s.agent, s.degree = baseline_bind(s.task), "n/a"
            else:
                best, _ = mm.bind(s.task)
                s.agent, s.degree = best.agent, best.degree
        ok, vs = ver.validate(f"b-{name}-{goal[-8:]}", steps, ctx, goal)
        detail = "; ".join(sorted({v.message.split(':')[0] for v in vs}))
        print(f"{kb.qname(goal):24} {name:9} {len(vs):>7}  {detail}")

# Độ trễ lập kế hoạch (định tuyến + HTN + matchmaking + SHACL), không thực thi
orch = Orchestrator(kb)
P = URIRef("https://ndta.gov.vn/person/bench")
ts = []
for i in range(30):
    t0 = time.perf_counter()
    orch.orchestrate("tự động mua sữa", P, execute=False, context=CASES[2][1])
    ts.append((time.perf_counter() - t0) * 1000)
ts.sort()
print(f"\nĐộ trễ lập kế hoạch (n=30, KB ~{len(kb.g)} bộ ba, in-memory): "
      f"p50={statistics.median(ts):.0f} ms, p95={ts[int(0.95 * len(ts)) - 1]:.0f} ms")
