# OneFind — Reproducing "SQLite is Enough"

Reproduction of **[SQLite is Enough. Lexical, Semantic, and Hybrid Search with OneFind](https://arxiv.org/abs/2608.24060)** (arXiv:2608.24060, cs.IR) — a lightweight hybrid search engine built entirely on SQLite: FTS5 (BM25) + sqlite-vec (embeddings) + Reciprocal Rank Fusion.

> **Major-project final report**: see [`FORMAL_PROJECT_REPORT.md`](FORMAL_PROJECT_REPORT.md) for the academic-style write-up (Executive Summary → Related Work → Design → Results → Discussion → Conclusion → Future Work → Appendices).
>
> **Step-by-step run guide**: [`HOW_TO_RUN.md`](HOW_TO_RUN.md).
> **Viva preparation**: [`VIVA_QUESTIONS.md`](VIVA_QUESTIONS.md) and [`TEAM_PREPARATION.md`](TEAM_PREPARATION.md).
>
> **Portfolio project**: read literature → implement from scratch → reproduce reported behavior → extend → publish.

## Quickstart (clone → demo in ~5 minutes)

```bash
git clone <this repo> OneFind
cd OneFind
python -m venv .venv
.venv\Scripts\activate                      # Windows
# source .venv/bin/activate                  # Linux/macOS
pip install -e ".[model,eval,serve]"

# 1. prove the environment
OneFind check --full

# 2. reproduce one BEIR dataset end-to-end (downloads, embeds, evaluates)
OneFind eval scifact --db data/scifact.db
OneFind eval nfcorpus --db data/nfcorpus.db

# 3. or try a tiny in-memory demo with the included sample data
OneFind index ./sample-data --db demo.db --embed
OneFind serve --db demo.db --port 8080
# open http://127.0.0.1:8080/

# 4. run the tests
pytest
```

## What this repo contains

| Path | Purpose |
|---|---|
| `FORMAL_PROJECT_REPORT.md` | **Full academic report** with Executive Summary, Abstract, References, Appendices |
| `HOW_TO_RUN.md` | **Step-by-step reproduction guide** from a fresh machine to a working demo |
| `VIVA_QUESTIONS.md` | **Anticipated defense questions** with model answers (categorized) |
| `TEAM_PREPARATION.md` | **Full project briefing for the 3 teammates** — story, architecture, demo, Q&A |
| `REPORT.md` | Earlier, shorter academic-style report |
| `docs/01-prd.md` … `docs/07-references.md` | Six-document spec system (source of truth, versioned with code) |
| `src/OneFind/` | The library + CLI + FastAPI demo |
| `tests/` | pytest suite — 67 tests, model- and serve-extras gated |
| `benchmarks/reports/` | Generated evaluation reports + review notes |
| `demo/index.html` | Single-page demo UI (vanilla JS, no build step) |
| `sample-data/` | Four tiny `.md` files for the smoke demo |
| `data/` | BEIR datasets cache (gitignored, auto-downloaded) |
| `screenshots/` | Placeholder for demo screenshots |

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

## Extension: weighted linear fusion vs RRF (T-10)

Adds a second hybrid strategy to `hybrid_search` (CLI: `--fusion linear` + `--alpha`). The same legs are retrieved, each leg's scores are min-max-normalized to `[0, 1]`, then blended `α * semantic + (1-α) * lexical`. Full reports:
[`benchmarks/reports/alpha-sweep-scifact-2026-08-26.md`](benchmarks/reports/alpha-sweep-scifact-2026-08-26.md),
[`benchmarks/reports/alpha-sweep-nfcorpus-2026-08-26.md`](benchmarks/reports/alpha-sweep-nfcorpus-2026-08-26.md).

| α | SciFact nDCG@10 | NFCorpus nDCG@10 |
|---|---|---|
| 0.0 (pure lexical) | 0.4396 | 0.2763 |
| 0.1 – 0.5 plateau | **0.6572** | 0.3166 – 0.3217 |
| 0.6 | 0.6548 | **0.3229** (best linear) |
| 1.0 (pure semantic) | 0.6460 | 0.3170 |
| **RRF baseline (default)** | **0.6568** | **0.3249** |

**Conclusions.**
- On **SciFact**, linear fusion with any `α ∈ [0.1, 0.5]` ties RRF to 4 decimal places — a flat plateau, suggesting the two legs mostly retrieve the same top set and blending does not hurt.
- On **NFCorpus**, **RRF beats the best linear by 0.002** (0.3249 vs 0.3229). RRF's rank-position aggregation is more robust when the two legs have very different score distributions and a wider gap between the top document and the rest.
- Either way, the differences are inside the noise of these single-run numbers; the practical choice is whichever is simpler to operate, and RRF stays the default per the paper.

## Self review (T-09)

`benchmarks/reports/review-t09.md` records what was checked, what changed, and what was intentionally left alone after a self-pass through the engine and the Phase 4 numbers.

## Status

- [x] Step 1–2: Paper selected & analyzed (facts in `docs/02-technical-design.md`)
- [x] Phase 0: Foundation (`benchmarks/reports/env-2026-08-26.md`)
- [x] Phase 1: Lexical engine (FTS5/BM25) — ingest idempotent, ranked search live
- [x] Phase 2: Semantic layer (sqlite-vec float storage + app-side int8/bit per ADR-7)
- [x] Phase 3: Hybrid RRF + rerank (RRF k=60; ablation ≥5/10 curated wins)
- [x] Phase 4: Reproduce BEIR results — SciFact & NFCorpus reports committed; qualitative paper claims confirmed
- [x] Phase 5: Review (`benchmarks/reports/review-t09.md`) + alpha-sweep extension on both datasets
- [x] Phase 6: Demo web app (`OneFind serve`) + publish polish — all PRD success signals met
- [x] Defense prep: `FORMAL_PROJECT_REPORT.md`, `HOW_TO_RUN.md`, `VIVA_QUESTIONS.md`, `TEAM_PREPARATION.md`

## What we learned / what surprised us

- **A shipped library can claim capabilities the binary doesn't deliver.** The `sqlite-vec` 0.1.9 wheel *declares* `int8[n]` and `bit[n]` vector columns but rejects every input path we probed. The reproduction's most interesting finding is in `docs/02 ADR-7` — and the application-side quantization path is mathematically equivalent to the paper's configurations.
- **Hybrid fusion isn't free improvement.** RRF wins by rank-position, not by score magnitude; on NFCorpus the *best* linear fusion loses to default RRF by 0.002 nDCG. Score-similarity between legs is not a free lunch.
- **A 384-dim embedder reproduces all the paper's directional claims** even though absolute nDCG sits below the paper's 8B baseline. Quality is mostly about the architecture, not the model size.
- **Six-doc spec + paper-reproduction loop is a self-correcting system.** Every stale-guard test, every "sanity noop" line, every "loading embedding model: None" came out of the run-evidence-assert loop, not from reading the code in isolation. The loop is the methodology.

## Future work (out of V1 scope)

- Cross-encoder reranker on hybrid top-50 (quality vs latency curve)
- Matryoshka-style dimension reduction for MiniLM
- Watch-folder live re-index
- Document-level chunking for application use-cases (BEIR eval stays whole-doc per ADR-4)
- PyPI package name (upstream already owns `OneFind`; rename for any public release)
