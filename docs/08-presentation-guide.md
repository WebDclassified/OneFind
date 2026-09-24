# OneFind Presentation Guide

## Goal

A jury-ready demonstration should prove three things quickly:

1. The system is useful on real, varied local knowledge.
2. The engineering is correct, safe, and reproducible.
3. The result is free to run and does not depend on a paid service.

## 8-minute live script

### 1. State the problem and promise — 45 seconds

> OneFind searches a private local knowledge base using exact keywords, meaning-based vectors, or a hybrid of both. Everything is stored in one SQLite file and runs locally on CPU.

### 2. Prove the runtime — 30 seconds

```bash
onefind --version
onefind check --json
```

Point out FTS5, sqlite-vec, model availability, and the free CPU stack.

### 3. Show the large knowledge base — 1 minute

```bash
onefind index ./showcase-data --db data/showcase.db --embed
```

State: 120 structured records across 12 domains, 202 gold queries, nested paths, tables, code, dates, Unicode, long text, and hostile HTML.

### 4. Prove retrieval quality — 1 minute

```bash
onefind smoke --db data/showcase.db --queries showcase-data/queries.jsonl --k 5
```

Report the current Hit@3, MRR@5, and no-answer accuracy from the terminal. Do not claim results that are not shown.

### 5. Demonstrate the simple UI — 2 minutes

```bash
onefind serve --db data/showcase.db --port 8080
```

Use this order:

1. `PROD-001 feature flags` — exact ID.
2. `How should our team handle rollback with clear ownership?` — meaning.
3. `CSP remote binding reset safety` — hybrid.
4. `edge 005 AND OR NOT` — safe operators.
5. `unmatched jury token zq0xx nonexistent` — no-answer state.
6. Switch to dark or mobile view only if time permits.

### 6. Show correctness, not just results

Explain:

- BM25 uses `-bm25()` and `ORDER BY score DESC`.
- Float uses native sqlite-vec KNN.
- Int8/binary are exact bounded scans and are not falsely claimed as faster.
- Rerank returns refreshed cosine scores and a complete candidate pool.
- Evaluation indexes are atomic and manifest-bound.

### 7. Show safety and packaging — 1 minute

- Indexed content is rendered with DOM text nodes, not raw HTML.
- CSP and frame protections are active.
- Remote binding is refused unless explicitly acknowledged.
- Reset is disabled without an explicit token.
- The wheel contains HTML, CSS, JavaScript, and favicon assets.

### 8. Close with evidence

Point to:

- `REFERENCE_FEATURES.md`
- `benchmarks/reports/`
- `sshot/manifest.json`
- `HOW_TO_RUN.md`

## Questions to expect

| Question | Short answer |
|---|---|
| Why no paid API? | Privacy, reproducibility, and zero recurring cost; MiniLM runs locally on CPU. |
| Is this really one SQLite file? | Yes for documents, FTS, vectors, metadata, and manifest. Model weights remain a reusable local cache. |
| How do you know rankings are correct? | Multi-hit BM25 regression, native float KNN, exact bounded quantized scans, and corrected BEIR evidence. |
| What happens when embeddings change? | The model name/revision is stored; mismatches are rejected and the index must be rebuilt. |
| Is the web server production-ready? | It is hardened for a local single-user demo, not public multi-tenant deployment. |
| How do you prevent XSS? | Structured text/highlight segments, no response-derived HTML sinks, CSP, and browser assertions. |
| What if a dataset changes during evaluation? | Dataset hashes and manifests are checked; sweeps fail before model loading on a mismatch. |
| What is intentionally not included? | Authentication, cloud scaling, paid APIs, GPU-only models, and a public RAG answer layer. |

## Failure-safe presentation checklist

- Run `onefind check` before the defense.
- Keep `data/showcase.db` and the model cache warm.
- Open the UI before presenting.
- Use the same five curated queries.
- Do not manually edit generated benchmark numbers.
- If a live query fails, show `sshot/21-xss-rendered-as-text.png` or the error state and explain the tested recovery path.
