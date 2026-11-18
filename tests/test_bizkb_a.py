from fastapi.testclient import TestClient
from rdflib import URIRef

from bizkb_a import BKA, COM, GOV, KnowledgeBase, Orchestrator
from bizkb_a.matchmaker import Matchmaker, match_degree
from bizkb_a.planner import ScriptedProposer
from bizkb_a.verifier import Step, Verifier

P = URIRef("https://ndta.gov.vn/person/test")
kb = KnowledgeBase()


def test_match_degrees():
    assert match_degree(kb, GOV.RegisterBirth, GOV.RegisterBirth) == "exact"
    assert match_degree(kb, GOV.IssueChildHealthInsurance, GOV.IssueHealthInsuranceCard) == "plugin"
    assert match_degree(kb, GOV.IssueHealthInsuranceCard, GOV.IssueChildHealthInsurance) == "subsumes"
    assert match_degree(kb, GOV.RegisterBirth, COM.ExecutePayment) == "fail"


def test_policy_gate_beats_score():
    best, rejected = Matchmaker(kb).bind(COM.ExecutePayment)
    assert best.agent == COM.agent_wallet
    assert any(r.agent == COM.agent_rogue_pay for r in rejected)


def test_shacl_catches_bad_binding_and_missing_mandate():
    steps = [Step(1, COM.ExecutePayment, COM.agent_rogue_pay, "exact", hitl=True)]
    ok, vs = Verifier(kb).validate("t1", steps)
    msgs = " ".join(v.message for v in vs)
    assert not ok and "N2" in msgs and "N4" in msgs and "N6" in msgs


def test_self_declared_license_is_not_enough():
    _, rejected = Matchmaker(kb).bind(COM.ExecutePayment)
    fake = next(r for r in rejected if r.agent == COM.agent_fake_licensed)
    assert any("chứng thực" in x for x in fake.reasons)


def test_hitl_required():
    ok, vs = Verifier(kb).validate("t2", [Step(1, COM.ObtainPaymentMandate, COM.agent_wallet, "exact", hitl=False)])
    assert not ok and any("N5" in v.message for v in vs)


def test_end_to_end_gov():
    rep = Orchestrator().orchestrate("đăng ký khai sinh cho bé", P)
    assert rep.status == "completed" and len(rep.steps) == 4
    assert "prov:actedOnBehalfOf" in rep.provenance_ttl


def test_llm_fallback_is_verified():
    prop = ScriptedProposer({COM.ReorderLastPurchase: [
        [COM.ExecutePayment],
        [COM.ObtainPaymentMandate, COM.ExecutePayment]]})
    rep = Orchestrator(proposer=prop).orchestrate("mua lại đơn hàng trước", P)
    assert rep.status == "completed"
    assert any("BÁC BỎ" in l for l in rep.log) and any("CHẤP NHẬN" in l for l in rep.log)


def test_ungrounded_rejected():
    assert Orchestrator().orchestrate("dự báo thời tiết", P).status == "rejected-ungrounded"


def test_api_defaults_to_no_auto_approval():
    from bizkb_a.api import app
    c = TestClient(app)
    base = {"text": "đặt mua sữa", "principal": str(P), "amount": 300000,
            "merchant": str(COM.merchant_A), "category": str(COM.Groceries)}
    r = c.post("/orchestrate", json=base).json()
    assert r["status"] == "halted"
    r2 = c.post("/orchestrate", json={**base, "auto_approve": True}).json()
    assert r2["status"] == "completed"
    assert c.get(f"/plans/{r2['plan_id']}/provenance").status_code == 200
    assert len(c.get("/agents").json()) >= 10


# ---------------- Agentic commerce ----------------
from datetime import datetime, timedelta, timezone  # noqa: E402

from bizkb_a.verifier import Mandate, PlanContext  # noqa: E402

NOW = datetime.now(timezone.utc)
MAND = Mandate(500_000, [COM.Groceries], NOW + timedelta(days=30))


