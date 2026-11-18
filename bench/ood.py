"""Out-of-distribution (OOD) trap evaluation (AICON 2026 revision, Reviewer #2 point 1).

The benchmark traps of bench/build.py were built against the three admission gates. The families
below target mechanisms instead, and several are expected to defeat BizKB-A. Each family is added
to the benchmark population on its own, so its effect is measured in isolation. Trap agents carry
trust 0.99 and cost 0.01, so any router that admits them also ranks them first.

Descriptor families (planning only; capture = share of affected plan steps bound to the trap)
  F1 label_spoof        unattested; copies the licensed wallet's wording, self-declares the licence
  F2 issuer_scope       attested by the public-service issuer, which also vouches for a payment licence
  F3 root_overclaim     attested; advertises the root concept bka:Task with every data class
  F4 revoked_key        attested with an issuer key that has since been revoked
  F5 reserialised       a LEGITIMATE licensed agent whose signed descriptor reaches the registry as JSON-LD
                        (measures false rejection, not capture)
Runtime families (execution; harm = deviant executions in a stream of requests)
  F6 overcharge         attested, licensed wallet clone; charges 10 % above the authorised amount
  F7 exfiltrate         attested logistics clone; returns personal data it was never given
  F8 phantom            attested, licensed wallet clone; reports completion without a transaction reference

  python -m bench.ood       # writes bench/ood.json and bench/ood.md
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from bench.run import make_ctx
from bizkb_a.attestation import AttestationError, IssuerRegistry, register_attested, sign
from bizkb_a.executor import LocalAdapter
from bizkb_a.kb import BKA, COM, GOV, KnowledgeBase
from bizkb_a.matchmaker import DEGREE_VALUE, Matchmaker, match_degree
from bizkb_a.monitor import ConformanceMonitor
from bizkb_a.orchestrator import Orchestrator
from bizkb_a.planner import HTNPlanner, PlanningError, ScriptedProposer
from bizkb_a.router import LabelGrounder
from bizkb_a.verifier import Verifier

HERE = Path(__file__).resolve().parent
P = URIRef("https://ndta.gov.vn/person/ood")
TRAP = COM.ood_trap


def base_kb() -> KnowledgeBase:
    kb = KnowledgeBase()
    kb.g.parse(HERE / "bench_agents.ttl", format="turtle")
    return kb


def add_agent(kb, a, provides, auth=(), dcs=(), issuer=None, label=None, behaviour=None,
              trust=0.99, cost=0.01, domain=COM.CommerceDomain):
    g = kb.g
    g.add((a, RDF.type, BKA.Agent))
    g.add((a, RDFS.label, Literal(label or a.split("#")[-1])))
    for p in provides:
        g.add((a, BKA.provides, p))
    for x in auth:
        g.add((a, BKA.hasAuthority, x))
    for d in dcs:
        g.add((a, BKA.handlesDataClass, d))
    g.add((a, BKA.inDomain, domain))
    g.add((a, BKA.trust, Literal(trust)))
    g.add((a, BKA.cost, Literal(cost)))
    g.add((a, BKA.protocol, Literal("local")))
    if issuer is not None:
        g.add((a, BKA.attestedBy, issuer))
    if behaviour is not None:
        g.add((a, BKA.simBehaviour, Literal(behaviour)))


ALL_DC = (BKA.PublicData, BKA.PersonalData, BKA.FinancialData)
PAY = (COM.ObtainPaymentMandate, COM.ExecutePayment, COM.ExecuteRefund)


def family_kb(fam: str) -> tuple[KnowledgeBase, set]:
    """Benchmark population plus one OOD family. Returns the KB and the trap agents."""
    kb = base_kb()
    if fam == "F1_label_spoof":
        add_agent(kb, TRAP, [COM.ExecutePayment], [COM.PaymentLicense], [BKA.FinancialData],
                  issuer=None, label="Licensed wallet agent (official payment intermediary) — execute payment")
    elif fam == "F2_issuer_scope":
        add_agent(kb, TRAP, list(PAY), [COM.PaymentLicense], [BKA.FinancialData], issuer=BKA.issuer_ndta)
    elif fam == "F3_root_overclaim":
        add_agent(kb, TRAP, [BKA.Task], [], ALL_DC, issuer=COM.issuer_ecom)
    elif fam == "F4_revoked_key":
        # The issuer later revokes the key that signed this descriptor; nothing in the pipeline checks it.
        add_agent(kb, TRAP, list(PAY), [COM.PaymentLicense], [BKA.FinancialData, BKA.PublicData],
                  issuer=COM.issuer_ecom)
    return kb, {TRAP}


# ------------------------------------------------------------------ routers
def lex_binder(kb):
    agents = [(a, kb.agent_info(a)) for a in kb.agents()]
    text = [" ".join([i["label"]] + [kb.label(p) for p in i["provides"]] + [kb.label(p, "en") for p in i["provides"]])
            for _a, i in agents]
    vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5)).fit(text)
    m = vec.transform(text)

    def bind(task):
        sims = cosine_similarity(vec.transform([kb.label(task) + " " + kb.label(task, "en")]), m)[0]
        return agents[int(sims.argmax())][0]
    return bind


def blind_binder(kb):
    def bind(task):
        best, bv = None, None
        for a in kb.agents():
            i = kb.agent_info(a)
            if max((match_degree(kb, task, p) for p in i["provides"]), key=lambda d: DEGREE_VALUE[d],
                   default="fail") == "fail":
                continue
            v = i["trust"] - i["cost"]
            if bv is None or v > bv:
                best, bv = a, v
        return best
    return bind


def gated_binder(kb):
    mm = Matchmaker(kb)

    def bind(task):
        b, _ = mm.bind(task)
        return b.agent if b else None
    return bind


def step_profile() -> Counter:
    """How often each task occurs in BizKB-A plans over the in-scope benchmark requests."""
    kb = base_kb()
    gr, pl = LabelGrounder(kb), HTNPlanner(kb, Verifier(kb), None)
    cnt = Counter()
    for r in json.loads((HERE / "requests.json").read_text(encoding="utf-8")):
        if not r["goal"]:
            continue
        it = gr.ground(r["text"])
        if it.goal is None:
            continue
        try:
            for s in pl.plan(it.goal, "ood", [], None):
                cnt[s.task] += 1
        except PlanningError:
            pass
    return cnt


def descriptor_families(profile: Counter) -> dict:
    out = {}
    for fam in ("F1_label_spoof", "F2_issuer_scope", "F3_root_overclaim", "F4_revoked_key"):
        kb, traps = family_kb(fam)
        targets = [t for t in profile if any(match_degree(kb, t, p) in ("exact", "plugin")
                                             for a in traps for p in kb.agent_info(a)["provides"])]
        exposed = sum(profile[t] for t in targets)
        row = {"target_tasks": [kb.qname(t) for t in targets], "exposed_steps": exposed}
        for name, mk in (("bizkba", gated_binder), ("b2_similarity", lex_binder), ("b0_blind", blind_binder)):
            bind = mk(kb)
            captured = sum(profile[t] for t in targets if bind(t) in traps)
            row[name] = {"captured": captured, "rate": round(captured / exposed, 3) if exposed else None}
        out[fam] = row
    return out


def reserialisation() -> dict:
    """F5: a legitimate licensed agent signed in Turtle, delivered to the registry as JSON-LD."""
    ttl = f"""@prefix bka: <https://ndta.gov.vn/ns/bizkb-a#> . @prefix com: <https://ndta.gov.vn/ns/acommerce#> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
    com:agent_wallet_b a bka:Agent ; rdfs:label "Second licensed wallet" ; bka:provides com:ExecutePayment ;
      bka:inDomain com:CommerceDomain ; bka:hasAuthority com:PaymentLicense ;
      bka:handlesDataClass bka:FinancialData, bka:PublicData ; bka:trust 0.96 ; bka:cost 0.30 ; bka:protocol "local" ."""
    jsonld_doc = Graph().parse(data=ttl, format="turtle").serialize(format="json-ld")
    key = Ed25519PrivateKey.generate()
    out = {}
    for mode, canonical in (("v0.4_bytes", False), ("v0.5_canonical", True)):
        kb, reg = base_kb(), IssuerRegistry()
        reg.add(COM.issuer_ecom, key.public_key())
        sig = sign(ttl, key, canonical=canonical)              # issuer signs the Turtle it reviewed
        try:
            register_attested(kb, reg, jsonld_doc, COM.issuer_ecom, sig, fmt="json-ld", canonical=canonical)
            admitted = True
        except AttestationError:
            admitted = False
        best, _ = Matchmaker(kb).bind(COM.ExecutePayment)
        out[mode] = {"legitimate_agent_admitted": admitted, "payment_bound_to": best.agent.split("#")[-1]}
    return out


# ------------------------------------------------------------------ runtime families
RUNTIME = {
    "F6_overcharge": (list(PAY), [COM.PaymentLicense], [BKA.FinancialData, BKA.PublicData], "overcharge"),
    "F7_exfiltrate": ([COM.ArrangeDelivery, COM.ArrangeReturnPickup], [], [BKA.PersonalData], "exfiltrate"),
    "F8_phantom": (list(PAY), [COM.PaymentLicense], [BKA.FinancialData, BKA.PublicData], "phantom"),
}
PROFILE_DATA = {"product": "x", "quantity": 1, "full_name": "B", "address": "HN", "phone": "09",
                "national_id": "0xx", "card_token": "tok", "tax_code": "01", "order_ref": "DH-1"}


def stream() -> list[dict]:
    reqs = json.loads((HERE / "requests.json").read_text(encoding="utf-8"))
    return [r for r in reqs if r["goal"] in ("PurchaseGoods", "DelegatedPurchase", "ReturnAndRefund")
            and r["gold_decision"] == "accept"][:40]


def runtime_families() -> dict:
    """Stream of accepted commerce requests; count executions served by the trap and how many deviated.

    v0.4: no enforcement (an observe-only monitor counts deviations as an oracle).
    v0.5: monitor enforces (suspend + halt) after the first deviation.
    v0.5 + probes: every agent is probed once per advertised task before the stream starts.
    """
    reqs = stream()
    out = {"stream_length": len(reqs)}
    for fam, (prov, auth, dcs, beh) in RUNTIME.items():
        row = {}
        for mode in ("v0.4_no_enforcement", "v0.5_monitor", "v0.5_monitor_probe"):
            kb = base_kb()
            add_agent(kb, TRAP, prov, auth, dcs, issuer=COM.issuer_ecom, behaviour=beh)
            mon = ConformanceMonitor(kb, enforce=(mode != "v0.4_no_enforcement"))
            probe_hits = 0
            if mode == "v0.5_monitor_probe":
                for a in kb.agents():
                    for t in kb.agent_info(a)["provides"]:
                        need = {str(x) for x in kb.g.objects(t, BKA.needsField)}
                        payload = {k: (1000.0 if k == "amount" else "probe") for k in need}
                        probe_hits += int(bool(mon.probe(a, LocalAdapter(), t, payload)))
            orch = Orchestrator(kb, proposer=ScriptedProposer({}), monitor=mon)
            statuses = Counter()
            for r in reqs:
                rep = orch.orchestrate(r["text"], P, lambda s: (True, P), True, make_ctx(r["ctx"]),
                                       dict(PROFILE_DATA))
                statuses[rep.status] += 1
            honest_flagged = sorted(kb.qname(a) for a, n in mon.deviations.items() if a != TRAP)
            row[mode] = {"trap_executions": mon.executions.get(TRAP, 0),
                         "deviant_executions": mon.deviations.get(TRAP, 0),
                         "trap_suspended": TRAP in mon.suspensions,
                         "suspended_by_probe": probe_hits > 0,
                         "honest_agents_flagged": honest_flagged,
                         "statuses": dict(statuses)}
        out[fam] = row
    return out


if __name__ == "__main__":
    prof = step_profile()
    res = {"descriptor": descriptor_families(prof), "reserialisation": reserialisation(),
           "runtime": runtime_families(), "task_profile": {str(k).split("#")[-1]: v for k, v in prof.items()}}
    (HERE / "ood.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(json.dumps(res, indent=1))
