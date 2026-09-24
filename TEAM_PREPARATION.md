# Team Preparation — Full Project Briefing

> **For**: the 3 teammates who will assist in defending the major project.
> **Goal**: by the time you finish reading this, you can answer any
> reasonable question about the project, demo the running system, and
> explain why every decision was made.
> **Reading time**: about 90 minutes if you read end to end; about 30
> minutes if you skim and use the section index.
> **Practice time**: another 2–3 hours of mock defense with the team.

---

## A Note on Contribution

Before we get into the technical content, a note on contribution. The implementation of this project — the code, the tests, the documentation, the evaluation — was done by **the lead author** (the person you'll see presenting first) with the assistance of an AI coding agent. **You are not expected to write the code again; you are expected to understand it deeply enough to defend it.**

This is normal for major-project defenses in most engineering programs. The lead author is the technical expert; the team members are co-defenders who can carry a portion of the defense, ask clarifying questions, and demonstrate that the work was reviewed by multiple people.

Your preparation job, then, is **understanding**, not implementation. Read this document carefully. Run the demo yourself at least once. Read the source code for the modules listed in your assigned section below. Practice explaining the architecture in your own words.

If you have a question you cannot answer after reading this document and the source code, **ask the lead author before the defense**. There are no bad questions in preparation.

---

## Section 0: The Project at a Glance

**The claim we are testing.** A recently published paper, *SQLite is Enough: Lexical, Semantic, and Hybrid Search with scrydb*, claims that a single SQLite database file is enough to deliver production-quality hybrid retrieval (lexical + semantic search, fused). The claim is disruptive because the standard architecture requires a separate vector database, a separate lexical index, and an orchestration layer.

**The project.** Reimplement the OneFind system from the paper, run it on two standard information-retrieval benchmarks, document any deviations, and conduct one independent extension experiment.

**What we built.** A Python library, a lowercase cross-platform CLI, a packaged FastAPI web UI, a 112-test suite, a 20-document/26-query smoke corpus, reproducible evaluation manifests, and seven living specification documents. The original phase history was followed by a v1.1 correctness and release-hardening pass.

**What we found.**
- Hybrid improves over the best single mode on both corrected datasets, while int8 stays close to float and binary trades quality for compact configuration math. Pure-cosine reranking does not improve these runs.
- The paper's toolchain (`sqlite-vec` 0.1.9) has a limitation that is not discussed in the paper: it declares `int8[n]` and `bit[n]` vector columns but rejects all int8/bit inputs. This is the project's most interesting finding.
- Corrected linear fusion and RRF are effectively tied: each best linear result is only 0.0004 nDCG above RRF in these single runs, so RRF stays as the simpler default.

---

## Section 1: The Story from Day 1 (chronological)

The project progressed through seven phases over the development period. Knowing the chronology helps you understand *why* things are the way they are.

### Day 1: Choosing the paper

We started by browsing `https://arxiv.org/archive/cs` for IR and database papers. The OneFind paper caught our attention because:
1. The central claim is counter-intuitive and operationally significant.
2. The authors released MIT-licensed code, so a reproduction is feasible.
3. The paper covers a range of retrieval modes (lexical, semantic at three precisions, hybrid), giving us a multi-dimensional experimental surface.

We did not pick a paper because it was "easy" or because the field was "hot". We picked it because it was reproducible and the claim was worth testing.

### Phase 0 (foundation): 1–2 days

We set up the project skeleton: a six-document specification system (PRD, Technical Design, App Flow, UI/UX Brief, Backend Design, Engineering Plan), a `pyproject.toml` with a CLI entry point, a sample corpus, a `onefind check` command for environment proof, and a CI workflow.

The environment-proof step is the first engineering discipline of the project: before any code is written, we prove that the runtime can host the work. The proof caught one real issue (FTS5 schema-qualified names in `bm25()`) before the lexical engine was built on top of it.

### Phase 1 (lexical): 1–2 days

We built the lexical engine: a SQLite schema with documents, chunks, FTS5, and schema_meta tables; a transactional 256-document ingest; BM25 search with deterministic tie-breaking; a hostile-input query sanitizer. The lexical engine is the foundation — if it does not work, the rest of the project has nothing to build on.

**Key discipline**: every test in this phase was tied to an acceptance criterion in the PRD, and every test failure was traced to a specific bug in the implementation. This pattern continued throughout the project.

### Phase 2 (semantic): 2–3 days

We added the semantic engine: a CPU SentenceEmbedder wrapper, native sqlite-vec float KNN, and application-side int8/binary ranking. This phase produced the project's most interesting toolchain finding (ADR-7): the `sqlite-vec` 0.1.9 wheel rejects all int8/bit inputs. The v1.1 implementation streams quantized comparisons in bounded blocks and keeps only one float vector table.

**Key discipline**: empirical probing before code. We tested the `sqlite-vec` library's int8/bit behavior with a tiny standalone script *before* writing the implementation, which saved days of debugging against a misbehaving library.

### Phase 3 (hybrid): 1–2 days

We added hybrid RRF fusion and an optional full-precision rerank. The implementation is small — about 30 lines of Python — but the test discipline is large: we wrote a 10-query ablation that asserts hybrid beats both single modes on at least 5 of 10 queries, which is a real acceptance criterion from the engineering plan.

**Key discipline**: the ablation test is a quantitative assertion, not a smoke test. If a future change regresses hybrid fusion, the test will fail.

### Phase 4 (evaluation): 2–3 days

We wrote the evaluation harness: BEIR dataset acquisition (parquet + qrels TSV via HuggingFace), an 8-configuration runner, ranx-based metric computation, and a markdown report writer. We ran the harness on SciFact (5,183 docs) and NFCorpus (3,633 docs) and committed the resulting reports.

**Key discipline**: the harness accepts both a registry name and a local folder, so the tests can use a synthetic mini-BEIR fixture while real runs use the actual BEIR datasets. The end-to-end test catches bugs in the integration that unit tests miss.

### Phase 5 (review + extension): 1–2 days

We did an initial self review (T-09) and a linear-fusion extension (T-10), then a separate v1.1 review corrected BM25 direction, rerank scoring, clean installation, evaluation isolation, and UI safety. The corrected sweep finds no meaningful single-run winner: linear leads RRF by 0.0004 on each dataset.

**Key discipline**: the extension is a parameter sweep, not a new algorithm. The contribution is the controlled comparison and the decision to retain RRF for simplicity when measured differences are negligible.

### Phase 6 (demo + publish): 1–2 days

We built the FastAPI-based localhost web demo and polished the README and report. The demo implements every UI state specified in the design brief (loading, empty, no-results, error, results, info). The README is structured to satisfy both GitHub visitors and engineering evaluators.

**Key discipline**: the demo is a real-time system, not a video. A reviewer can run `onefind serve` and interact with the system, which is the strongest possible demonstration that the project works.

---

## Section 2: The Big Picture (elevator pitch)

If a professor asks "what is this project?" and you have 30 seconds, say:

> "This is a reproduction of a recent paper, *SQLite is Enough*, that proposes a single SQLite database file as sufficient for production-quality hybrid retrieval — combining lexical search via FTS5, semantic search via the `sqlite-vec` extension, and Reciprocal Rank Fusion. We reimplemented the pipeline from the paper, ran the same evaluation on two standard BEIR benchmarks, found a toolchain limitation in the `sqlite-vec` 0.1.9 wheel that the paper does not discuss, and ran an independent extension comparing RRF against a weighted linear alternative. The result is a tested, runnable artifact that reproduces the paper's qualitative findings under a smaller embedder."

If you have 60 seconds, add:

> "The architecture is sound. The paper's three-precision configuration menu — float, int8, binary — is not directly reproducible with the `sqlite-vec` build we used, so we recovered it via application-side quantization in NumPy, which is mathematically equivalent. The reproduction confirms hybrid retrieval beats the best single mode on both datasets, int8 quantization loses essentially nothing vs float, and binary quantization degrades by about 10% but still dominates lexical by a factor of 10×."

---

## Section 3: Technical Architecture (for non-implementers)

The system has three runtime layers: the library, the CLI, and the demo. All three share the same data layer: a single SQLite file.

```
                ┌─────────────────────────────────────┐
                │           OneFind library            │
                │  ingest → embed → store → search   │
                └────────────────┬────────────────────┘
                                 │
                                 ▼
   ┌────────────┐  ┌────────────────────────────┐
   │ CLI         │  │  single SQLite file        │
   │ (argparse)  │  │  ├─ documents              │
   └────────────┘  │  ├─ chunks                  │
                  │  ├─ fts (BM25 lexical)      │
   ┌────────────┐  │  ├─ vec_float (cosine KNN)  │
   │ Demo       │  │  └─ schema_meta            │
   │ (FastAPI)  │  └────────────────────────────┘
   └────────────┘
```

**Lexical search** uses SQLite's FTS5 extension. Each document's title and body are stored in a `fts5` virtual table. A query becomes a `MATCH` against this table; SQLite computes BM25 scores using its built-in `bm25()` function. The system retrieves the top-k documents and returns them with highlighted snippets.

**Semantic search** uses the `sqlite-vec` extension. Each document's text is encoded into a 384-dimensional vector by the MiniLM model and stored in a `vec_float` virtual table. A query is encoded the same way and submitted as a KNN MATCH; the system retrieves the top-k most-similar documents. The system also supports int8 and binary precisions, computed application-side in NumPy because the shipped `sqlite-vec` wheel does not accept int8/bit inputs.

**Hybrid search** runs both legs, then fuses the rankings using Reciprocal Rank Fusion (RRF): `score(d) = Σ_i 1 / (60 + rank_i(d))` where the sum is over the two legs. The fused top-k is returned. An optional second-stage rerank rescores the fused pool by full-precision cosine.

**The data model** has five parts: relational tables for documents and chunks, a virtual FTS5 table for lexical search, a virtual sqlite-vec table for vector search, and a key-value metadata table for model and configuration parameters.

---

## Section 4: Each Phase Explained Simply

You do not need to know every line of code. You need to know what each phase accomplished and what trade-offs it made.

### Phase 0 — Foundation

**What we built.** A package skeleton, a six-document specification, a sample corpus, and a `onefind check` command that verifies the runtime environment.

**Key trade-off.** Lightweight dependencies (no PyTorch until you ask for it). The base install is `pip install -e .`; the `[model]` extra pulls in sentence-transformers. This means a fresh machine can verify the environment in seconds, without waiting for a 200 MB download.

### Phase 1 — Lexical Engine

**What we built.** A SQLite schema with documents / chunks / FTS5 / schema_meta tables, a transactional 256-document ingest, BM25 lexical search, deterministic tie-breaking, a hostile-input query sanitizer, and `[highlighted]` snippets.

**Key trade-off.** Whole-document indexing (one chunk per document). This matches the BEIR protocol and the paper's example, but it is not appropriate for application-scale use-cases where documents are long. Chunking is left for future work.

### Phase 2 — Semantic Engine

**What we built.** A SentenceEmbedder wrapper, a float32 vector store, an int8 quantizer, a binary quantizer, and a search dispatcher.

**Key trade-off (and key finding).** The shipped `sqlite-vec` 0.1.9 wheel declares int8 and bit vector columns but rejects all int8/bit inputs. We worked around the limitation by computing int8 and binary rankings in NumPy over the stored float32 vectors. The math is identical to the paper's intent.

### Phase 3 — Hybrid Engine

**What we built.** Reciprocal Rank Fusion with k=60 and deterministic tie-breaking; an optional full-precision cosine rerank of the fused candidate pool.

**Key trade-off.** Standard hybrid uses leg depth `k`. Reranking retrieves 50 candidates per leg by default, fuses the complete union, and then applies pure cosine. Corrected measurements show that this reranker can discard useful lexical-only evidence, so it remains optional.

### Phase 4 — Evaluation Harness

**What we built.** A dataset loader that reads BEIR parquet and qrels TSV files, an 8-configuration runner, ranx-based metric computation, a per-query latency measurement, and a markdown report writer. Two real evaluation runs: SciFact and NFCorpus.

**Key trade-off.** Two datasets instead of the paper's larger scope. Touché and TREC-COVID are out of V1 scope. The built-in registry contains SciFact and NFCorpus; another compatible dataset can be supplied as a local folder. The two built-in datasets are small enough for CPU evaluation and span fact-checking and biomedical retrieval.

### Phase 5 — Review and Extension

**What we built.** A self review pass that recorded what was checked, what changed, and what was intentionally left alone. An independent extension that compared RRF against a weighted linear alternative across `α ∈ {0, 0.1, …, 1.0}`.

**Key trade-off.** The extension is a parameter sweep, not a new algorithm. The contribution is the controlled comparison: linear and RRF are effectively tied, so simplicity determines the default.

### Phase 6 — Demo and Publish

**What we built.** A FastAPI localhost application with stats/search/health endpoints and a packaged HTML/CSS/JavaScript UI. It implements explicit idle/loading/result/empty/no-index/error states, capability-aware controls, safe text rendering, a strict CSP, and reset disabled unless explicitly enabled.

**Key trade-off.** Vanilla JavaScript in the front-end. No build step, no node_modules, no framework. A reviewer can run `onefind serve` and interact with the system immediately.

---

## Section 5: Key Terms Glossary

If you encounter a term you do not recognize, look here first.

- **ADRs (Architectural Decision Records)**: short, dated, immutable records of single engineering decisions. The project has nine ADRs in `docs/02-technical-design.md`.
- **BEIR**: a benchmark for information retrieval (Thakur et al., 2021). We use SciFact and NFCorpus, two of the eighteen BEIR datasets.
- **BM25**: a ranking function for lexical search. Implemented in SQLite's FTS5 extension.
- **Cosine similarity**: the standard similarity measure for embedding vectors. For unit-normalized vectors, cosine similarity equals the dot product.
- **Embedding**: a dense, fixed-dimensional vector representation of a piece of text, produced by a neural model.
- **FTS5**: SQLite's full-text-search extension, included by default in modern CPython builds.
- **Hybrid retrieval**: combining lexical and semantic search into a single result list.
- **MiniLM**: a small, distilled transformer model. `all-MiniLM-L6-v2` is a 22M-parameter variant.
- **nDCG@10**: the primary effectiveness metric in this project. Normalized Discounted Cumulative Gain at rank 10.
- **Reciprocal Rank Fusion (RRF)**: a simple, parameter-light rank-aggregation method. `score(d) = Σ_i 1 / (k + rank_i(d))` with k=60.
- **Rerank**: a second-pass retrieval step that scores the first-pass candidate pool with a more expensive similarity function.
- **SQLite-vec**: a loadable SQLite extension that adds vector similarity search via `vec0` virtual tables.

---

## Section 6: The Five Most Important Decisions (ADRs)

The project has nine ADRs. Five of them are central to the defense. Memorize these.

### ADR-1: Reimplement rather than fork

We chose to reimplement the OneFind pipeline from the paper rather than fork the upstream MIT-licensed code. A fork would have produced a working artifact in days; a reimplementation took weeks. But the reimplementation is what makes the engineering value defensible: every behavior is justified by the paper (or a documented deviation), not inherited as black-box code from a library we did not read.

### ADR-3: MiniLM instead of Qwen3-Embedding-8B

The paper's headline configurations use Qwen3-Embedding-8B (8 billion parameters). We use MiniLM-L6-v2 (22 million parameters) instead. The cost is a uniform shift in absolute nDCG: the architecture is sound, the model just moves the number up or down. The MiniLM gap is documented in the MTEB leaderboard as the expected cost of running on CPU with no GPU.

### ADR-7: sqlite-vec int8/bit workaround

The shipped `sqlite-vec` 0.1.9 wheel declares `int8[n]` and `bit[n]` vector columns but rejects all int8/bit inputs. We computed int8 and binary rankings in NumPy over the stored float32 vectors. The math is identical to the paper's intent. This is the project's most interesting finding and the kind of result only an independent reproduction can produce.

### ADR-8: RRF defaults

RRF k=60 (Cormack et al., 2009 default), leg depth = k, rerank pool = fused candidates. The alpha-sweep extension tested the alternative (weighted linear with min-max normalization) and confirmed that RRF is the right default.

### ADR-9: BEIR acquisition via HuggingFace

The original BEIR data was hosted on Google Drive. We switched to HuggingFace-hosted parquet + sibling qrels TSV repositories. Pyarrow is loaded on-demand via the `[eval]` extra. This decision kept the harness portable and removed a flakiness risk that the PRD had flagged as R1.

---

## Section 7: How to Demo the Project (Step-by-Step)

Practice this on your own machine before the defense.

### 7.1 Build the index (if not already done)

```bash
onefind eval scifact --db data/scifact.db
```

Runtime varies substantially by CPU because embedding 5,183 documents is the dominant cold-start cost. Run it well before the defense and reuse the generated database for the live demo.

### 7.2 Start the server

```bash
onefind serve --db data/scifact.db --port 8080
```

The server prints a message confirming it is listening. Open a browser to `http://127.0.0.1:8080/`.

### 7.3 Demo the UI states

Walk through the following states. Each one is a different part of the design brief.

| State | How to trigger it |
|---|---|
| Empty (no index) | Use a non-existent db path: `onefind serve --db nonexistent.db --port 8080` |
| Empty (index but no query) | Open the UI; the page renders with an empty query box |
| Loading | Type a query and submit; for the first semantic query, loading takes ~1 second |
| Results (lexical) | Query "vitamin C supplementation" with mode = lexical |
| Results (semantic) | Query "does vitamin C help with colds" with mode = semantic |
| Results (hybrid) | Query anything with mode = hybrid |
| No results | Query "asdfqwer nonexistent" |
| Error | Stop the server, then submit a query (returns 503) |

### 7.4 Show the test suite

```bash
pytest
```

Expected: 112 tests pass in about 90 seconds. If the test suite is slow, mention that the model load is the dominant cost and happens once per session.

### 7.5 Show the reports

Open the following files in any text editor or render them in a markdown viewer:

- `benchmarks/reports/env-2026-08-26.md` — environment proof
- `benchmarks/reports/eval-scifact-2026-08-26.md` — SciFact results
- `benchmarks/reports/eval-nfcorpus-2026-08-26.md` — NFCorpus results
- `benchmarks/reports/alpha-sweep-scifact-2026-08-26.md` — alpha-sweep on SciFact
- `benchmarks/reports/alpha-sweep-nfcorpus-2026-08-26.md` — alpha-sweep on NFCorpus
- `benchmarks/reports/review-t09.md` — self review pass

---

## Section 8: Likely Defense Questions and Answers

A complete list is in `VIVA_QUESTIONS.md`. The most likely questions for you to handle are below.

### Q: What does the project do?

> "It reproduces a recent paper, *SQLite is Enough*, that claims a single SQLite database file is enough for production-quality hybrid retrieval. We reimplemented the pipeline, evaluated it on two BEIR benchmarks, found a toolchain limitation the paper does not discuss, and ran an independent alpha-sweep extension."

### Q: Why is your nDCG@10 lower than the paper's?

> "We use a smaller embedder (MiniLM, 22M parameters) instead of the paper's Qwen3-Embedding-8B. The model is 365× smaller, and the absolute nDCG gap is the expected cost of running on CPU. The qualitative findings — hybrid beats single mode, int8 ≈ float, binary degrades ~10% — all reproduce."

### Q: What is the most interesting finding?

> "The shipped `sqlite-vec` 0.1.9 wheel declares int8 and bit vector columns but rejects all int8/bit inputs. The paper's three-precision configuration menu is not directly reproducible with that build. We recovered the configurations via application-side quantization in NumPy. This is the kind of result only an independent reproduction can produce."

### Q: Why did you reimplement instead of fork?

> "A fork would have produced a working artifact in days but would have hidden which parts of the system we actually understood. Reimplementing forced us to justify every behavior from the paper or a documented deviation. It is the strongest demonstration of comprehension for an engineering evaluation."

### Q: How many tests are there?

> "112 tests across 15 modules. They cover environment checks, ingest identities, BM25 direction, model-free fusion math, fake-model vector consistency, native/quantized semantic search, rerank scores and candidate pools, the evaluation harness, gold-query smoke tests, and the packaged FastAPI UI."

### Q: Show me the search query.

> Open `src/onefind/search.py` and walk through the BM25 query, explaining:
> - `-bm25(fts)` makes "higher is better" (semantic uses the same convention)
> - `snippet(fts, -1, '', '', ' … ', 12)` produces private-delimiter highlight segments
> - `ORDER BY score DESC, doc_id ASC` returns strongest matches first and breaks ties deterministically

---

## Section 9: Presentation Division Suggestion

The defense will likely be 15–20 minutes. A reasonable division for 4 people is below. The lead author takes the largest share because they are the most familiar with the implementation. You should all understand the entire project, but each person takes a portion of the presentation.

| Slot | Duration | Owner | Content |
|---|---|---|---|
| 1. Introduction | 2 min | Lead author | The paper, the claim, the gap, the contributions |
| 2. Architecture | 4 min | Member 1 | The data model, the three search modes, the design decisions |
| 3. Results | 4 min | Member 2 | The two BEIR evaluations, the alpha-sweep extension, the comparison with the paper |
| 4. Discussion | 3 min | Member 3 | The ADR-7 finding, the trade-offs, the limitations |
| 5. Demo | 3 min | Lead author | Live demo: index a query, switch modes, show no-results state |
| 6. Q&A | 5–10 min | All | Answer questions; lead author takes the technical ones |

Member 1, 2, 3 are placeholders; assign them to the actual team members.

### What the lead author should be ready to defend alone

- The specific numbers in every report.
- The code for the BM25 query and the int8 quantizer.
- The architecture of the data model.
- The detailed history of how ADR-7 was discovered.

### What each member should be ready to defend alone

**Member 1 (architecture)**
- The data model and why it is structured the way it is.
- The three search modes and their trade-offs.
- The decision to reimplement rather than fork.
- The choice of MiniLM (ADR-3) and its consequences.

**Member 2 (results)**
- The meaning of nDCG@10 and other metrics.
- The interpretation of the SciFact and NFCorpus tables.
- The methodology of the alpha-sweep and what it found.
- The latency numbers and what they imply for production use.

**Member 3 (discussion)**
- The ADR-7 finding in detail: how it was discovered, what it means.
- The trade-offs the project makes.
- The limitations of the system.
- The future-work items and what each would involve.

### What to do if you don't know the answer

- Say "I don't know" or "I would need to look that up". Do not bluff.
- If you can identify the file or section where the answer is, say so: "That would be in `docs/02-technical-design.md` ADR-7; the lead author can speak to it more specifically."
- Defer to the lead author for any question you cannot answer confidently.

---

## Section 10: Practice Schedule

A 5-day preparation plan that should leave you confident for the defense.

### Day 1: Read and understand (90 min)

1. Read this document end to end.
2. Read `README.md`.
3. Skim `FORMAL_PROJECT_REPORT.md` (focus on Executive Summary, §4, §6, §7).
4. Skim `VIVA_QUESTIONS.md` to get a feel for what will be asked.

### Day 2: Run the project (60 min)

1. Clone the repo (or use the existing one).
2. Run the setup steps in `HOW_TO_RUN.md` (Section 1–4).
3. Run the test suite.
4. Run the SciFact evaluation well before the demo; cold CPU embedding is the dominant cost and runtime varies by machine.
5. Start the demo server, run a few queries, stop the server.

### Day 3: Read the code (120 min)

1. Read `src/onefind/store.py` (the data model).
2. Read `src/onefind/search.py` (the search modes).
3. Read `src/onefind/embed.py` (the embedder and quantizers).
4. Read `src/onefind/evaluate.py` (the evaluation harness).
5. Skim the other modules.

### Day 4: Practice defending (90 min)

1. Each team member presents their assigned section (5 min each, total 20 min).
2. The lead author asks each member questions from `VIVA_QUESTIONS.md`.
3. The team discusses any gaps in understanding.
4. Practice the demo runbook from Section 7 above.

### Day 5: Mock defense (60 min)

1. One person plays the role of the evaluator.
2. The evaluator asks questions from `VIVA_QUESTIONS.md` plus any others.
3. The team responds; the lead author takes the technical questions.
4. The team discusses what went well and what to revise.

---

## Section 11: What to Do If You Don't Know an Answer

This is the most important section. The defense will include questions you cannot answer. The way you handle them is what evaluators remember.

### Do not bluff

Bluffing is the worst thing you can do. If an evaluator catches you (and they will), you lose credibility for the rest of the defense. If you don't know, say so.

### Identify the right file

If you can point to the file or section where the answer is, you demonstrate that you know the project's structure even if you don't remember the specific number. For example:

> "I'm not sure of the exact number, but it would be in the SciFact evaluation report. The lead author has the details."

### Defer to the right person

If the question is in someone else's domain, defer:

> "Member 2 took the results section; she would know the specific number."

This is not weakness. It is a sign of a well-coordinated team.

### Use the project as a reference

The repository is the source of truth. If you are allowed to, you can navigate the repo live during the defense to find the answer. This shows that you understand the project structure and that the documentation is honest. (Check with the lead author whether live navigation is allowed.)

### Stay calm

A question you cannot answer is not a failure. It is a moment to demonstrate composure. Say "I don't know, but I can find out" and follow up with an offer to email the answer after the defense.

---

## Section 12: Quick Reference Card

Print this card and bring it to the defense.

```
┌──────────────────────────────────────────────────────────┐
│  PROJECT:  OneFind reproduction                           │
│  PAPER:    arXiv:2608.24060, *SQLite is Enough*          │
│  HEADLINE:  hybrid nDCG@10 = 0.6568 on SciFact          │
│                                                         │
│  COMMANDS                                                │
│    onefind check [--full]                                │
│    onefind index <path> --db X --embed                    │
│    onefind search "Q" --db X --mode {lex|sem|hyb}        │
│    onefind eval <dataset> --db X                          │
│    onefind sweep-alpha <dataset> --db X                   │
│    onefind smoke --db X --queries gold.jsonl              │
│    onefind serve --db X --port 8080                       │
│    pytest                                                │
│                                                         │
│  ADRs to remember                                        │
│    1: reimplement not fork                              │
│    3: MiniLM not Qwen3-8B                               │
│    7: sqlite-vec int8/bit workaround                    │
│    8: RRF k=60                                          │
│    9: BEIR via HF parquet                               │
│                                                         │
│  KEY NUMBERS                                             │
│    SciFact hybrid float  = 0.6568                       │
│    NFCorpus hybrid float = 0.3466                       │
│    SciFact int8 = 0.6465; float = 0.6451                │
│    Linear and RRF differ by only 0.0004                 │
│                                                         │
│  FILES TO REMEMBER                                       │
│    docs/02 ADR records                                  │
│    docs/06 engineering plan                             │
│    src/onefind/{store,search,embed,evaluate,serve}.py    │
│    benchmarks/reports/*.md                              │
│                                                         │
│  IF ASKED "I don't know":                               │
│    1. Don't bluff                                       │
│    2. Identify the right file/section                   │
│    3. Defer to the right team member                    │
│    4. Offer to follow up after the defense              │
└──────────────────────────────────────────────────────────┘
```

---

## Section 13: Final Checklist Before the Defense

Use this list on the day of the defense.

- [ ] Laptop fully charged; power cable packed.
- [ ] Project repo cloned and working directory ready.
- [ ] `pip install -e ".[model,eval,serve]"` already run (no install delays).
- [ ] `onefind check` passes (or you have a story about why it doesn't).
- [ ] `data/scifact.db` is built (no need to re-run the full eval on the day).
- [ ] `data/nfcorpus.db` is built.
- [ ] `onefind serve` starts cleanly on port 8080.
- [ ] `pytest` passes.
- [ ] Browser bookmarked to `http://127.0.0.1:8080/`.
- [ ] Markdown reports open in a viewer for quick reference.
- [ ] This document and `VIVA_QUESTIONS.md` are accessible (printed or on a second device).
- [ ] Each team member has practiced their assigned section.
- [ ] Each team member can answer the 5 questions from Section 8.
- [ ] You have a working understanding of every other section.

---

## Closing Note

The project is solid. The lead author did the implementation with care; the documentation is honest; the deviation records are explicit. The defense is a communication exercise, not a knowledge test. The job of the team is to communicate clearly and to support the lead author where the lead author's expertise is needed.

Read this document. Run the project. Practice the questions. Trust the lead author. You are ready.

Good luck.
