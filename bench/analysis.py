"""Supplementary analyses requested in review (AICON 2026, Reviewer #2 points 3-5).

  python -m bench.analysis        # writes bench/analysis.json and bench/analysis.md

1. Proposition 1: worst-case bound Delta vs the empirical threshold of the benchmark population,
   and a constructed population in which the weighted variant at w5 = 0.25 selects a trap.
2. Results broken down by trap type (which gate the trap fails) and by request language.
3. Planning latency over all in-scope requests, curated path and fallback path reported separately.
   The fallback proposer is scripted (hand-written proposals replayed), so fallback latency
   excludes model inference time.
"""
from __future__ import annotations

import json
import statistics
import time
from collections import defaultdict
from pathlib import Path

from rdflib import Literal
from rdflib.namespace import RDF, RDFS

from bench import run as R
from bizkb_a.kb import BKA, GOV, KnowledgeBase
from bizkb_a.matchmaker import Matchmaker
from bizkb_a.planner import HTNPlanner, PlanningError, ScriptedProposer

HERE = Path(__file__).resolve().parent
VI_CHARS = set("ăâđêôơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹáàãéèíìóòõúùý")


def lang(text: str) -> str:
    return "vi" if any(c in VI_CHARS for c in text.lower()) else "en"


# ------------------------------------------------------------------ 1. Proposition 1
def proposition_one() -> dict:
    w = R.matchmaker.w
    delta = w[0] * (1.0 - 0.8) + w[1] + w[2] + w[3] / 2
    # empirical threshold: largest score advantage of a gate-failing candidate over the best
    # admissible one, per task, in the benchmark population
    worst, arg = 0.0, None
    for t in {p for a in R.kb.agents() for p in R.kb.agent_info(a)["provides"]}:
        c = R.matchmaker.candidates(t)
        ok = [x.score for x in c if x.gate_ok]
        bad = [x.score for x in c if not x.gate_ok and x.degree in ("exact", "plugin")]
        if ok and bad and max(bad) - max(ok) > worst:
            worst, arg = max(bad) - max(ok), R.kb.qname(t)

    # constructed population: one compliant provider at plug-in degree with moderate trust/cost,
    # one unlicensed exact-match provider with high trust/low cost (attested, so only N2 fails)
    kb = KnowledgeBase()
    task = GOV.IssueChildHealthInsurance
    for a in list(kb.agents()):                       # keep only the two constructed agents
        kb.g.remove((a, None, None))
    comp, trap = GOV.cx_compliant, GOV.cx_trap
    for a, prov, auth, tau, cost in ((comp, GOV.IssueHealthInsuranceCard, GOV.SocialInsuranceAuthority, .60, .60),
                                     (trap, task, None, .99, .01)):
        kb.g.add((a, RDF.type, BKA.Agent))
        kb.g.add((a, RDFS.label, Literal(a.split("#")[-1])))
        kb.g.add((a, BKA.provides, prov))
        kb.g.add((a, BKA.inDomain, GOV.GovDomain))
        kb.g.add((a, BKA.handlesDataClass, BKA.PersonalData))
        kb.g.add((a, BKA.trust, Literal(tau)))
        kb.g.add((a, BKA.cost, Literal(cost)))
        kb.g.add((a, BKA.attestedBy, BKA.issuer_ndta))
        if auth is not None:
            kb.g.add((a, BKA.hasAuthority, auth))
    mm = Matchmaker(kb)
    cands = {c.agent: c for c in mm.candidates(task)}
    gated, _ = mm.bind(task)
    weighted = {}
    for w5 in (0.10, 0.25, 0.26, 0.30):
        best = max(cands.values(), key=lambda c: c.score + (w5 if c.gate_ok else 0.0))
        weighted[f"{w5:.2f}"] = best.agent.split("#")[-1]
    return {"delta": round(delta, 3), "empirical_threshold": round(worst, 3), "set_by": arg,
            "constructed": {"compliant_score": cands[comp].score, "trap_score": cands[trap].score,
                            "gap": round(cands[trap].score - cands[comp].score, 3),
                            "gated_choice": gated.agent.split("#")[-1], "weighted_choice": weighted}}


# ------------------------------------------------------------------ 2. Breakdowns
def trap_type(agent) -> str | None:
    """Classify a gate-failing agent by the first gate it fails, in gate order."""
    info = R.kb.agent_info(agent)
    if not info["attested"]:
        return "unattested"
    for t in info["provides"]:
        auth = R.kb.g.value(t, BKA.requiresAuthority)
        if auth is not None and auth not in info["authorities"]:
            return "no_authority"
        dc = R.kb.g.value(t, BKA.dataClass)
        if dc is not None and dc not in info["data_classes"]:
            return "wrong_data_class"
    return None