def _run(text, ctx, approver=None):
    o = Orchestrator()
    return o.orchestrate(text, P, approver or (lambda s: (True, P)), True, ctx,
                         {"product": "x", "card_token": "t", "address": "a", "national_id": "n"})


def test_delegated_within_mandate_runs_without_hitl():
    rep = _run("tự động mua sữa", PlanContext(420_000, COM.merchant_A, COM.Groceries, MAND),
               approver=lambda s: (False, P))   # nếu có HITL sẽ bị từ chối
    assert rep.status == "completed" and not any(s["hitl"] for s in rep.steps)


def test_mandate_cap_category_expiry_missing():
    assert "C2" in " ".join(_run("tự động mua", PlanContext(900_000, COM.merchant_A, COM.Groceries, MAND)).violations)
    assert "C3" in " ".join(_run("tự động mua", PlanContext(100_000, COM.merchant_A, COM.Electronics, MAND)).violations)
    old = Mandate(500_000, [COM.Groceries], NOW - timedelta(days=1))
    assert "C4" in " ".join(_run("tự động mua", PlanContext(100_000, COM.merchant_A, COM.Groceries, old)).violations)
    assert "C7" in " ".join(_run("tự động mua", PlanContext(100_000, COM.merchant_A, COM.Groceries)).violations)


def test_unverified_merchant_and_prohibited_goods():
    assert "C1" in " ".join(_run("đặt mua", PlanContext(100_000, COM.merchant_B, COM.Groceries)).violations)
    assert "C5" in " ".join(_run("đặt mua", PlanContext(100_000, COM.merchant_A, COM.PrescriptionDrugs)).violations)


def test_cross_border_forces_hitl_even_with_mandate():
    rep = _run("tự động mua", PlanContext(100_000, COM.merchant_X, COM.Groceries, MAND), lambda s: (False, P))
    assert rep.status == "halted"


def test_data_minimisation_in_provenance():
    rep = _run("đặt mua", PlanContext(100_000, COM.merchant_A, COM.Groceries))
    assert "national_id" not in rep.provenance_ttl


def test_annotation_and_attestation():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from bizkb_a.annotator import annotate_agent_card
    from bizkb_a.attestation import AttestationError, IssuerRegistry, register_attested, sign
    k = KnowledgeBase()
    r = annotate_agent_card(k, {"name": "Pay X", "skills": [{"id": "p", "name": "thực hiện thanh toán"}]},
                            COM.CommerceDomain, "https://ex.vn/")
    assert r.coverage == 1.0 and "draft" in r.turtle
    key = Ed25519PrivateKey.generate(); reg = IssuerRegistry(); reg.add(COM.issuer_ecom, key.public_key())
    ttl = r.turtle.replace('"draft"', '"attested"')
    sig = sign(ttl, key)
    import pytest
    with pytest.raises(AttestationError):            # content tampering is caught
        register_attested(k, reg, ttl.replace('"attested"', '"attested", "x"'), COM.issuer_ecom, sig)
    assert register_attested(k, reg, ttl, COM.issuer_ecom, sig)


def test_canonical_signature_survives_reserialisation():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from rdflib import Graph
    from bizkb_a.attestation import AttestationError, IssuerRegistry, sign
    ttl = """@prefix bka: <https://ndta.gov.vn/ns/bizkb-a#> . @prefix com: <https://ndta.gov.vn/ns/acommerce#> .
    com:a a bka:Agent ; bka:provides com:ExecutePayment ; bka:trust 0.9 ; bka:hasAuthority com:PaymentLicense ."""
    key = Ed25519PrivateKey.generate(); reg = IssuerRegistry(); reg.add(COM.issuer_ecom, key.public_key())
    sig = sign(ttl, key)
    g = Graph().parse(data=ttl, format="turtle")
    reg.verify(COM.issuer_ecom, g.serialize(format="json-ld"), sig, fmt="json-ld")     # passes
    reg.verify(COM.issuer_ecom, g.serialize(format="nt"), sig, fmt="nt")               # passes
    import pytest
    with pytest.raises(AttestationError):                       # v0.4 byte signing breaks on JSON-LD
        reg.verify(COM.issuer_ecom, g.serialize(format="json-ld"), sign(ttl, key, canonical=False),
                   fmt="json-ld", canonical=False)
    with pytest.raises(AttestationError):                       # a changed value still fails
        reg.verify(COM.issuer_ecom, ttl.replace("0.9", "0.99"), sig)


