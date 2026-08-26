# Viva Questions — Anticipated Defense Questions and Model Answers

This document is a study guide for the project defense (viva voce). It lists the questions most likely to be asked, organized by category, with model answers drawn from the project documentation. Every answer is grounded in a specific file, commit, or section in the project so you can cross-reference.

**How to use this document.** Read each question once. Cover the answer and try to answer it in your own words. If you cannot, mark the question and return to it. The most important questions for the defense are marked with a 🟢. The traps (questions you should not overstate) are marked with a 🔴.

---

## Section 1: Project Overview (warm-up questions)

### Q1.1 What is the project about, in two sentences?

**Model answer.** The project reproduces the OneFind system proposed in arXiv:2608.24060, which delivers lexical, semantic, and hybrid information retrieval from a single SQLite database file. We reimplemented the pipeline, evaluated every configuration on two BEIR datasets, and conducted an independent alpha-sweep extension.

**Ground truth**: `README.md` (one-paragraph description); `FORMAL_PROJECT_REPORT.md` (Executive Summary).

### Q1.2 Why did you pick this paper?

**Model answer.** The paper's central claim — that a single SQLite file is sufficient for production-quality hybrid retrieval — is counter-intuitive and operationally significant. It is recent (2026), the authors released MIT-licensed code, and the architecture is interesting enough to study in depth. A reproduction is therefore natural: it tests the claim, surfaces any toolchain limitations, and produces a runnable artifact.

**Why this answer works**: it shows motivation (not "the guide told me to") and frames the project as a research contribution, not a code exercise.

### Q1.3 What is the gap you are trying to fill?

**Model answer.** Three gaps:
1. The paper's toolchain (`sqlite-vec` 0.1.9) has a limitation not discussed in the paper — int8 and bit vector columns are declared but not functional. An independent reproduction surfaces this.
2. The paper's default fusion is RRF; the natural question of whether tuned linear fusion is competitive is not addressed in the paper. Our extension answers it.
3. The paper's evaluation focuses on a large embedder (Qwen3-Embedding-8B); for a CPU-only budget, the architecture's behavior under a smaller model is unmeasured. We measure it.

### Q1.4 What are the contributions of the project?

**Model answer.** Four contributions, in order of importance:
1. A complete, tested, runnable reproduction of the OneFind pipeline in 12 clean commits.
2. A documented, empirically verified limitation in the shipped `sqlite-vec` 0.1.9 build (ADR-7), with a working application-side workaround that preserves the paper's mathematical intent.
3. An independent extension experiment (alpha sweep) that compares RRF against a weighted linear alternative, producing a defensible conclusion that RRF is more robust across datasets.
4. A six-document specification system that demonstrates engineering discipline beyond the code.

**Source**: `FORMAL_PROJECT_REPORT.md`, §1.4.

---

## Section 2: Technical Architecture (interviewer wants to test understanding)

### 🟢 Q2.1 Explain the overall architecture.

**Model answer.** Three layers:
- **Library** (`src/OneFind/`) — pure-Python modules for ingest, embedding, storage, search, evaluation, and serving.
- **CLI** (`src/OneFind/cli.py`) — argparse-based subcommand dispatch with central error handling that maps typed exceptions to exit codes.
- **Demo** (`src/OneFind/serve.py` + `demo/index.html`) — a FastAPI backend with three JSON endpoints plus a static single-page HTML application that implements every UI state specified in the design brief.

The data layer is a single SQLite file containing relational tables (documents, chunks, schema_meta), the FTS5 virtual table for lexical search, and the `vec_float` virtual table for vector search. All retrieval happens against this one file.

**Source**: `FORMAL_PROJECT_REPORT.md`, §4.1; `docs/02-technical-design.md` ADR-2.

### 🟢 Q2.2 What does the data model look like?

