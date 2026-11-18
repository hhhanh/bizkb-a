# BizKB-A — Reference implementation (v0.5)

Ontology-governed orchestration of LLM agent networks over MCP and A2A: closed-set grounding,
HTN planning with a norm-verified LLM fallback, admission gates on legal authority, data class and
descriptor attestation, whole-plan SHACL validation, PROV-O provenance, canonicalised descriptor
signatures, and runtime conformance monitoring.

Design principle: **the LLM proposes, the ontology disposes** — every LLM output passes a semantic
gate before it can influence execution.

This repository accompanies:

> H. H. Hoang et al., *Ontology-Governed Orchestration of LLM Agent Networks: Admission Control,
> Verified Planning and Provenance over MCP and A2A*. EAI AICON 2026, Springer LNICST (to appear).

It builds on BizKB (ICCCI 2009, LNCS 5796).

## Quick start

```bash
pip install -r requirements.txt
pytest -q tests                 # 20 tests
python demo.py                  # 13 scenarios (public services, agentic commerce, out of scope)
./run.sh                        # live demo UI + REST API on http://localhost:8080
```

## Reproducing the paper

| Paper item | Command | Output |
|---|---|---|
| Benchmark (300 requests, 50 agents) | `python -m bench.build && python -m bench.run` | `bench/results.md`, `bench/results.json` |
| Proposition 1 analysis, per-trap and per-language breakdowns, latency over all in-scope requests | `python -m bench.analysis` | `bench/analysis.json` |
| Validation cost vs. knowledge-base size | `python -c "from bench.run import latency_study; print(latency_study())"` | stdout |
| Out-of-distribution trap families F1–F8 | `python -m bench.ood` | `bench/ood.json` |

The benchmark generator is seeded (`SEED = 20260919`). Latency figures depend on hardware.

**About the LLM fallback in the benchmark.** The fallback branch in all benchmark runs is served by
`ScriptedProposer`, which replays three hand-written proposals representing typical failure modes
(an invented task, a missing authorisation step, a correct decomposition). No language model is
queried during the benchmark. A real model can be used in the demo service with
`ANTHROPIC_API_KEY=... BKA_USE_LLM=1`, or by implementing the `Proposer` protocol for a locally
hosted model.

## Layout

| Component | Path |
|---|---|
| Core, capability, delegation and monitoring vocabulary | `ontology/core.ttl` |
| Domain modules (public services, agentic commerce) | `ontology/domain_*.ttl` |
| Step norms N1–N7, commerce plan norms C1–C7 | `shapes/norms.ttl`, `shapes/norms_commerce.ttl` |
| Runtime effect shapes E1–E4 | `shapes/runtime/effects.ttl` |
| Grounding, HTN planner, gated matchmaker, SHACL verifier | `bizkb_a/router.py`, `planner.py`, `matchmaker.py`, `verifier.py` |
| Executor (local / A2A / MCP adapters), disclosure control | `bizkb_a/executor.py` |
| PROV-O recorder | `bizkb_a/provenance.py` |
| Descriptor attestation (Ed25519 over RDFC-1.0 canonical N-Quads) | `bizkb_a/attestation.py` |
| Runtime conformance monitor (effect checks, suspension, probes) | `bizkb_a/monitor.py` |
| Agent Card annotator | `bizkb_a/annotator.py` |
| REST API and live demo | `bizkb_a/api.py`, `bizkb_a/live.py`, `ui/index.html` |

Inline comments and log messages are partly in Vietnamese.

## Known limitations

- No revocation or rotation of issuer keys, and no check that an issuer is competent for the
  authority it attests (both demonstrated by out-of-distribution families F2 and F4).
- An attested agent that advertises a very general concept is admitted at plug-in degree for every
  task that needs no authority (family F3).
- Runtime monitoring is detective: a deviant act happens once before suspension unless an admission
  probe catches it first, and probes can be evaded by an agent that behaves differently when probed.
- Norms are illustrative models of legal rules, not rules in force.
- The benchmark is synthetic and authored by the developers.

## Licence

Code: Apache License 2.0 (`LICENSE`). Ontologies, shapes and benchmark data: CC BY 4.0
(`LICENSE-DATA`).

## Citation

See `CITATION.cff`. A Zenodo DOI is minted for each GitHub release.
