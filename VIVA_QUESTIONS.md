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
1. A complete, independently hardened reproduction with a linear phase history and a tested v1.1 correction pass.
2. A documented, empirically verified limitation in the shipped `sqlite-vec` 0.1.9 build (ADR-7), with a working application-side workaround that preserves the paper's mathematical intent.
3. An independent alpha-sweep extension comparing RRF with weighted linear fusion; the corrected runs are effectively tied, supporting RRF as the simpler default rather than claiming a universal winner.
4. A six-document specification system that demonstrates engineering discipline beyond the code.

**Source**: `FORMAL_PROJECT_REPORT.md`, §1.4.

---

## Section 2: Technical Architecture (interviewer wants to test understanding)

### 🟢 Q2.1 Explain the overall architecture.

**Model answer.** Three layers:
- **Library** (`src/onefind/`) — modules for ingest, embedding, storage, search, evaluation, and serving.
- **CLI** (`src/onefind/cli.py`) — lowercase argparse command dispatch with typed exit codes.
- **Demo** (`src/onefind/serve.py` + `src/onefind/static/`) — a FastAPI backend plus packaged HTML/CSS/JavaScript implementing the complete state map without a build step.

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

**Source**: `docs/05-backend-design.md`; `src/onefind/store.py`.

### Q2.3 How does the search pipeline work end-to-end?

**Model answer.** For a lexical search:
1. Sanitize the query by quoting every whitespace token (dropping embedded double quotes) to defuse FTS5 syntax operators.
2. Execute FTS5 MATCH with `-bm25(fts) AS score` and `ORDER BY score DESC, doc_id ASC`; private-use delimiters mark safe highlight ranges.
3. Negating BM25 makes higher scores better, so descending order is required; the document-ID tie-break ensures determinism.

For semantic search:
1. Encode the query once with the same CPU embedder used at index time.
2. Float mode executes sqlite-vec cosine KNN against `vec_float` and converts distance to `1 - distance`.
3. Int8 mode applies the fixed normalized-vector scale in bounded blocks and computes quantized cosine.
4. Binary mode compares sign vectors in bounded blocks and returns negative Hamming distance.

For hybrid search:
1. Run both legs at depth `k`, or at `candidate_depth` per leg when reranking.
2. Fuse with RRF (`Σ 1 / (60 + rank)`) or normalized linear scores. Alpha endpoints are exact leg passthroughs.
3. Optionally rerank the complete fused union with one query encoding and return refreshed cosine scores.

**Source**: `src/onefind/search.py`; `docs/05-backend-design.md`.

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

**Model answer.** The paper's headline configurations use Qwen3-Embedding-8B, which is impractical for a free CPU-only reproduction. We use `all-MiniLM-L6-v2` with 22M parameters and 384 dimensions. It remains free and local, but cold corpus embedding can still take tens of minutes depending on the laptop; reports include actual latency rather than assuming a fixed duration.

The deviation is documented in ADR-3. Our SciFact hybrid is 0.6568 versus roughly 0.73 in the paper. The model-size difference is the most plausible major cause, but this project does not isolate it with a controlled same-corpus ablation, so we describe it as consistent with model scale rather than fully proven. The directional findings—hybrid beats single mode, int8 stays near float, and binary degrades—survive.

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

**Model answer.** The paper uses RRF, and ADR-8 keeps it as the default. The corrected alpha sweep tests normalized linear fusion. SciFact linear α=0.1 scores 0.6572 versus RRF 0.6568; NFCorpus linear α=0.2 scores 0.3470 versus RRF 0.3466. Both differences are 0.0004 in a single run, so we treat the methods as effectively tied and retain RRF because it is simpler and does not require score-scale assumptions.

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

**Source**: `tests/test_eval.py`; `src/onefind/search.py`.

### Q4.2 What happens if I re-index the same documents?

