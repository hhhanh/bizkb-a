"""Kịch bản minh họa BizKB-A v0.2. Không cần API key (ScriptedProposer mô phỏng LLM)."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from rdflib import URIRef

from bizkb_a import COM, Orchestrator
from bizkb_a.annotator import annotate_agent_card
from bizkb_a.attestation import AttestationError, IssuerRegistry, register_attested, sign
from bizkb_a.planner import ScriptedProposer
from bizkb_a.verifier import Mandate, PlanContext

CITIZEN = URIRef("https://ndta.gov.vn/person/cong-dan-A")
BUYER = URIRef("https://ndta.gov.vn/person/nguoi-mua-B")
NOW = datetime.now(timezone.utc)
GROCERY_MANDATE = Mandate(500_000, [COM.Groceries], NOW + timedelta(days=30))
BUYER_DATA = {"product": "sữa công thức", "quantity": 2, "full_name": "Nguyễn Văn B", "address": "Hà Nội",
              "phone": "09xxxxxxx", "card_token": "tok_***", "national_id": "0xx...", "tax_code": "01xx", "order_ref": "DH-001"}

proposer = ScriptedProposer({COM.ReorderLastPurchase: [
    [COM.RetrieveOrderHistory, COM.QuickPay, COM.ArrangeDelivery],
    [COM.RetrieveOrderHistory, COM.ExecutePayment, COM.ArrangeDelivery],
    [COM.RetrieveOrderHistory, COM.VerifyMerchant, COM.ObtainPaymentMandate, COM.ExecutePayment,
     COM.ArrangeDelivery, COM.IssueEInvoice]]})
orch = Orchestrator(proposer=proposer)
deny = lambda s: (False, BUYER)
approve = lambda s: (True, BUYER)


def ctx(amount, merchant=COM.merchant_A, cat=COM.Groceries, mandate=None):
    return PlanContext(amount, merchant, cat, mandate)


SCENARIOS = [
    # (mã, mô tả, yêu cầu, chủ thể, context, approver, verbose)
    ("G1", "GovTech – liên thông khai sinh", "Tôi muốn đăng ký khai sinh cho con", CITIZEN, None, approve, True),
    ("A1", "Mua có mặt người dùng (human-present)", "Đặt mua 2 hộp sữa công thức", BUYER,
     ctx(350_000), approve, True),
    ("A2", "Mua tự động trong hạn mức ủy quyền (human-not-present)", "Tự động mua sữa khi giá dưới 450 nghìn", BUYER,
     ctx(420_000, mandate=GROCERY_MANDATE), approve, True),
    ("A3", "Mua tự động VƯỢT hạn mức", "Tự động mua sữa khi có hàng", BUYER,
     ctx(800_000, mandate=GROCERY_MANDATE), approve, False),
    ("A4", "Mua tự động SAI danh mục ủy quyền", "Tự động mua tai nghe khi giảm giá", BUYER,
     ctx(300_000, cat=COM.Electronics, mandate=GROCERY_MANDATE), approve, False),
    ("A5", "Mua tự động KHÔNG có ủy quyền", "Tự động mua sữa khi giá tốt", BUYER, ctx(300_000), approve, False),
    ("A6", "Ủy quyền đã HẾT HẠN", "Tự động mua sữa khi giá tốt", BUYER,
     ctx(300_000, mandate=Mandate(500_000, [COM.Groceries], NOW - timedelta(days=1))), approve, False),
    ("A7", "Người bán CHƯA đăng ký TMĐT", "Đặt mua nồi cơm", BUYER, ctx(600_000, COM.merchant_B), approve, False),
    ("A8", "Hàng CẤM giao dịch qua tác tử", "Đặt mua thuốc kháng sinh", BUYER,
     ctx(200_000, cat=COM.PrescriptionDrugs), approve, False),
    ("A9", "Xuyên biên giới dưới ủy quyền → bắt buộc HITL, người dùng từ chối", "Tự động mua sữa nhập khẩu", BUYER,
     ctx(400_000, COM.merchant_X, mandate=GROCERY_MANDATE), deny, True),
    ("A10", "Trả hàng và hoàn tiền", "Tôi muốn trả hàng đơn vừa rồi", BUYER, ctx(350_000), approve, True),
    ("A11", "Goal chưa có phương thức → LLM dự phòng có kiểm chứng", "Mua lại đơn hàng trước", BUYER,
     ctx(350_000), approve, True),
    ("X1", "Ngoài phạm vi ontology", "Xin cấp giấy phép xây dựng", CITIZEN, None, approve, True),
]

Path("out").mkdir(exist_ok=True)
summary = []
for code, title, text, who, c, appr, verbose in SCENARIOS:
    print(f"\n=== {code} {title} ===\nYêu cầu: {text}")
    rep = orch.orchestrate(text, who, appr, True, c, BUYER_DATA if who == BUYER else {"citizen_request": text})
    lines = rep.log if verbose else [l for l in rep.log if l.startswith(("Định tuyến", "Kiểm chứng"))]
    for line in lines:
        print(" ", line)
    print(f"  → TRẠNG THÁI: {rep.status}")
    summary.append((code, title, rep.status, "; ".join(rep.violations)))
    if rep.provenance_ttl:
        (Path("out") / f"prov_{code}.ttl").write_text(rep.provenance_ttl, encoding="utf-8")

# ---- Giảm chi phí chú giải + chống descriptor tự khai ----
print("\n=== R1 Chú giải bán tự động Agent Card → descriptor nháp ===")
card = {"name": "FastPay Wallet", "url": "https://agents.example.vn/fastpay",
        "skills": [{"id": "pay", "name": "Thực hiện thanh toán", "tags": ["payment"]},
                   {"id": "refund", "name": "Hoàn tiền", "tags": ["refund"]},
                   {"id": "loyalty", "name": "Tích điểm thành viên", "tags": ["loyalty"]}]}
r = annotate_agent_card(orch.kb, card, COM.CommerceDomain, "https://ndta.gov.vn/agent/")
print(f"  Tự ánh xạ {len(r.mapped)}/{len(r.mapped)+len(r.unmapped)} skill ({r.coverage:.0%}): {r.mapped}")
print(f"  Cần con người: {r.needs_human}")

print("\n=== R2 Đăng ký descriptor có chữ ký của tổ chức tin cậy ===")
issuer_key = Ed25519PrivateKey.generate()
reg = IssuerRegistry()
reg.add(COM.issuer_ecom, issuer_key.public_key())
confirmed = r.turtle + f"""
<{r.agent}> <https://ndta.gov.vn/ns/bizkb-a#hasAuthority> <{COM.PaymentLicense}> ;
  <https://ndta.gov.vn/ns/bizkb-a#handlesDataClass> <https://ndta.gov.vn/ns/bizkb-a#FinancialData> ;
  <https://ndta.gov.vn/ns/bizkb-a#trust> 0.9 ; <https://ndta.gov.vn/ns/bizkb-a#cost> 0.3 .
"""
confirmed = confirmed.replace('"draft"', '"attested"')
sig = sign(confirmed, issuer_key)
try:
    register_attested(orch.kb, reg, confirmed.replace("0.9", "0.99"), COM.issuer_ecom, sig)
except AttestationError as e:
    print(f"  Descriptor bị sửa sau khi ký → TỪ CHỐI: {e}")
added = register_attested(orch.kb, reg, confirmed, COM.issuer_ecom, sig)
print(f"  Chữ ký hợp lệ → đã đăng ký {[orch.kb.qname(a) for a in added]}")

print("\n=== TỔNG HỢP ===")
for code, title, status, v in summary:
    print(f"  {code:4} {status:22} {title}" + (f"  | {v}" if v else ""))