**Model answer.** Five objects in the database:
1. `documents` — relational table with `doc_id`, `title`, `body`, `source`, `meta_json`, `created_at`, `updated_at`.
2. `chunks` — relational table with stable `row_id` (autoincrement), `doc_id` foreign key, `seq`, and `text`. V1 stores one row per document (whole-doc protocol, ADR-4).
3. `fts` — FTS5 virtual table with `doc_id UNINDEXED`, `title`, `body`, `tokenize='porter unicode61'`. BM25 ranking via `bm25()` function.
4. `vec_float` — sqlite-vec virtual table with `embedding float[384] distance_metric=cosine`. Cosine KNN via `MATCH ... AND k = ?`.
5. `schema_meta` — key/value table for model name, dimension, int8 scale, schema version.

A subtle but important detail: chunk `row_id` is stable across re-indexes via `INSERT ... ON CONFLICT ... DO UPDATE ... RETURNING row_id`, so the `vec_float` table keys off chunks without orphaning rows on re-ingest.

**Source**: `docs/05-backend-design.md`; `src/OneFind/store.py`.

### Q2.3 How does the search pipeline work end-to-end?

**Model answer.** For a lexical search:
1. Sanitize the query by quoting every whitespace token (dropping embedded double quotes) to defuse FTS5 syntax operators.
2. Execute `SELECT doc_id, title, -bm25(fts), snippet(fts, -1, '[', ']', ' … ', 12) FROM fts WHERE fts MATCH ? ORDER BY score, doc_id LIMIT ?`.
3. The `-bm25()` negation makes "higher is better" (consistent with semantic), and the `doc_id` tie-break ensures determinism.

For a semantic search at a given precision:
1. Encode the query with the same embedder used at index time.
2. Serialize the query vector to the precision's native format (`serialize_float32` for float, quantized bytes for int8, packed bits for binary).
3. Execute the KNN MATCH against the corresponding vec table, JOIN chunks and documents for `doc_id` and `title`.
4. Convert distance to similarity: `score = 1 - distance` for float/int8 (cosine); `score = -hamming` for binary.

For a hybrid search:
1. Run both legs at depth `k`.
2. Fuse with RRF (`Σ 1 / (60 + rank)`) or linear (`α * sem + (1-α) * lex` after min-max normalization).
3. Optionally rerank the fused pool by full-precision cosine.

**Source**: `src/OneFind/search.py`; `docs/05-backend-design.md`.

### Q2.4 What is Reciprocal Rank Fusion and why k=60?

**Model answer.** RRF, from Cormack, Clarke, and Büttcher (2009), is `score(d) = Σ_i 1 / (k + rank_i(d))` summed over the rankings being fused. The rank starts at 1; the constant `k` dampens the influence of high ranks. The original paper recommends `k=60` as the default; we adopt that default in ADR-8.

RRF is simple, parameter-light, and surprisingly robust. It does not require score normalization across legs (which is its main advantage over linear fusion), and it is the fusion method used by the original OneFind paper.

**Source**: `docs/02-technical-design.md` ADR-8; Cormack et al. (2009).

### 🟢 Q2.5 What is sqlite-vec and how does it differ from a dedicated vector database?

**Model answer.** `sqlite-vec` is a loadable SQLite extension that adds vector similarity search via a `vec0` virtual table. The vector column accepts `float[n]`, `int8[n]`, or `bit[n]` arrays, and the SQL `MATCH` operator returns KNN results ordered by distance.

The difference from a dedicated vector database (Pinecone, Weaviate, Qdrant) is operational: sqlite-vec stores vectors in the same SQLite file as the relational data and the FTS5 index, so there is no separate service to run, no separate configuration, and the entire index is one file. The trade-off is ceiling: dedicated vector databases are optimized for billions of vectors; sqlite-vec is appropriate up to maybe 100K–1M vectors, depending on the application.

**Source**: `docs/02-technical-design.md` ADR-2; `https://github.com/asg017/sqlite-vec`.

---

## Section 3: ADRs and Design Decisions (interviewer wants to test judgment)

### 🟢 Q3.1 What is an ADR? Why did you write them?

