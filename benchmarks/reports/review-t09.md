# T-09 — Self review pass

> **Historical review.** This pass missed the BM25 direction and rerank-score defects later found in independent v1.1 review. The corrected implementation and new reports supersede its affected conclusions.

Re-read of the implementation vs paper sections 2–4 and own engineering
plan. Notes on what was checked, what changed, and what was intentionally
left alone.

## Correctness (engine vs paper)

| Area | Check | Result |
|---|---|---|
| Lexical ranking | `bm25()` negated, ascending order = descending relevance | OK; smoke test shows relevant doc first |
| Lexical tie-break | `ORDER BY score, doc_id` | OK; deterministic |
| BM25 host]ile input | sanitizer quotes every token, FTS5 syntax operators neutralized | OK; tested with `AND`, unbalanced quotes |
| Hybrid RRF | Cormack et al. 2009, k=60, deterministic `(-score, doc_id)` | OK; pure-Python unit test asserts exact scores |
| Hybrid leg depth | each leg returns k, fused top-k | OK; documented in ADR-8 |
| Rerank scope | fused pool (≤2k) rescored by full-precision cosine | OK; result count stable |
| Semantic int8 math | global symmetric scale, cosine on quantized ints | OK; unit test asserts cosine preserved within 5e-3 |
| Binary packing | sign-bit LSB-first, pad to byte boundary | OK; known example verifies bit pattern |
| Eval determinism | re-ran mini-BEIR twice, effectiveness rows identical | OK |
| Eval metric names | ranx spells `precision@10`, not `p@10` | fixed in code |

## Performance (real runs on this CPU)

- SciFact 5,183 docs · 300 queries · 8 configs: p95 latency in [2ms..276ms]
  (lexical <5ms; semantic/hybrid 130-220ms including query encoding;
  rerank 270ms).
- NFCorpus 3,633 docs · 323 queries · same envelope.
- These are end-to-end with the model loaded warm; model loading is the
  dominant one-time cost (~2-3s on this CPU).

## What I found and fixed mid-review

- `cmd_eval` originally passed `args.model` (default `None`) explicitly,
  overriding the `DEFAULT_MODEL` default in `run_eval`. Fixed by
  normalizing in `run_eval` to `model_name or DEFAULT_MODEL`.
- My first eval test fixture authored a self-inconsistent qrels
  (judged *vitamin C* against the *Photosynthesis* document), exposed
  by the sanity assertion that lexical cannot score 0.0000 on a
  coherent fixture.

## What I did NOT change (and why)

- **Hybrid fetch depth = k (not deeper):** kept per ADR-8 to keep V1
  simple; the T-10 alpha sweep is the revisit trigger.
- **Latency includes query encoding:** honest end-to-end measure.
  Pre-encoding all queries across configs would change the definition
  and hide the real per-query cost.
- **sqlite-vec version pin:** not added because ADR-7 records the
  limitation and the app-side quantization path is a feature, not a
  workaround to hide.

## T-10 trigger fired (ADR-8 revisit)

The engineering plan's T-10 extension list includes "weighted α-sweep vs
RRF — does tuned linear beat RRF anywhere?"; with the Phase-4 numbers in
hand, this is the natural next experiment. See `evaluate.run_alpha_sweep`.
