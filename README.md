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

_Filled by `benchmarks/run_eval.py` in Phase 4 — see `docs/06-engineering-plan.md`._

## Status

- [x] Step 1–2: Paper selected & analyzed (facts in `docs/02-technical-design.md`)
- [x] Phase 0: Foundation (`benchmarks/reports/env-2026-08-26.md`)
- [x] Phase 1: Lexical engine (FTS5/BM25) — ingest idempotent, ranked search live
- [ ] Phase 2: Semantic layer (sqlite-vec, 3 precisions)
- [ ] Phase 3: Hybrid RRF + rerank
- [ ] Phase 4: Reproduce BEIR results
- [ ] Phase 5: Review + extension experiment
- [ ] Phase 6: Demo UI + publish
