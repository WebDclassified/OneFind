# 01 · Product Requirements Document

Project: scrydb reproduction · Version: v0.1 (draft) · Status: Proposed
Owner: project author · Companion docs: 02–07 in this folder

## PRODUCT SUMMARY

CS students / junior ML-and-IR engineers who want to understand and demonstrate modern hybrid retrieval currently face heavyweight alternatives (Elasticsearch, pgvector, hosted vector DBs); this project rebuilds the scrydb pipeline — FTS5 lexical + sqlite-vec semantic + RRF hybrid search inside one local SQLite file — from its paper, reproduces its BEIR evaluation directionally on affordable hardware, and ships a demo + written comparison as a portfolio artifact.

## TARGET USER & CURRENT ALTERNATIVE

- Primary: you (portfolio/research skill-building) plus evaluators (professors, interviewers reading the repo).
- Secondary: developers prototyping agentic/RAG search who need zero-infra retrieval.
- Current alternatives: Elasticsearch/OpenSearch (ops-heavy), Postgres+pgvector (server), Pinecone/Weaviate (cloud cost, data leaves machine), upstream scrydb itself (the object of study — we reimplement to learn, then compare).

## V1 GOALS

- G1: Python library implementing lexical (FTS5 BM25), semantic (sqlite-vec KNN at binary/int8/float precision), and hybrid (RRF) search over one SQLite database.
- G2: CLI: `index`, `search`, `eval`, `serve`.
- G3: Evaluation harness reproducing the paper's experimental design on a reduced BEIR subset (SciFact + NFCorpus core; FiQA stretch) with AP, RR, P@10, nDCG@10 + query latency p50/p95.
- G4: Written results comparison: our numbers vs paper-reported patterns and MTEB baselines for our chosen embedder.
- G5: One meaningful extension (see 06 §Extensions) + polished README.

## NON-GOALS (this release)

- Multi-user auth, server deployment, horizontal scaling (>~100K docs).
- Chunking strategies (V1 indexes whole documents, matching the paper's BEIR protocol; chunking is an application-layer feature later).
- LLM answer generation / full RAG chat.
- Matching absolute nDCG numbers of the paper's 8B-parameter embedder (explicit deviation, see ADR-3).

## REQUIREMENTS (must-have)

**FR-01 Ingest** — Given a folder of `.txt`/`.md` files or a JSONL corpus, when `scrydb index <path> --db x.db` runs, then every document is stored and embedded; re-running is idempotent (upsert by stable doc id).
Acceptance criteria: given empty folder → clear error, non-zero exit; given valid corpus → row counts match input; interrupted run leaves DB openable (transactional batches).

**FR-02 Lexical search** — FTS5 BM25 ranking over indexed docs.
Acceptance: top-k returned for a keyword query on a loaded DB; empty query → validation error; no hits → empty result, exit 0.

**FR-03 Semantic search** — cosine KNN over stored embeddings with three storage precisions: `float32`, `int8`, `binary`.
Acceptance: same query returns semantically related docs lacking exact keywords; precision modes all return results and differ only in measured quality/speed.

**FR-04 Hybrid search** — RRF fusion of lexical + semantic rankings (paper's k parameter configurable, default per paper).
Acceptance: fused ordering deterministic; `--mode lexical|semantic|hybrid` all reachable from CLI and API.

**FR-05 Evaluation harness** — `scrydb eval --dataset scifact` downloads/prepares the BEIR set, runs all configurations, emits a markdown report with AP/RR/P@10/nDCG@10 per configuration and latency percentiles.
Acceptance: report regenerates deterministically given fixed seeds/model; runs end-to-end on CPU laptop.

**FR-06 Results documentation** — README table comparing ours vs paper direction (does hybrid ≥ best single mode? does binary/int8 lose little quality vs float?) with deviations explained.
Acceptance: every number traceable to a committed report file.

**FR-07 Demo serve** — `scrydb serve` opens a local-only page: query box, mode toggle, results with scores.
Acceptance: loading/empty/no-results/error states all implemented (see doc 03).

## SUCCESS SIGNALS

- S1 Directional fidelity: hybrid nDCG@10 ≥ max(single-mode) on SciFact (paper reports this pattern); recorded with our numbers.
- S2 Quantization trade-off reproduced: binary/int8 within a small, reported margin of float quality at measurably higher speed.
- S3 Efficiency: p95 query latency ≤150 ms @ ≤10K docs on reference CPU laptop.
- S4 Portfolio: repo passes a cold clone-to-demo test by a peer in ≤15 min.

## ASSUMPTIONS / DEPENDENCIES / RISKS / OPEN QUESTIONS

- A1 (assumption): bundled CPython SQLite on Windows target includes FTS5; sqlite-vec wheel installs cleanly. Verify in Phase 0 (T-00).
- D1 (dependency): first run downloads the embedding model (~30–130 MB) — needs network once.
- R1 (risk): BEIR download flakiness → vendor a cached copy under `data/` after first success.
- R2 (risk): paper's headline model is Qwen3-Embedding-8B; our smaller embedder will shift absolute numbers — mitigated by comparing against MTEB baselines for *our* model (ADR-3).
- OQ1: Do you have any CUDA GPU? Default plan assumes CPU-only (MiniLM-class embedder). If a GPU exists, we add bge-base as a second model.
- OQ2: Dataset scope confirmed as SciFact+NFCorpus core, FiQA stretch? (Touché/TREC-COVID excluded for scale.)
- OQ3: Package name: upstream owns "scrydb". Default working name stays `scrydb` locally; rename (e.g. `scrylite`) before any public release.