# ---------------- live demo service ----------------
def test_live_service_end_to_end():
    from fastapi.testclient import TestClient
    from bizkb_a.live import app as live_app
    c = TestClient(live_app)
    assert c.get("/api/kb").json()["engine"] == "live"
    body = {"text": "đặt mua sữa", "amount": 300000, "merchant": "merchant_A", "category": "Groceries"}
    r = c.post("/api/orchestrate", json=body).json()
    assert r["status"] == "waiting" and r["viol"] == []
    assert any(x["agent"] == "agent_fake_licensed" for b in r["bind"] for x in b["rejected"])
    r = c.post(f"/api/plans/{r['pid']}/decision", json={"approve": True}).json()
    r = c.post(f"/api/plans/{r['pid']}/decision", json={"approve": True}).json()
    assert r["status"] == "completed" and "prov:actedOnBehalfOf" in r["prov"]
    assert all("national_id" not in e["sent"] for e in r["exec"])


def test_live_service_rejects_over_cap():
    from fastapi.testclient import TestClient
    from bizkb_a.live import app as live_app
    c = TestClient(live_app)
    r = c.post("/api/orchestrate", json={"text": "tự động mua sữa", "amount": 900000, "merchant": "merchant_A",
                                         "category": "Groceries",
                                         "mandate": {"cap": 500000, "days": 30, "cats": ["Groceries"]}}).json()
    assert r["status"] == "rejected" and any(v["c"] == "C2" for v in r["viol"])


# ---------------- runtime conformance monitor (v0.5) ----------------
def _deviant(kb, behaviour):
    """An attested, licensed wallet clone that wins the ranking but misbehaves at run time."""
    from rdflib import Literal
    a = COM[f"agent_wallet_{behaviour}"]
    for p, o in list(kb.g.predicate_objects(COM.agent_wallet)):
        kb.g.add((a, p, o))
    kb.g.set((a, BKA.trust, Literal(0.99)))
    kb.g.set((a, BKA.cost, Literal(0.05)))
    kb.g.add((a, BKA.simBehaviour, Literal(behaviour)))
    return a


def test_monitor_catches_overcharge_and_suspends():
    from bizkb_a.monitor import ConformanceMonitor
    kb = KnowledgeBase(); a = _deviant(kb, "overcharge"); mon = ConformanceMonitor(kb)
    o = Orchestrator(kb, monitor=mon)
    ctx = PlanContext(300_000, COM.merchant_A, COM.Groceries)
    r1 = o.orchestrate("đặt mua sữa", P, lambda s: (True, P), True, ctx, {"product": "x", "card_token": "t"})
    assert r1.status == "halted-nonconformant" and a in mon.suspensions
    assert "conformanceViolation" in r1.provenance_ttl
    r2 = o.orchestrate("đặt mua sữa", P, lambda s: (True, P), True, ctx, {"product": "x", "card_token": "t"})
    assert r2.status == "completed"                     # the deviant agent is no longer admitted
    assert all(s["agent"] != kb.qname(a) for s in r2.steps)


def test_probe_catches_deviant_before_service():
    from bizkb_a.executor import LocalAdapter
    from bizkb_a.monitor import ConformanceMonitor
    kb = KnowledgeBase(); a = _deviant(kb, "phantom"); mon = ConformanceMonitor(kb)
    assert mon.probe(a, LocalAdapter(), COM.ExecutePayment, {"amount": 1000.0, "card_token": "probe"})
    best, _ = Matchmaker(kb).bind(COM.ExecutePayment)
    assert best.agent == COM.agent_wallet
