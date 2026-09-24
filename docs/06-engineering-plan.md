# 06 · Engineering Implementation Plan

Project: OneFind · Version: 1.1 · Status: Implemented
Execution model: **paper-reproduction loop** — each phase = read relevant paper section → implement one task → test → compare against paper behavior → commit with evidence. AI assists per-task, never generates whole phases.

## v1.1 hardening pass — completed

The original V1 reproduced the architecture but contained correctness and release gaps discovered during independent review. The hardening pass adds:

- strongest-first BM25 regression coverage;
- native sqlite-vec float KNN;
- complete rerank pool, refreshed cosine scores, and one query encoding;
- exact linear-fusion endpoint passthroughs;
- canonical path IDs, JSONL ingest, and schema-v2 identity safety;
- transactional encoder validation and vector health;
- atomic manifest-bound evaluation databases;
- bounded quantized scanning and external cache invalidation;
- 20-document/26-query free smoke corpus;
- packaged, accessible, CSP-protected no-build UI;
- 112 tests, clean-install CI, wheel verification, and MIT licensing.

Historical benchmark claims are superseded until regenerated from this implementation.

## Phase map (reproduction guide steps → phases)

| Step (guide) | Phase | Output |
|---|---|---|
| S3 project structure | P0 Foundation | repo layout, env, CI, `check` command |
| S4–S5 tasks | P1–P3 engine | lexical → semantic → hybrid |
| S6 reproduce results | P4 evaluation | BEIR reports + comparison tables |
| S7 AI review | P5a review pass | review findings fixed |
| S8 extend | P5b extension | one experiment beyond the paper |
| S9 publish | P6 polish | demo UI + final README |

## Tasks

### T-00 · Environment proof (P0)
Outcome: `onefind check` passes on target laptop. Deps: none.
Notes: verify FTS5 in bundled SQLite; install sqlite-vec; download MiniLM; record versions in `benchmarks/env-<date>.md`.
Acceptance: check exits 0 listing sqlite/fts5/vec/model versions; exit 3 with named fix when an extension is absent.
Tests: unit test mocking missing extension path. Evidence: terminal transcript committed to docs.

### T-01 · Skeleton & CI (P0)
Outcome: package layout per README table; pytest green; GitHub Actions runs it on push.
Acceptance: `pytest -q` passes from clean clone; Actions badge green.

### T-02 · Ingest + documents/fts schema (P1)
Outcome: `index` builds documents + FTS5 rows transactionally; idempotent re-run.
Acceptance criteria: given SciFact corpus sample → counts match; duplicate run changes nothing except updated_at; empty folder → error code 4.
Tests: ingest round-trip, idempotency, failure rollback.

### T-03 · Lexical search (P1)
Outcome: BM25 top-k with snippets; deterministic ordering.
Acceptance: keyword query returns expected doc in top-10 on smoke set; empty query → EmptyQueryError(2).
Evidence: CLI transcript.

### T-04 · Embeddings + vec_float (P2)
Outcome: batched encoding at index time; cosine KNN query mode.
Acceptance: semantic search finds paraphrase doc containing no shared keywords (curated test case); p95 embed+query budget met on smoke set.

### T-05 · Quantized precisions: int8 + binary (P2)
Outcome: float vectors stored in sqlite-vec; int8/binary precision available through deterministic application-side transforms per ADR-7.
Acceptance: same query across precisions returns overlapping-but-not-identical rankings (sanity), all three modes runnable.

### T-06 · RRF fusion + rerank flag (P3)
Outcome: hybrid mode fusing lexical+semantic via RRF (paper's k configurable); optional rerank of top candidates by float cosine.
Acceptance: fusion deterministic; rerank never lowers k count; ablation smoke shows hybrid ≥ each single mode on ≥5/10 curated queries.

### T-07 · Eval harness (P4)
Outcome: `eval --dataset scifact|nfcorpus` downloads/caches BEIR, runs all configurations, writes markdown report (AP/RR/P@10/nDCG@10 + latency p50/p95).
Acceptance: report regenerates deterministically (pinned model); runs unattended on CPU; numbers land in README comparison table with ours-vs-paper columns.

### T-08 · Reproduction analysis (P4)
Outcome: written analysis — do directional findings hold? (hybrid ≥ single-mode; binary/int8 ≈ float quality at speed gain). Deviations explained (ADR-3 model swap, dataset subset).
Acceptance: every claim traceable to a committed report file; discrepancies investigated, not hidden.

### T-09 · AI review pass (P5a)
Outcome: full-codebase review request (correctness vs paper §2–4, edge cases, perf); findings triaged into fixes or documented non-actions.
Evidence: review notes file + fix commits.

### T-10 · Extension experiment (P5b) — pick ONE
Options ranked by effort/value:
1. Weighted score-fusion sweep α∈{0.1..0.9} vs RRF — does tuned linear beat RRF anywhere?
2. Cross-encoder reranker on hybrid top-50 (quality gain vs latency cost curve).
3. Dimensionality/matryoshka truncation study for MiniLM embeddings.
Acceptance: one-page experiment section added to report with chart + conclusion.

### T-11 · Demo serve UI (P6)
Outcome: localhost page implementing doc 03 states + doc 04 tokens exactly.
Acceptance: all six UI states demonstrable; keyboard/a11y checklist passes.

### T-12 · Publish polish (P6)
Outcome: final README (overview, setup, results vs paper, learnings, future work), tagged release v1.0.
Acceptance: cold clone-to-demo by peer ≤15 min (PRD S4).

## Definition of done (per task)

- Acceptance criteria pass including failure states; tests cover new behavior.
- Docs updated when decisions change (version bump in changed doc headers).
- Commit message links task id; evidence (report/transcript) referenced.

## Risks watchlist

BEIR download flakiness (R1) → cache after first success · CPU embedding throughput below budget → shrink stretch datasets before touching core scope · scope creep → any new idea lands in "future work" unless a phase owns it.
