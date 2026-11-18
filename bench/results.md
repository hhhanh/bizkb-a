# Benchmark results (300 requests, 50 agents)

| System | Goal accuracy | Correct refusal | Binding precision | Violations/plan | Plans with a violation | Decision agreement | Correct norm cited | Binding-induced viol./plan | LLM calls |
|---|---|---|---|---|---|---|---|---|---|
| BizKB-A | 97.6% | 100.0% | 100.0% | 0.41 | 35.5% | 100.0% | 100.0% | 0.00 | 33 |
| B3 no LLM fallback | 97.6% | 100.0% | 100.0% | 0.43 | 37.2% | 95.7% | 89.7% | 0.00 | 0 |
| B4 weighted policy | 97.6% | 100.0% | 100.0% | 0.41 | 35.5% | 100.0% | 100.0% | 0.00 | 33 |
| B2′ similarity router | 96.1% | 0.0% | 81.4% | 2.29 | 92.0% | 43.5% | 100.0% | 1.84 | 57 |
| B0 policy-blind | 96.1% | 0.0% | 13.1% | 2.08 | 100.0% | 38.0% | 100.0% | 1.63 | 57 |

w5 sweep (b4 ablation): {"0.00": {"admissible_bindings": 0, "of": 4}, "0.01": {"admissible_bindings": 0, "of": 4}, "0.02": {"admissible_bindings": 0, "of": 4}, "0.05": {"admissible_bindings": 3, "of": 4}, "0.10": {"admissible_bindings": 4, "of": 4}, "0.25": {"admissible_bindings": 4, "of": 4}}

Grounding errors (BizKB-A): [{"text": "same order again", "gold": "ReorderLastPurchase", "got": "PurchaseGoods"}, {"text": "same order again", "gold": "ReorderLastPurchase", "got": "PurchaseGoods"}, {"text": "reorder my previous purchase", "gold": "ReorderLastPurchase", "got": "PurchaseGoods"}, {"text": "same order again", "gold": "ReorderLastPurchase", "got": "PurchaseGoods"}, {"text": "reorder my previous purchase", "gold": "ReorderLastPurchase", "got": "PurchaseGoods"}, {"text": "reorder my previous purchase", "gold": "ReorderLastPurchase", "got": "PurchaseGoods"}]

Pipeline latency (n=36): p50 180.8 ms, p95 257.5 ms.

| Validation | KB triples | full p50/p95 (ms) | scoped p50/p95 (ms) |
|---|---|---|---|
| base | 1522 | 203.5/286.4 | 186.8/212.1 |
| inflated | 51522 | 1463.1/1576.2 | 186.0/192.8 |
