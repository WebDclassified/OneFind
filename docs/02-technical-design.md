# 02 · Technical Design

Project: OneFind · Version: 1.1 · Status: Implemented

## System context

```text
CLI / packaged web UI / evaluation harness
                    │
                    ▼
        onefind.search dispatcher
       lexical │ semantic │ hybrid
          │        │         │
        FTS5   float/int8/binary  RRF/linear
          └────────┬────────────┘
                   ▼
                SQLite
      documents · chunks · FTS5 · vec_float · metadata
```

The default deployment requires no runtime network access after the model and optional datasets have been cached. All document/index content lives in one `.db` file.

## Stack

| Area | Decision | Reason |
|---|---|---|
| Language | Python ≥3.11 | Matches the scientific/IR ecosystem |
| Storage | SQLite + FTS5 + sqlite-vec | Single-file, transactional, local architecture |
| Embeddings | CPU `all-MiniLM-L6-v2` | Free, compact, reproducible directional study |
| Quantization | Application-side NumPy blocks | sqlite-vec 0.1.9 rejects int8/bit inputs |
| Metrics | ranx | Python-native BEIR-style metrics |
| API | argparse + library functions + FastAPI | Small dependency surface |
| UI | Packaged HTML/CSS/vanilla JS | No CDN, framework, or build step |
| CI | Mandatory core + full local stack + wheel | Verifies clean install and package data |

## Modules

- `ingest.py` — text/folder/JSONL loading and collision-safe IDs.
- `embed.py` — CPU model wrapper, composition, and quantization transforms.
- `store.py` — schema-v2 lifecycle, transactions, vector health, and metadata.
- `search.py` — BM25, native float KNN, block-scanned quantized modes, RRF, linear fusion, and reranking.
- `evaluate.py` — atomic BEIR builds, manifests, reports, alpha sweeps, and smoke cases.
- `serve.py` — capability-aware API, packaged static UI, CSP, lifecycle, and reset safety.
- `cli.py` — lowercase cross-platform command surface.

## Decision records

### ADR-1 — Reimplement from the paper

The project is an independent implementation and extension, not a fork. This demonstrates understanding and makes deviations auditable.

### ADR-2 — SQLite as the retrieval store

FTS5 handles lexical ranking and sqlite-vec stores/queries float vectors. The design favors operational simplicity and a single archival index over distributed infrastructure.

### ADR-3 — CPU-first MiniLM

`all-MiniLM-L6-v2` keeps the project free and accessible. Absolute scores are not expected to match an 8B model; directional retrieval findings are the target.

### ADR-4 — Whole-document V1 indexing

One chunk maps to one document, preserving the standard BEIR evaluation unit. Document chunking remains an application-layer extension.

### ADR-5 — ranx metrics

ranx provides nDCG@10, MAP@10, MRR@10, and Precision@10 without Java services.

### ADR-7 — Application-side int8 and binary ranking

The tested sqlite-vec 0.1.9 wheel declares quantized vector types but rejects quantized input paths. One float table is retained. Int8 uses a fixed scale of `1/127` for unit-normalized embeddings; binary uses sign bits and Hamming distance. Both scan bounded row blocks and maintain a deterministic top-k set.

This preserves the paper's mathematical configurations while honestly reporting O(N) quantized behavior and float storage.

### ADR-8 — Hybrid parameters

RRF uses k=60. Without reranking, each leg retrieves the requested result depth. With reranking, each leg retrieves `--candidate-depth` (default 50), fusion creates the complete union, and cosine produces refreshed top-k scores using one query encoding.

### ADR-9 — Atomic manifest-bound evaluation

Evaluation builds into a unique staging database. Only a successful, healthy build replaces the target. The manifest contains source identity, corpus/query/qrels hashes, selected-ID digests, model/revision, dimensions, metrics, and configurations.

### ADR-10 — Canonical file IDs and schema v2

File documents use the relative path including suffix as their ID. This eliminates same-directory `.md`/`.txt` and cross-directory stem collisions. Schema v2 intentionally rejects old indexes; rebuilding is safer than silently mixing identities.

### ADR-11 — Safe packaged UI

The UI is package data under `onefind/static`. All response-derived content is inserted with DOM text nodes or server-generated highlight segments. A restrictive CSP and defensive headers are applied. Remote binds require an explicit unsafe acknowledgement.

## Storage and transactions

- Documents, chunks, FTS rows, and float vectors are written in explicit batch transactions.
- Encoder shape, finite values, and nonzero norms are validated.
- A failed batch rolls back completely.
- Lexical updates to an already-embedded index are rejected unless an embedder is attached.
- Attaching a model to a lexical index backfills missing vectors.
- Index health compares document/chunk/FTS/vector counts and detects missing/orphan vectors.
- External vector updates invalidate cached matrices through SQLite `data_version`.

## Performance and observability

- Float search uses sqlite-vec KNN.
- Quantized search uses 2,048-row blocks by default and holds only a bounded candidate set.
- Evaluation reports p50/p95 latency, dataset hashes, model revision, commit, Python/OS, and package versions.
- The web API reports total request time and separate model-load time.

## Security and privacy

- No telemetry or hosted search service.
- Loopback binding is the default and non-loopback binds require `--allow-remote`.
- FTS input is tokenized and parameterized.
- Indexed HTML is displayed as text.
- CSP, frame denial, MIME sniffing protection, no-referrer, and browser permission restrictions are enabled.
- Reset is disabled unless an explicit startup token is supplied.
- Foreign absolute paths are not returned by browser-facing stats.

## Known boundaries

- Int8/binary search remains an exact full scan.
- Whole-document retrieval is not optimized for very long application documents.
- The unauthenticated remote mode is intentionally opt-in and unsuitable for direct internet exposure.
- Reports preserve actual latency rather than claiming the original fixed budget was universally met.
