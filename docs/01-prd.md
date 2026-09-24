# 01 · Product Requirements Document

Project: OneFind · Version: 1.1 · Status: Implemented

## Product summary

OneFind is a free, local information-retrieval toolkit for students, researchers, and developers who need lexical, semantic, and hybrid search without a hosted vector service. It stores documents and retrieval structures in one SQLite file, exposes a Python API and CLI, and includes a localhost web demo.

## Target user

- Students and engineers learning modern hybrid information retrieval.
- Portfolio and academic reviewers inspecting a reproducible implementation.
- Developers prototyping private local search over small or medium document collections.

## V1 goals

- **G1 — Retrieval engine:** FTS5 BM25, sqlite-vec float cosine KNN, exact int8/binary ranking, hybrid RRF, weighted linear fusion, and optional cosine reranking.
- **G2 — Interfaces:** installable library, lowercase `onefind` CLI, and packaged localhost web UI.
- **G3 — Evaluation:** SciFact and NFCorpus with nDCG@10, MAP@10, MRR@10, Precision@10, and p50/p95 latency.
- **G4 — Evidence:** committed reports with dataset hashes, model revision, package/runtime metadata, and a database manifest.
- **G5 — Extension:** an alpha sweep comparing weighted linear fusion with RRF.
- **G6 — Safe local operation:** no paid API, cloud dependency, telemetry, or remote binding by default.

## Non-goals

- Multi-user authentication or internet-facing deployment.
- Horizontal scaling or billions of vectors.
- LLM answer generation.
- GPU-required models.
- Public PyPI release under the current name.
- Whole-corpus approximate indexes for quantized modes; v1 uses exact bounded scans.

## Functional requirements

### FR-01 — Ingest

Accept a folder containing `.txt`/`.md`, a standalone text file, or a local JSONL dataset. Use the canonical relative filename as the document ID for file corpora, preventing same-stem collisions. Re-indexing is idempotent. Empty, malformed, duplicate, and missing inputs produce typed errors.

### FR-02 — Lexical search

Return strongest-first FTS5 BM25 matches with deterministic document-ID tie-breaking, safe arbitrary-query tokenization, and highlighted snippets.

### FR-03 — Semantic search

Return cosine neighbors in float, int8, or binary mode. Float uses native sqlite-vec KNN. Int8 and binary use deterministic application-side transforms over stored float vectors in bounded blocks.

### FR-04 — Hybrid search

Fuse lexical and semantic rankings with configurable RRF. Weighted linear fusion is available for research comparison. Alpha endpoints are exact passthroughs. Optional reranking uses a configurable per-leg candidate depth and returns refreshed cosine scores.

### FR-05 — Evaluation

Build a fresh temporary index, evaluate all configurations, write a provenance-rich report, and atomically publish the database only after success. Persist and verify a dataset/configuration manifest before alpha sweeps.

### FR-06 — Results documentation

Every current report must contain a machine-readable metric table plus model revision, dataset fingerprints, commit, runtime, and package versions. Historical reports affected by corrected defects must be marked superseded.

### FR-07 — Demo serve

Provide a responsive, accessible, packaged web UI with idle, loading, result, no-index, no-result, and error states. Untrusted indexed text must render as text, never executable HTML. Bind to loopback unless the user explicitly acknowledges remote exposure.

## Success signals

- **S1 — Direction:** hybrid nDCG@10 is competitive with or better than the best single mode on both full BEIR runs.
- **S2 — Quantization:** int8 remains close to float quality; binary has a documented quality trade-off.
- **S3 — Latency:** report p95 honestly for every mode. The original ≤150 ms target is a budget, not a claim of guaranteed compliance.
- **S4 — Retrieval smoke:** bundled gold queries achieve at least 0.80 Hit@3 and 0.80 MRR@5, with perfect lexical no-answer handling.
- **S5 — Reproducibility:** clean core CI, full local-stack CI, and a wheel containing the UI.
- **S6 — Safety:** mandatory regression tests cover BM25 direction, rerank scores/pool, untrusted HTML, schema isolation, and stale-vector prevention.

## Dependencies and risks

- First model/dataset download requires internet; later runs are local.
- CPU embedding dominates cold semantic latency.
- Quantized modes are exact O(N) scans and have lower scale than a dedicated vector service.
- Mutable model repositories require revision capture in evaluation evidence.
- Broad dependency ranges are bounded in `pyproject.toml`; a future lock file can further stabilize developer environments.