def breakdowns() -> dict:
    reqs = json.loads((HERE / "requests.json").read_text(encoding="utf-8"))
    planners = {s: HTNPlanner(R.kb, R.verifier, None if s == "b3_nofb" else ScriptedProposer({}))
                for s in ("bizkba", "b2_sim", "b0_blind")}
    sysmap = {"bizkba": "bizkba", "b2_sim": "b1_lex", "b0_blind": "b0_blind"}
    traps_for_task = defaultdict(lambda: defaultdict(set))
    for a in R.kb.agents():
        tt = trap_type(a)
        if tt:
            for t in R.kb.agent_info(a)["provides"]:
                traps_for_task[t][tt].add(a)
                for sub in R.kb.g.transitive_subjects(RDFS.subClassOf, t):
                    traps_for_task[sub][tt].add(a)
    by_trap = {s: defaultdict(lambda: [0, 0]) for s in planners}       # [captured, exposed]
    by_lang = {s: defaultdict(lambda: defaultdict(int)) for s in planners}
    for req in reqs:
        if req["goal"] is None:
            continue
        lg = lang(req["text"])
        for s, planner in planners.items():
            r = R.run_one(sysmap[s], req, planner)
            m = by_lang[s][lg]
            m["n"] += 1
            m["goal_ok"] += int(r["goal"] is not None and str(r["goal"]).endswith("#" + req["goal"]))
            m["decision_ok"] += int(r["decision"] == req["gold_decision"])
            for task, agent, _h in r["steps"]:
                m["steps"] += 1
                m["bind_ok"] += int(agent in R.gold_admissible(task))
                for tt, agents in traps_for_task.get(task, {}).items():
                    by_trap[s][tt][1] += 1
                    by_trap[s][tt][0] += int(agent in agents)
    return {"by_trap": {s: {k: {"captured": v[0], "exposed": v[1]} for k, v in d.items()}
                        for s, d in by_trap.items()},
            "by_lang": {s: {k: dict(v) for k, v in d.items()} for s, d in by_lang.items()}}


# ------------------------------------------------------------------ 3. Latency
def latency() -> dict:
    reqs = [r for r in json.loads((HERE / "requests.json").read_text(encoding="utf-8")) if r["goal"]]
    planner = HTNPlanner(R.kb, R.verifier, ScriptedProposer({}))
    paths = {"curated": [], "fallback": []}
    outcomes = defaultdict(int)
    for req in reqs:
        R.reset_fallback(planner)
        ctx = R.make_ctx(req["ctx"])
        t0 = time.perf_counter()
        intent = R.grounder.ground(req["text"])
        path, outcome = "curated", "ok"
        if intent.goal is None:
            outcome = "ungrounded"
        else:
            c = ctx if R.kb.domain_of(intent.goal) == R.COM.CommerceDomain else None
            path = "curated" if R.kb.methods_for(intent.goal) else "fallback"
            try:
                steps = planner.plan(intent.goal, "lat", [], c)
                for s in steps:
                    b, _ = R.matchmaker.bind(s.task)
                    if b is None:
                        outcome = "unbindable"
                        break
                    s.agent, s.degree = b.agent, b.degree
                if outcome == "ok":
                    ok, _v = R.verifier.validate("lat", steps, c, intent.goal, scoped=True)
                    outcome = "accepted" if ok else "rejected"
            except PlanningError:
                outcome = "rejected_in_planning"
        paths[path].append((time.perf_counter() - t0) * 1000)
        outcomes[outcome] += 1

    def summ(xs):
        xs = sorted(xs)
        return {"n": len(xs), "p50": round(statistics.median(xs), 1),
                "p95": round(xs[max(0, int(.95 * len(xs)) - 1)], 1), "max": round(xs[-1], 1)}
    allx = paths["curated"] + paths["fallback"]
    return {"all": summ(allx), "curated": summ(paths["curated"]), "fallback": summ(paths["fallback"]),
            "outcomes": dict(outcomes)}


if __name__ == "__main__":
    t0 = time.time()
    out = {"proposition_1": proposition_one()}
    print(json.dumps(out["proposition_1"], indent=1))
    out["latency"] = latency()
    print(json.dumps(out["latency"], indent=1))
    out["breakdowns"] = breakdowns()
    (HERE / "analysis.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    print(json.dumps(out["breakdowns"], indent=1))
    print(f"elapsed {time.time() - t0:.0f}s")
