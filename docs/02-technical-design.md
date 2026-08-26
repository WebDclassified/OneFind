# 02 · Technical Design Document

Project: scrydb reproduction · Version: v0.1 (draft) · Status: Proposed

## System context

```
CLI / demo page          evaluation harness
      │                        │
      ▼                        ▼
┌─────────────────────────────────────┐
│ scrydb library (Python)             │
│  ingest → embed → store             │
│  search: lexical │ semantic │ RRF   │
└───────────────┬─────────────────────┘
                ▼
   single SQLite file (FTS5 tables + sqlite-vec virtual tables + documents)
```

No runtime network dependency after the one-time embedding-model download. Everything lives in one `.db` file — the paper's core claim ("SQLite is enough") is also our architecture constraint.

## Stack decisions (baseline brief)

| Area | Decision | Reason / constraint |
|---|---|---|
| Language | Python ≥3.11, `venv`, pinned in `requirements.txt` | Matches upstream ecosystem (sentence-transformers, sqlite-vec bindings) |
| Storage | SQLite single file: FTS5 + sqlite-vec | The paper's thesis; zero-infra; archival single-file story |
| Embeddings | CPU-first: `all-MiniLM-L6-v2` (384-dim); optional second model if GPU found | ADR-3 |
| Metrics | `ranx` for AP/RR/P@10/nDCG@10; custom timing wrapper | Standard IR metrics without heavy Java deps |
| API style | Library functions + thin CLI (`typer` or `argparse`) + optional FastAPI `serve` | Library-first, like the paper |
| Delivery | Local run; GitHub Actions running pytest on push (Phase 0) | Reproducibility signal for portfolio |
| Identity | None — local single-user tool | Out of scope (PRD non-goal) |

## Architecture of the library

Modules:
- `ingest.py` — corpus loading (folder of txt/md, BEIR JSONL), id assignment, transactional batch upsert.
- `embed.py` — `SentenceEmbedding` wrapper (model name → normalized vectors), batched encode, precision transforms (`float32`, int8 scale/zero-point, binary sign-bits packed).
- `store.py` — schema creation/migration, FTS5 sync triggers, vec-table writes.
- `search.py` — three modes + RRF fusion + optional rerank (cosine over float vectors of top candidates).
- `evaluate.py` — BEIR loader, qrels handling, ranx evaluation, latency measurement, markdown report writer.

## Decision records

**ADR-1: Reimplement rather than fork.**
Context: goal is learning + portfolio signal. Options: fork upstream MIT repo / reimplement from paper. Choice: reimplement from paper, allowed to read upstream for API sanity. Consequences: slower start, far stronger demonstration; risk of subtle divergence documented via tests vs reported behaviors. Revisit when: timeline pressure before deadline → switch to "extension on upstream" framing.

**ADR-2: SQLite+FTS5+sqlite-vec as sole storage.**
Options: Chroma / FAISS sidecar / pgvector / pure SQLite. Choice: pure SQLite per paper thesis. Consequences: simpler story and deploy; ceiling ~100K docs comfortable. Revisit when: benchmark corpus grows beyond that.

**ADR-3 (key deviation): embedder downscale.**
Context: paper's experiments center on Qwen/Qwen3-Embedding-8B (8B params — impractical here). Options: (a) same model via paid API/GPU; (b) smaller local model compared against *its own* MTEB baseline. Choice: (b) MiniLM-class. Reason: keeps everything free/local/CPU while preserving experimental design (13-ish configurations × datasets). Consequences: absolute nDCG will differ from paper tables; we compare directional patterns + our-model MTEB reference instead. Revisit when: GPU access appears → add bge-base run as second column.

**ADR-4: whole-document indexing for evaluation fidelity.**
BEIR protocol scores whole documents; upstream example indexes docs directly. Choice: no chunking in V1 eval path. Consequences: honest comparability with published numbers; chunking added later only for the app-facing feature set. 

**ADR-5: metrics library = `ranx`.**
Options: pytrec_eval (C ext) / ranx (pure Python). Choice: ranx. Consequence: trivial install on Windows; if a metric mismatches expectations we cross-check one table with pytrec_eval once.

## Performance budget & observability

- Index throughput target: ≥100 docs/s on SciFact-sized corpora (CPU).
- Query budget p95 ≤150 ms @ ≤10K docs (any mode).
- Observability: `--verbose` structured logs (stage timings: embed ms, fts ms, knn ms, fuse ms); every eval report embeds machine metadata (CPU, RAM, package versions).

## Security & privacy

- No secrets required; model cache under user cache dir.
- `serve` binds `127.0.0.1` only.
- Docs indexed from disk stay on disk; README carries a note warning users not to index confidential folders into shareable `.db` files.

## Failure behaviour

- Missing FTS5/sqlite-vec at startup → explicit error naming the missing extension and install hint (T-00 checks this first).
- Corrupt/partial DB → open() runs integrity check and reports recoverable vs fatal.