**Model answer.** The system is idempotent. Re-indexing the same documents with the same `doc_id` produces an upsert (via SQLite's `ON CONFLICT ... DO UPDATE`): the existing row is updated, the FTS5 row is deleted-and-reinserted, the chunk row is preserved (with the same stable `row_id` thanks to the upsert), and the vector row is deleted-and-reinserted. The final state is identical to the pre-re-index state except that `updated_at` is refreshed.

This was important to verify because the FTS5 and vec tables need to be re-synced with the relational tables on every re-index, and a subtle bug here would orphan rows.

**Source**: `src/onefind/store.py` `_flush`; `tests/test_ingest.py::test_reindex_is_idempotent`.

### Q4.3 What happens if a query contains FTS5 syntax like `AND` or `OR`?

**Model answer.** Hostile query syntax is neutralized by a sanitizer that quotes every whitespace token with double quotes, dropping any embedded double quotes in the input. The resulting query is a sequence of quoted tokens that FTS5 interprets as implicit ANDs. Operators like `AND`, `OR`, `NOT`, `(`, `)`, `:`, `*` in user input are thus treated as literal characters, not as syntax.

This was tested explicitly in `test_hostile_query_syntax_does_not_crash` and `test_cli_no_match_exit_zero` with hostile input.

**Source**: `src/onefind/search.py::sanitize_fts_query`; `tests/test_search_lexical.py`.

### Q4.4 How do you handle a very large corpus?

**Model answer.** Scaling choices are explicit:
- Ingest commits validated batches of 256 documents; a failed batch rolls back.
- Float retrieval uses native sqlite-vec KNN.
- Int8/binary retrieval scans stored vectors in 2,048-row blocks and retains only a bounded top-k set, so memory does not grow with the full corpus.
- SQLite `data_version` invalidates cached data after external writes.
- The whole-document design targets small and medium local corpora. Large-scale or internet-facing deployment would need chunking, stronger operations, and usually dedicated infrastructure.

The built-in registry currently contains SciFact and NFCorpus. Other datasets can be supplied as local registry-format folders but are not claimed as built-in registry entries.

**Source**: `src/onefind/store.py`; `docs/02-technical-design.md` ADR-2.

### Q4.5 How is the web demo secured?

**Model answer.** The local demo has defense in depth:
- non-loopback binding is refused unless `--allow-remote` is explicit;
- browser responses use a restrictive CSP, frame denial, MIME sniffing protection, no-referrer, and a restrictive permissions policy;
- indexed HTML is returned as text/highlight segments and never inserted with `innerHTML`;
- FTS values are parameterized and token-quoted;
- the browser API does not expose the absolute index path;
- reset is disabled unless the operator supplies a startup token, and state transitions are locked.

There is no account system, so remote mode still requires a trusted firewall or authenticated reverse proxy.

**Source**: `src/onefind/serve.py`; `docs/02-technical-design.md` security section.

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

**Model answer.** In the corrected runs, hybrid float reaches 0.6568 on SciFact versus semantic float 0.6451, and 0.3466 on NFCorpus versus 0.3167. Hybrid therefore improves over the best single mode on both datasets. Pure-cosine reranking of the deeper fused pool reduces nDCG to 0.6451 and 0.3167 respectively, so reranking is optional and not a claimed improvement.

**Source**: `benchmarks/reports/eval-scifact-2026-08-26.md`; `FORMAL_PROJECT_REPORT.md` §6.1.

### Q5.2 How do the absolute numbers compare to the paper's?

**Model answer.** Our SciFact hybrid at 0.6568 is below the paper's ~0.73 hybrid. The gap is uniform across configurations and is fully explained by the model size difference (ADR-3): we use `all-MiniLM-L6-v2` (22M parameters, 384-dim); the paper uses `Qwen3-Embedding-8B` (8B parameters, 4096-dim). The MiniLM-vs-Qwen3 gap is consistent with the MTEB leaderboard on similar tasks; it is the expected cost of running on CPU with no GPU budget.

The directional findings (qualitative claims) reproduce faithfully. The absolute numbers do not, by design.

**Source**: `FORMAL_PROJECT_REPORT.md` §6.4.

### Q5.3 What did the alpha-sweep extension find?

**Model answer.** The corrected sweep evaluates exact alpha endpoints and intermediate normalized linear fusion. Best linear is 0.6572 at α=0.1 on SciFact and 0.3470 at α=0.2 on NFCorpus. RRF is 0.6568 and 0.3466. The 0.0004 gaps are too small for a strong single-run superiority claim; RRF remains the default because it avoids score normalization and tuning.

**Source**: `benchmarks/reports/alpha-sweep-scifact-2026-09-24.md`; `benchmarks/reports/alpha-sweep-nfcorpus-2026-09-24.md`.

### Q5.4 What are the latency numbers?

**Model answer.** Corrected SciFact p95 latency (5,183 documents):
- Lexical: 8.6 ms
- Semantic float: 447.9 ms
- Semantic int8: 1,819.6 ms
- Semantic binary: 1,702.8 ms
- Hybrid float: 174.8 ms
- Hybrid float + rerank: 405.1 ms

Float uses native sqlite-vec KNN. Quantized modes pay exact bounded scan costs and are much slower here, so we do not claim the paper's quantized speed advantage. The original 150 ms target is missed by vector modes. Reports expose real p50/p95 values instead of hiding them.

**Source**: `benchmarks/reports/eval-scifact-2026-09-24.md`; `README.md`.

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

**Model answer.** Model capacity is the leading explanation, not a proven sole cause. The paper uses Qwen3-Embedding-8B while we use MiniLM-L6-v2 (22M parameters, 384 dimensions). The size difference is consistent with lower absolute nDCG, but isolating it would require a controlled same-corpus, same-engine comparison. We state that limitation rather than claiming a fully isolated causal result.

The directional findings (qualitative claims) reproduce faithfully, which is the more important result: it shows that the architecture, not the model, is what produces the qualitative claims.

**Source**: `docs/02-technical-design.md` ADR-3; `FORMAL_PROJECT_REPORT.md` §6.4.

### Q6.3 What is the biggest limitation of the project?

**Model answer.** Three candidates:
1. **Model scale** — MiniLM cannot match Qwen3-8B's absolute quality. A GPU or a more capable embedder would close the absolute gap.
2. **Dataset scope** — only two BEIR datasets; the paper evaluates on eight. Touché and TREC-COVID would test the engine at higher scale.
3. **sqlite-vec int8/bit** — the application-side workaround is mathematically equivalent but does not deliver the latency story the paper claims for the native implementation. A future `sqlite-vec` release may fix the limitation and let us flip the path back to native.

**Source**: `FORMAL_PROJECT_REPORT.md` §6.3, §9.

### Q6.4 Is the system production-ready?

**Model answer.** No—not as an internet-facing or high-scale production service. v1.1 is hardened for a free, local, single-user research/demo workload with tests, manifests, safety controls, and package verification. Larger corpora, multi-user traffic, strict latency, remote exposure, and operations would require additional infrastructure and authentication.

**Source**: `FORMAL_PROJECT_REPORT.md` §6.3.

---

## Section 7: "What Would You Do Differently" (interviewer wants to test reflection)

### Q7.1 If you were starting over, what would you change?

**Model answer.** Three things:
1. **Create a reproducible dependency lock early.** v1.1 adds bounded direct ranges, pip checks, and package CI, but an exact platform-tested lock would improve reproducibility further.
2. **Run adversarial and multi-hit retrieval tests earlier.** The original suite passed despite reversed BM25 because most fixture queries had one hit.
3. **Make optional test gates finer-grained.** v1.1 adds a mandatory model-free CI lane and a full model/eval/serve lane; future work could split each capability into equally strict jobs.

### Q7.2 What is the next experiment you would run?

**Model answer.** Three candidates, in order of expected yield:
1. **Cross-encoder reranking on the hybrid top-50.** Corrected pure-cosine reranking hurts both datasets, which motivates a reranker that can still use lexical evidence rather than discarding it.
2. **Matryoshka dimension study**. Re-embed the corpus with 64/128/192-dim slices of MiniLM and measure nDCG vs cost. A different axis of the "quantization doesn't hurt" claim.
3. **Scale to Touché (382K documents)** to test the engine at 50× the current scale and measure how the latency story holds up.

**Source**: `FORMAL_PROJECT_REPORT.md` §9.

### Q7.3 What did you learn from the project?

**Model answer.** Four things (elaborated in `FORMAL_PROJECT_REPORT.md` §7):
1. **A shipped library can claim capabilities the binary does not deliver** — empirical probing is the only honest test of a library's capabilities (ADR-7 finding).
2. **Hybrid fusion math is subtler than the formula suggests** — corrected RRF and linear results are effectively tied within 0.0004, so simplicity rather than a large quality gap drives the default.
3. **Architecture is more important than model scale for this class of system** — a 384-dim embedder reproduces all the paper's directional findings.
4. **A spec-driven workflow is a real engineering tool** — the six-doc system caught bugs that a code-first workflow would have shipped.

---

## Section 8: Specific Code Questions (interviewer wants to test code literacy)

### Q8.1 Show me the FTS5 query and explain it.

**Model answer.** From `src/onefind/search.py`:

```sql
SELECT doc_id, title,
       -bm25(fts) AS score,
       snippet(fts, -1, '', '', ' … ', 12) AS snip
FROM fts
WHERE fts MATCH ?
ORDER BY score DESC, doc_id ASC
LIMIT ?
```

- SQLite BM25 is lower-is-better and normally negative, so `-bm25()` creates a conventional higher-is-better score.
- The query therefore sorts `score DESC`; sorting ascending would retain the worst matches.
- Private-use delimiters avoid confusing literal document brackets with highlight markers. The API converts them to text segments before the UI renders them.

### Q8.2 Why is the connection using `check_same_thread=False`?

**Model answer.** FastAPI sync handlers may run in worker threads. The index is opened in the application lifespan and shared with `check_same_thread=False`. A process lock serializes model loading, search, and reset transitions; SQLite provides its own connection locking. The lock is still required because model initialization and closing/reopening the index are higher-level state transitions.

**Source**: `src/onefind/store.py::Index.open`; `src/onefind/serve.py`.

### Q8.3 Walk me through what happens when I run `onefind eval scifact`.

**Model answer.** Step by step:
1. CLI parses arguments, calls `cmd_eval`.
2. `cmd_eval` calls `evaluate.run_eval`.
3. `run_eval` calls `datasets.load_local_or_registry("scifact")` which downloads the BEIR parquet + qrels TSV files to `data/scifact/` (first run only; cached after).
4. `run_eval` loads the corpus, queries, and qrels.
5. `run_eval` creates a unique staging database, loads the CPU embedder, and indexes the selected corpus there.
6. Each configuration builds a query-ID to document-ID/score run and is evaluated with nDCG@10, MAP@10, MRR@10, and Precision@10.
7. The database manifest records dataset hashes, selected IDs, model revision, and configuration; only a successful run atomically replaces `data/scifact.db`.
8. A provenance-rich markdown report is written under `benchmarks/reports/`. A failed model or indexing step leaves any existing target untouched.
9. The CLI prints the report path and exits 0.

**Source**: `src/onefind/evaluate.py`; `src/onefind/cli.py`.

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

**Right answer**: "I worked on it across [X weeks/months] in roughly [Y hours/week]. The commit history records the sequence; you can see the phase-by-phase progression. The original phase history is linear, followed by a v1.1 hardening pass that corrects retrieval and release issues."

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
| What's the headline result? | Hybrid nDCG@10 beats best single mode: 0.6568 on SciFact and 0.3466 on NFCorpus. |
| What's the extension finding? | Best linear and RRF are effectively tied: 0.6572 vs 0.6568 on SciFact and 0.3470 vs 0.3466 on NFCorpus. |
| What's the test count? | 112 tests across 15 modules, including mandatory model-free tests. |
| What's the commit story? | A linear phase-by-phase prototype history followed by a documented v1.1 correctness and release-hardening pass. |
| What's the demo? | FastAPI + single-page HTML, `onefind serve --db data/scifact.db --port 8080`. |
| What's the bottleneck? | Query encoding (~80 ms) and NumPy distance computation (~50–100 ms) on CPU. |
| What would you do next? | Cross-encoder rerank, matryoshka study, scale to Touché. |

---

Good luck with the defense. The project is solid; the defense is about communicating why.