**Model answer.** An Architectural Decision Record is a short, dated, immutable record of a single engineering decision: context, options considered, choice, reason, consequences, and revisit trigger. ADRs prevent the "why did we do this" question from being unanswerable six months later. They are also the natural place to record deviations from a paper or from common practice, which is what makes them useful in a reproduction project.

I wrote nine ADRs in this project (ADR-1 through ADR-9; ADR-6 was skipped as a numbering gap when a planned topic was absorbed into ADR-7). The most important ones are ADR-3 (model downscaling), ADR-7 (sqlite-vec int8/bit workaround), ADR-8 (hybrid RRF defaults), and ADR-9 (BEIR acquisition).

**Source**: `docs/02-technical-design.md`.

### 🟢 Q3.2 Why MiniLM instead of the paper's Qwen3-Embedding-8B?

**Model answer.** The paper's headline configurations use Qwen3-Embedding-8B — an 8-billion-parameter model. That is impractical on CPU: a single embedding forward pass takes seconds, and the full SciFact corpus (5,183 docs) would take hours. We use `all-MiniLM-L6-v2` (22M parameters, 384-dim) instead, which is CPU-friendly and finishes the SciFact embedding pass in 2–3 minutes.

The deviation is documented in ADR-3. The cost is a uniform shift in absolute nDCG: our SciFact hybrid at 0.6568 versus the paper's ~0.73, a gap explained by the model size difference (365× fewer parameters). The directional findings — hybrid beats single mode, int8 ≈ float, binary degrades by ~10% — all survive, which is the more important result.

**Source**: `docs/02-technical-design.md` ADR-3; `FORMAL_PROJECT_REPORT.md` §6.4.

### 🟢 Q3.3 What is ADR-7 and why is it important?

**Model answer.** ADR-7 is the project's most significant finding. The paper attributes its three-precision configuration menu (float, int8, binary) to `sqlite-vec`. The shipped `sqlite-vec` 0.1.9 Python wheel *declares* `int8[n]` and `bit[n]` vector columns, but **rejects all int8/bit inputs** for both INSERT and MATCH query vectors, across every input format I tested (raw BLOB, `serialize_int8`, JSON arrays, float-format BLOBs). The error message is "expected to be of type int8/bit, but a float32 vector was provided" for all of them.

This means the paper's int8 and binary configurations are not directly reproducible with the build we used. We recovered them by computing int8 (global-scale scalar quantization) and binary (sign-bit packing, Hamming distance) rankings in NumPy over the stored float32 vectors. The math is identical to the paper's intent; only the implementation locus is different (Python/numpy instead of C extension).

The paper's authors may or may not have known about this limitation depending on which `sqlite-vec` build they used. If they used a newer build (the project on GitHub is more recent than the 0.1.9 PyPI wheel), the int8/bit path may work. If they used 0.1.9, they may have run into the same issue without writing about it.

The importance of the finding is that it is the kind of result only an independent reproduction can produce. The reproduction tests the paper's claims and surfaces toolchain realities that the paper itself does not see.

**Source**: `docs/02-technical-design.md` ADR-7; `FORMAL_PROJECT_REPORT.md` §6.2.

### Q3.4 Why did you reimplement from the paper rather than fork the upstream code?

**Model answer.** A fork would have produced a working artifact in days but would have hidden which parts of the system I actually understood. Reimplementing forced me to justify every behavior from the paper (or a documented deviation), which is the strongest possible demonstration of comprehension for an engineering evaluation. It also surfaced the ADR-7 finding, which a fork would have inherited as broken behavior rather than an investigation.

**Source**: `docs/02-technical-design.md` ADR-1; `FORMAL_PROJECT_REPORT.md` §3.2.

### Q3.5 Why RRF as the default fusion rather than linear?

**Model answer.** The paper uses RRF; we adopt the same default in ADR-8. The alpha-sweep extension tested the alternative (linear with min-max normalization) and found that RRF is more robust across datasets. On SciFact the two are equivalent (flat plateau α=0.1..0.5); on NFCorpus RRF wins by 0.002 nDCG. So the paper's choice is well-founded.

