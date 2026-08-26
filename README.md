# scrydb — Reproducing "SQLite is Enough"

Reproduction of **[SQLite is Enough. Lexical, Semantic, and Hybrid Search with scrydb](https://arxiv.org/abs/2608.24060)** (arXiv:2608.24060, cs.IR) — a lightweight hybrid search engine built entirely on SQLite: FTS5 (BM25) + sqlite-vec (embeddings) + Reciprocal Rank Fusion.

> Portfolio project: read literature → implement from scratch → reproduce reported behavior → extend → publish.

## What this repo contains

| Path | Purpose |
|---|---|
| `docs/01-prd.md` … `docs/07-references.md` | Six-document spec system (source of truth, versioned with code) |
| `src/scrydb/` | The library implementation |
| `tests/` | pytest suite (unit + integration) |
| `benchmarks/` | BEIR evaluation harness + generated reports |
| `demo/` | Thin web demo (later phase) |

## Quickstart

```bash
pip install -e ".[model]"        # model extra optional until Phase 2
scrydb check                      # prove FTS5 + sqlite-vec (+ add --full to load the model)
scrydb index ./sample-data --db demo.db
scrydb search "chlorophyll" --mode lexical --db demo.db   # hybrid arrives in Phase 3
```

## Results (vs paper)

Reproduced on CPU with `all-MiniLM-L6-v2` (the paper uses an 8B-parameter embedder — see ADR-3 for the deliberate deviation). Full reports: [`benchmarks/reports/eval-scifact-2026-08-26.md`](benchmarks/reports/eval-scifact-2026-08-26.md), [`benchmarks/reports/eval-nfcorpus-2026-08-26.md`](benchmarks/reports/eval-nfcorpus-2026-08-26.md).

### nDCG@10 — ours vs paper (SciFact)

| Configuration | Ours (MiniLM) | Paper (Qwen3-8B MTEB / RRF) | Direction reproduced? |
|---|---|---|---|
| lexical | 0.0467 | ~0.18 | — (paper shows BM25 weak on fact-checking too) |
| semantic (float) | 0.6451 | 0.769 | gap explained by ADR-3 model scale |
| semantic (int8) | 0.6451 | ≈ float (paper claim) | **yes** — identical to float here |
| semantic (binary) | 0.5827 | ≈ 0.59 (paper Hamming column) | **yes** — ~10% drop vs float |
| hybrid (float) | **0.6568** | top of paper's hybrid table | **yes** — best of all |
| hybrid (int8) | 0.6568 | ≈ hybrid float | **yes** — quantization safe |
| hybrid (binary) | 0.5959 | within ~2% of paper Hamming hybrid | **yes** |
| hybrid + rerank | 0.6548 | similar (paper notes rerank often neutral) | **yes** — rerank no help when float already optimal |

### Qualitative claims the reproduction confirms

- **Hybrid ≥ best single mode** on both datasets (SciFact 0.6568 > 0.6451; NFCorpus 0.3249 > 0.3167).
- **int8 loses essentially nothing** vs float at retrieval-quality level (SciFact identical; NFCorpus -0.0011).
- **Binary is the cheapest precision that still dominates lexical** (SciFact 0.5827 vs 0.0467).
- **Rerank is a tie on these collections** — float cosine over the fused candidate pool is already near-optimal when the fused pool is small.

### Deviations from the paper (all documented in `docs/02`)

- **ADR-3** — model downscaled to MiniLM-L6-v2 (CPU, free). Absolute nDCG shifts as expected; directional findings survive.
- **ADR-7** — int8/binary computed application-side over stored float vectors because the shipped `sqlite-vec` 0.1.9 wheel rejects all int8/bit inputs (verified empirically). Math is identical to the paper's configurations.
- **ADR-8** — RRF k=60, leg depth = requested k, rerank pool = fused candidates.
- **Dataset scope** — SciFact + NFCorpus (5.2K + 3.6K docs). Touché (382K) and TREC-COVID (171K) excluded from the default scope; the harness supports them via registry.

## Status

- [x] Step 1–2: Paper selected & analyzed (facts in `docs/02-technical-design.md`)
- [x] Phase 0: Foundation (`benchmarks/reports/env-2026-08-26.md`)
- [x] Phase 1: Lexical engine (FTS5/BM25) — ingest idempotent, ranked search live
- [x] Phase 2: Semantic layer (sqlite-vec float storage + app-side int8/bit per ADR-7)
- [x] Phase 3: Hybrid RRF + rerank (RRF k=60; ablation ≥5/10 curated wins)
- [x] Phase 4: Reproduce BEIR results — SciFact & NFCorpus reports committed; qualitative paper claims confirmed
- [ ] Phase 5: Review + extension experiment
- [ ] Phase 6: Demo UI + publish
