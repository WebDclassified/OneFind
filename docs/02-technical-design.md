# 02 · Technical Design Document

Project: scrydb reproduction · Version: v0.4 (draft) · Status: Proposed

> v0.2 change: added ADR-7 — application-side int8/binary quantization after an
> empirical finding about the installed sqlite-vec build.
> v0.3 change: added ADR-8 — hybrid RRF defaults (fusion constant, leg depth,
> rerank pool).
> v0.4 change: added ADR-9 — BEIR acquisition via HuggingFace parquet + qrels
> sibling repos; pyarrow joins via the [eval] extra.

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

**ADR-7: int8/binary search modes computed application-side over float32 storage (Phase 2).**
Status: Accepted
Context: the paper evaluates semantic retrieval at three storage precisions (float cosine, int8 cosine, binary Hamming) attributed to sqlite-vec capabilities.
Finding: the installed wheel (`sqlite_vec` v0.1.9) declares `int8[n]` / `bit[n]` columns but **rejects every input path for them** — raw BLOB, `serialize_int8`, JSON array, even float-BLOB — for both INSERT and MATCH query vectors ("expected to be of type int8/bit, but a float32 vector was provided"). Only float32 works end-to-end.
Options: (a) hunt for a newer/dev sqlite-vec build and pin it; (b) keep one native `vec_float` table and compute int8-cosine and sign-bit-Hamming in numpy over the stored vectors inside the library API.
Choice: (b).
Reason: metrically identical to the paper's configurations, fully deterministic, zero fragile pins, and honest at our corpus sizes (~ms brute force in numpy). The limitation is itself a reproducible result worth reporting.
Consequences: the paper's *latency* claims for quantized search are not reproduced natively (our eval measures our real latencies instead); storage keeps one copy of vectors, not three.
Revisit when: a sqlite-vec release accepts quantized inputs — then `semantic_search` flips back behind the same function signature and T-10 can benchmark both paths.

**ADR-8: hybrid fusion parameters.**
Status: Accepted
Context: the paper fuses lexical + semantic rankings via RRF but our extraction does not state its fusion constant or retrieval depth.
Choice: RRF k=60 (Cormack et al. 2009 default), each leg retrieves depth k (the requested result count), fused top-k returned; `--rerank` rescores the fused candidate pool (≤2k docs) by full-precision cosine against stored vectors.
Reason: standard, deterministic, and matches the paper's "optionally reranked using more costly approaches" second stage.
Consequences: a deeper leg depth might raise recall at higher latency; kept simple for V1.
Revisit when: Phase 4 evaluation shows recall@10 deficits vs paper direction — then sweep leg depth as part of T-10 extension option 1.

**ADR-9: BEIR datasets acquired from HuggingFace-hosted copies.**
Status: Accepted
Context: original BEIR hosting has shifted over the years; PRD risk R1 anticipated download flakiness.
Finding: `BeIR/<name>` on the HF Hub serves corpus/queries as parquet under `corpus/` and `queries/` folders, with qrels in sibling repos `BeIR/<name>-qrels` as raw TSV.
Choice: download-and-cache those files under `data/` (gitignored); read parquet via `pyarrow`, exposed through a new `[eval]` extra alongside `ranx`.
Consequences: one extra dependency in the eval path only; registry names (`scifact`, `nfcorpus`) plus a local-folder mode keep tests fully offline-capable.
Revisit when: HF layout changes again — loaders accept both jsonl and parquet to soften future moves.

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