Practically, RRF also has the advantage of requiring no per-leg score normalization, which makes it easier to add a new ranking signal in the future.

**Source**: `docs/02-technical-design.md` ADR-8; `benchmarks/reports/alpha-sweep-nfcorpus-2026-08-26.md`.

### Q3.6 Why the six-document specification system?

**Model answer.** Because every design decision deserves a name and a place. The six documents (PRD, Technical Design, App Flow, UI/UX Brief, Backend Design, Engineering Plan) force explicit acceptance criteria for every requirement, which surfaces edge cases that pure coding misses. In this project the spec caught: the FTS5 `bm25()` schema-qualified-name gotcha (Phase 0), the chunk-row-id stability requirement for vector-table referential integrity (Phase 2), and the choice between measured and synthetic latency (Phase 4).

The six-doc system is also the project's audit trail. Every major decision has a citation in the form of a section ID and a commit hash.

**Source**: `docs/01-prd.md` … `docs/07-references.md`; `FORMAL_PROJECT_REPORT.md` §3.1.

---

## Section 4: Implementation Details (interviewer wants to test depth)

### Q4.1 How do you ensure the search results are deterministic?

**Model answer.** Determinism is enforced at three levels:
1. **Sort tie-breaks**: every `ORDER BY` clause in the codebase includes `doc_id ASC` as a secondary key. With deterministic sort and no randomness in the model, the same query returns the same results.
2. **No randomness in encoding**: sentence-transformers models are deterministic in evaluation mode. There is no dropout at inference, no sampling. The same input produces the same vector.
3. **Seeded tests**: the test suite asserts byte-identical effectiveness numbers across two independent runs of the mini-BEIR fixture.

Latency, of course, is not deterministic and is reported as a p50/p95 distribution rather than as exact numbers.

**Source**: `tests/test_eval.py`; `src/OneFind/search.py`.

### Q4.2 What happens if I re-index the same documents?

