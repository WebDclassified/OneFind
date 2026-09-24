# OneFind Reference Features

This is the canonical feature reference for OneFind v1.1. Feature IDs are stable. Any feature change must update this file, the relevant specification, tests, presentation guide, and browser evidence in the same change.

## Product promise

OneFind provides private, free, CPU-friendly lexical, semantic, and hybrid retrieval in one local SQLite file without a paid API or hosted vector database.

## Feature matrix

| ID | Feature | User interface | CLI / library | Verification |
|---|---|---|---|---|
| F-01 | Local single-file engine | Indexed status and local privacy badge | `onefind check`; `Index.open()` | `tests/test_store.py`, `onefind check` |
| F-02 | Keyword BM25 search | “Keyword” mode | `onefind search --mode lexical`; `lexical_search()` | `tests/test_search_lexical.py` |
| F-03 | Semantic float search | “Full precision” option | `onefind search --mode semantic --precision float`; `semantic_search()` | `tests/test_semantic.py` |
| F-04 | Int8 semantic search | “Int8” option | `--precision int8` | `tests/test_semantic.py`, showcase smoke |
| F-05 | Binary semantic search | “Binary” option | `--precision binary` | `tests/test_semantic.py`, showcase smoke |
| F-06 | Hybrid RRF search | “Hybrid” mode | `--mode hybrid`; `hybrid_search()` | `tests/test_hybrid.py` |
| F-07 | Linear fusion extension | Alpha is available in CLI/library | `--fusion linear --alpha`; `linear_fuse()` | `tests/test_fusion.py`, `tests/test_sweep.py` |
| F-08 | Cosine reranking | Precision-aware hybrid option | `--rerank --candidate-depth` | `tests/test_hybrid.py`, `tests/test_search_edges.py` |
| F-09 | Text / Markdown / JSONL ingest | Upload-free local corpus model | `onefind index PATH`; `ingest_path()` | `tests/test_ingest.py` |
| F-10 | Collision-safe document IDs | Source shown on result cards | Canonical relative filename IDs | `tests/test_ingest.py` |
| F-11 | Transactional vector writes | Health status | `Index.add_documents()` | `tests/test_store.py` |
| F-12 | Vector health and stale-write protection | Capability-aware controls | `Index.health()`; `has_vectors()` | `tests/test_store.py`, `tests/test_serve.py` |
| F-13 | Atomic manifest-bound BEIR evaluation | Provenance-rich reports | `onefind eval`; `run_eval()` | `tests/test_eval.py` |
| F-14 | Gold-query smoke evaluation | Presentation corpus and curated examples | `onefind smoke`; `run_smoke()` | `tests/test_smoke.py`, `tests/test_showcase_smoke.py` |
| F-15 | Large presentation corpus | 120-record knowledge base | `showcase-data/` | `tests/test_showcase_corpus.py` |
| F-16 | Packaged responsive UI | Dark/mobile/accessibility states | `onefind serve` | `tests/test_serve.py`, `sshot/manifest.json` |
| F-17 | Safe untrusted-content rendering | Literal text, no executable HTML | CSP + text/highlight segments | `tests/test_serve.py`, browser XSS scenario |
| F-18 | Loopback-first server | On-device status | Default `127.0.0.1`; `--allow-remote` override | `tests/test_cli.py`, browser scenarios |
| F-19 | Reset safety | Reset not exposed in normal UI | Disabled unless explicit token | `tests/test_serve.py` |
| F-20 | Reproducible browser evidence | Presentation screenshot index | `tools/browser_check.py` | `sshot/manifest.json` |
| F-21 | MIT package and wheel | N/A | `pip install .` | `pyproject.toml`, wheel asset check |
| F-22 | Defense-ready documentation | Jury-facing runbook and Q&A | `HOW_TO_RUN.md`, `TEAM_PREPARATION.md`, `VIVA_QUESTIONS.md` | Manual review |

## Data reference

| Dataset | Documents | Queries | Purpose |
|---|---:|---:|---|
| `sample-data/` | 20 | 26 | Small offline smoke and UI examples |
| `showcase-data/` | 120 | 202 | Large complex presentation and retrieval quality |
| BEIR local cache | SciFact: 5,183; NFCorpus: 3,633 | 300 / 323 judged | Research reproduction and corrected benchmark evidence |

## Evidence reference

- `benchmarks/reports/eval-scifact-2026-09-24.md`
- `benchmarks/reports/eval-nfcorpus-2026-09-24.md`
- `benchmarks/reports/alpha-sweep-scifact-2026-09-24.md`
- `benchmarks/reports/alpha-sweep-nfcorpus-2026-09-24.md`
- `benchmarks/reports/v1.1-independent-review.md`
- `sshot/manifest.json`

## Change rule

When adding or changing a feature:

1. Keep or allocate a stable feature ID.
2. Update the relevant row here.
3. Update the owning specification under `docs/`.
4. Add or adjust a regression test.
5. If UI-visible, delete prior `sshot/*.png` and rerun `tools/browser_check.py`.
6. Update `README.md`, `HOW_TO_RUN.md`, `TEAM_PREPARATION.md`, and `VIVA_QUESTIONS.md` when the change affects presentation or defense claims.