**Model answer.** The system is idempotent. Re-indexing the same documents with the same `doc_id` produces an upsert (via SQLite's `ON CONFLICT ... DO UPDATE`): the existing row is updated, the FTS5 row is deleted-and-reinserted, the chunk row is preserved (with the same stable `row_id` thanks to the upsert), and the vector row is deleted-and-reinserted. The final state is identical to the pre-re-index state except that `updated_at` is refreshed.

This was important to verify because the FTS5 and vec tables need to be re-synced with the relational tables on every re-index, and a subtle bug here would orphan rows.

**Source**: `src/OneFind/store.py` `_flush`; `tests/test_ingest.py::test_reindex_is_idempotent`.

### Q4.3 What happens if a query contains FTS5 syntax like `AND` or `OR`?

**Model answer.** Hostile query syntax is neutralized by a sanitizer that quotes every whitespace token with double quotes, dropping any embedded double quotes in the input. The resulting query is a sequence of quoted tokens that FTS5 interprets as implicit ANDs. Operators like `AND`, `OR`, `NOT`, `(`, `)`, `:`, `*` in user input are thus treated as literal characters, not as syntax.

This was tested explicitly in `test_hostile_query_syntax_does_not_crash` and `test_cli_no_match_exit_zero` with hostile input.

**Source**: `src/OneFind/search.py::sanitize_fts_query`; `tests/test_search_lexical.py`.

### Q4.4 How do you handle a very large corpus?

**Model answer.** Several scaling concerns are addressed:
- **Ingest is batched**: 256 documents per transaction, with the connection committing after each batch. A crash mid-ingest leaves earlier batches committed and the database openable.
- **Vector queries are read-from-disk on first call and cached in memory** for the lifetime of the Index instance. The cache is invalidated on writes.
- **In-process NumPy distance computation** is fast up to about 100K documents. Beyond that, a dedicated vector database would be more appropriate.
- **For V1 scope** (5K–10K documents), the architecture comfortably fits in CPU memory and the cache is ~7 MB.

The harness supports Touché (382K) and TREC-COVID (171K) via the BEIR registry, but they are out of V1 scope for evaluation.

**Source**: `src/OneFind/store.py`; `docs/02-technical-design.md` ADR-2.

### Q4.5 How is the web demo secured?

**Model answer.** Three layers of security:
- **Bind to 127.0.0.1**: the server is only reachable from the local machine. No network exposure.
- **No authentication**: this is intentional, because there is no sensitive data — the index is a research artifact. If the demo were to be exposed to a network, auth would be the first thing to add.
- **Reset requires a token**: the `/api/reset` endpoint requires a `confirm_token` that is generated at startup and printed to the console. The token must be passed in the request body.

There is no file-traversal risk because the index path is fixed at startup. There is no SQL injection risk because all queries use parameterised statements via sqlite3's `?` placeholders.

**Source**: `src/OneFind/serve.py`; `docs/02-technical-design.md` security section.

### Q4.6 How would you add a new retrieval mode (e.g., cross-encoder rerank)?

**Model answer.** The code is structured to make this straightforward:
1. Implement a function in `search.py` that takes the existing fused candidate pool and returns a re-ranked list of `Hit` objects.
2. Add a flag (e.g., `--cross-encoder`) to the `hybrid_search` function and to the CLI parser.
3. Add a unit test that asserts the new mode returns k results and the new mode is wired correctly through the dispatcher.
4. Add an integration test in `tests/test_hybrid.py` that asserts the new mode produces sensible rankings on a fixture.

The cross-encoder rerank is actually listed as future work in `FORMAL_PROJECT_REPORT.md` §9 because it requires a separate cross-encoder model and the additional infrastructure cost was not justified for V1.

---

## Section 5: Results and Evaluation (interviewer wants to verify the numbers)

### 🟢 Q5.1 What was the headline result?

**Model answer.** On SciFact, our hybrid RRF (float) achieves nDCG@10 = 0.6568, beating the best single mode (semantic float at 0.6451) by 0.012. On NFCorpus, hybrid RRF (float) achieves nDCG@10 = 0.3249, beating semantic float at 0.3167 by 0.008. Hybrid + rerank is essentially identical to hybrid alone (0.6548 vs 0.6568 on SciFact; 0.3260 vs 0.3249 on NFCorpus), which reproduces the paper's observation that rerank is often neutral on these collections.

**Source**: `benchmarks/reports/eval-scifact-2026-08-26.md`; `FORMAL_PROJECT_REPORT.md` §6.1.

### Q5.2 How do the absolute numbers compare to the paper's?

**Model answer.** Our SciFact hybrid at 0.6568 is below the paper's ~0.73 hybrid. The gap is uniform across configurations and is fully explained by the model size difference (ADR-3): we use `all-MiniLM-L6-v2` (22M parameters, 384-dim); the paper uses `Qwen3-Embedding-8B` (8B parameters, 4096-dim). The MiniLM-vs-Qwen3 gap is consistent with the MTEB leaderboard on similar tasks; it is the expected cost of running on CPU with no GPU budget.

The directional findings (qualitative claims) reproduce faithfully. The absolute numbers do not, by design.

**Source**: `FORMAL_PROJECT_REPORT.md` §6.4.

### Q5.3 What did the alpha-sweep extension find?

**Model answer.** The alpha-sweep tested weighted linear fusion (with min-max normalization) against the default RRF across `α ∈ {0, 0.1, …, 1.0}` on both datasets. On SciFact, the linear plateau at `α ∈ [0.1, 0.5]` ties RRF to 4 decimal places (both 0.657). On NFCorpus, RRF beats the best linear (α=0.6, nDCG=0.3229) by 0.002 nDCG. The conclusion: RRF's rank-position aggregation is more robust across heterogeneous datasets, and the paper's choice is well-founded.

**Source**: `benchmarks/reports/alpha-sweep-scifact-2026-08-26.md`; `benchmarks/reports/alpha-sweep-nfcorpus-2026-08-26.md`; `FORMAL_PROJECT_REPORT.md` §6.2.

### Q5.4 What are the latency numbers?

**Model answer.** p95 query latency on SciFact (5,183 docs):
- Lexical: 2.3 ms
- Semantic float: 146 ms
- Semantic int8: 213 ms
- Semantic binary: 170 ms
- Hybrid float: 156 ms
- Hybrid + rerank: 276 ms

The bottleneck is query encoding (~50–80 ms) and the in-process NumPy distance computation. These numbers are consistent with the paper's claim that the architecture scales to small-to-medium corpora on commodity hardware. For larger corpora or strict latency requirements, the literature points to GPU inference, batched encoding, or a dedicated vector service.

**Source**: `benchmarks/reports/eval-scifact-2026-08-26.md`; `FORMAL_PROJECT_REPORT.md` §6.3.

---

## Section 6: Deviations and Limitations (interviewer probes for honesty)

### 🔴 Q6.1 Did you reproduce the paper exactly?

**Model answer.** No, and the project is honest about this. Three documented deviations:
- **ADR-3**: model downscaling from Qwen3-Embedding-8B to MiniLM-L6-v2 (CPU budget).
- **ADR-7**: int8/binary computed application-side in NumPy because the shipped `sqlite-vec` 0.1.9 wheel rejects int8/bit inputs.
- **Dataset scope**: SciFact + NFCorpus only; Touché and TREC-COVID out of V1 scope.

A literal reproduction was not the goal. The goal was a defensible reproduction with documented, justified deviations.

**Source**: `docs/02-technical-design.md` ADRs; `FORMAL_PROJECT_REPORT.md` §6.3.

### 🔴 Q6.2 Why is your nDCG@10 lower than the paper's?

**Model answer.** Model size. The paper uses Qwen3-Embedding-8B (8B parameters, 4096-dim). We use MiniLM-L6-v2 (22M parameters, 384-dim). The model is 365× smaller. The MTEB leaderboard shows a comparable gap between these two models on similar tasks, so the result is the expected cost of running on CPU with no GPU.

The directional findings (qualitative claims) reproduce faithfully, which is the more important result: it shows that the architecture, not the model, is what produces the qualitative claims.

**Source**: `docs/02-technical-design.md` ADR-3; `FORMAL_PROJECT_REPORT.md` §6.4.

### Q6.3 What is the biggest limitation of the project?

**Model answer.** Three candidates:
1. **Model scale** — MiniLM cannot match Qwen3-8B's absolute quality. A GPU or a more capable embedder would close the absolute gap.
2. **Dataset scope** — only two BEIR datasets; the paper evaluates on eight. Touché and TREC-COVID would test the engine at higher scale.
3. **sqlite-vec int8/bit** — the application-side workaround is mathematically equivalent but does not deliver the latency story the paper claims for the native implementation. A future `sqlite-vec` release may fix the limitation and let us flip the path back to native.

**Source**: `FORMAL_PROJECT_REPORT.md` §6.3, §9.

### Q6.4 Is the system production-ready?

**Model answer.** For small-to-medium corpora on CPU (≤100K documents, no strict latency requirements, single-user or low-traffic), yes. For larger corpora, multi-user workloads, or strict latency, the architecture is a starting point but the implementation would need hardening, batching, GPU inference, and proper authentication/authorization. The project is a research artifact and a reproduction, not a production deployment.

**Source**: `FORMAL_PROJECT_REPORT.md` §6.3.

---

## Section 7: "What Would You Do Differently" (interviewer wants to test reflection)

### Q7.1 If you were starting over, what would you change?

**Model answer.** Three things:
1. **Pin dependencies** more strictly. We use loose version constraints in `pyproject.toml` to maximize reproducibility across machines, but a stricter pin (e.g., `==`) would prevent the `sqlite-vec` version drift that produced the ADR-7 surprise. A pre-flight check on dependency versions in the CI pipeline would be valuable.
2. **Start the evaluation harness earlier** in the project. We wrote the harness in Phase 4, but having even a smoke harness in Phase 1 would have caught the quantization behavior earlier and shaped the design.
3. **Add a CI environment that runs against the model extra** rather than only the lexical-only tests. Currently CI runs without the model, which is faster but skips the most behavior-rich tests. A matrix job with the model extra would be more comprehensive.

### Q7.2 What is the next experiment you would run?

**Model answer.** Three candidates, in order of expected yield:
1. **Cross-encoder reranker on the hybrid top-50**. The current rerank is full-precision cosine; a cross-encoder (e.g., `cross-encoder/ms-marco-MiniLM-L-6-v2`) would be a different quality lever. Worth running to see whether the rerank-tie pattern in our extension generalizes or breaks.
2. **Matryoshka dimension study**. Re-embed the corpus with 64/128/192-dim slices of MiniLM and measure nDCG vs cost. A different axis of the "quantization doesn't hurt" claim.
3. **Scale to Touché (382K documents)** to test the engine at 50× the current scale and measure how the latency story holds up.

**Source**: `FORMAL_PROJECT_REPORT.md` §9.

### Q7.3 What did you learn from the project?

**Model answer.** Four things (elaborated in `FORMAL_PROJECT_REPORT.md` §7):
1. **A shipped library can claim capabilities the binary does not deliver** — empirical probing is the only honest test of a library's capabilities (ADR-7 finding).
2. **Hybrid fusion math is subtler than the formula suggests** — RRF and linear fusion tie on SciFact but RRF wins on NFCorpus; the result depends on the legs' score distributions.
3. **Architecture is more important than model scale for this class of system** — a 384-dim embedder reproduces all the paper's directional findings.
4. **A spec-driven workflow is a real engineering tool** — the six-doc system caught bugs that a code-first workflow would have shipped.

---

## Section 8: Specific Code Questions (interviewer wants to test code literacy)

### Q8.1 Show me the FTS5 query and explain it.

**Model answer.** From `src/OneFind/search.py`:

```sql
SELECT doc_id, title,
       -bm25(fts) AS score,
       snippet(fts, -1, '[', ']', ' … ', 12) AS snip
FROM fts
WHERE fts MATCH ?
ORDER BY score ASC, doc_id ASC
LIMIT ?
```

- `-bm25(fts)` negates the BM25 score so that higher means more relevant (consistent with semantic scores, which are also "higher is better").
- `snippet(fts, -1, '[', ']', ' … ', 12)` produces a 12-token snippet with the matched terms wrapped in `[...]` brackets. The `-1` tells FTS5 to pick the best matching column (title or body).
- `ORDER BY score ASC, doc_id ASC` orders by relevance (with `score = -bm25` ascending = descending relevance) with deterministic tie-break on document ID.

### Q8.2 Why is the connection using `check_same_thread=False`?

**Model answer.** FastAPI dispatches request handlers in a thread pool. The SQLite connection is created in the main thread (during FastAPI's `on_event("startup")`) but is then used in the worker threads that handle requests. Python's sqlite3 module by default refuses cross-thread connection use, so the default `check_same_thread=True` would raise. `check_same_thread=False` is safe for our use case because SQLite has its own locking mechanism that handles concurrent access correctly.

**Source**: `src/OneFind/store.py::Index.open`; `src/OneFind/serve.py`.

### Q8.3 Walk me through what happens when I run `OneFind eval scifact`.

**Model answer.** Step by step:
1. CLI parses arguments, calls `cmd_eval`.
2. `cmd_eval` calls `evaluate.run_eval`.
3. `run_eval` calls `datasets.load_local_or_registry("scifact")` which downloads the BEIR parquet + qrels TSV files to `data/scifact/` (first run only; cached after).
4. `run_eval` loads the corpus, queries, and qrels.
5. `run_eval` opens the SQLite database at `data/scifact.db`, attaches the embedder, and adds all 5,183 documents to the index. Embedding takes ~2–3 minutes.
6. For each of 8 configurations, `run_eval` calls `run_configuration` which loops over all 300 judged queries, times each, and builds a `run` dict mapping query-id to `{doc_id: score}`.
7. For each configuration, `ranx.evaluate` computes AP, RR, P@10, and nDCG@10.
8. `run_eval` writes a markdown report to `benchmarks/reports/eval-scifact-YYYY-MM-DD.md`.
9. The CLI prints the report path and exits 0.

**Source**: `src/OneFind/evaluate.py`; `src/OneFind/cli.py`.

---

## Section 9: Defense Traps (questions to handle carefully)

### 🔴 Q9.1 "Did you invent anything new?"

**Wrong answer**: "Yes, the alpha-sweep extension is new." (This overstates; the alpha-sweep is a parameter sweep, not a new method.)

**Right answer**: "The project is a reproduction, not a new method. The contribution is a faithful, documented, tested reproduction plus a parameter-sweep extension that produces a defensible conclusion. The ADR-7 finding is the most original result; it is a toolchain observation, not a new algorithm."

### 🔴 Q9.2 "Is your system better than the paper's?"

**Wrong answer**: "Yes, we use MiniLM and it's faster." (This is a non-sequitur; the paper's model is intentionally larger for higher absolute quality.)

**Right answer**: "Our system is different from the paper's in three documented ways (ADR-3, ADR-7, dataset scope). The trade-offs are: lower absolute quality (because of MiniLM), but the same architecture on commodity hardware, with an application-side workaround for the `sqlite-vec` int8/bit limitation. We do not claim our system is 'better'; we claim our reproduction is faithful and our deviations are documented."

### 🔴 Q9.3 "How long did this take?"

**Wrong answer**: "Six weeks" or any specific claim you cannot defend.

**Right answer**: "I worked on it across [X weeks/months] in roughly [Y hours/week]. The commit history records the sequence; you can see the phase-by-phase progression. The 12 commits cover the full project from environment proof to demo."

### 🔴 Q9.4 "What would you do if you had more time?"

**Wrong answer**: "Everything" (vague) or "make it production-ready" (vague).

**Right answer**: "I would run the cross-encoder reranker experiment (future work item), scale to Touché to test the engine at higher scale, and add a public API spec (OpenAPI) to the FastAPI demo. In that order."

### 🔴 Q9.5 "What's the worst part of the project?"

**Wrong answer**: Nothing (overstates) or "the encoding pipeline" (vague).

**Right answer**: "The hardest part was diagnosing the `sqlite-vec` int8/bit limitation. The error message ('expected to be of type int8/bit, but a float32 vector was provided') is misleading because every input I tried produced the same message. I had to systematically test every input format I could think of (raw BLOB, `serialize_int8`, JSON arrays, float-format BLOBs) before concluding that the column was non-functional. That took about a day of focused debugging. The result is documented in ADR-7 and is the project's most interesting finding."

---

## Section 10: Quick-Fire Reference

If you have 5 minutes before the defense, scan these:

| Question | One-line answer |
|---|---|
| What does the system do? | Lexical + semantic + hybrid search from one SQLite file. |
| How is the paper reproduced? | Reimplemented from scratch, evaluated on two BEIR datasets, ran an alpha-sweep extension. |
| What's the most important deviation? | ADR-3 (MiniLM not Qwen3-8B) and ADR-7 (sqlite-vec int8/bit workaround). |
| What's the headline result? | Hybrid nDCG@10 beats best single mode; 0.6568 on SciFact, 0.3249 on NFCorpus. |
| What's the extension finding? | RRF and linear fusion tie on SciFact; RRF wins by 0.002 on NFCorpus. |
| What's the test count? | 67 tests across 9 modules. |
| What's the commit count? | 12 commits, linear history, one per task or phase. |
| What's the demo? | FastAPI + single-page HTML, `OneFind serve --db data/scifact.db --port 8080`. |
| What's the bottleneck? | Query encoding (~80 ms) and NumPy distance computation (~50–100 ms) on CPU. |
| What would you do next? | Cross-encoder rerank, matryoshka study, scale to Touché. |

---

Good luck with the defense. The project is solid; the defense is about communicating why.
