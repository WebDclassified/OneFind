# Evaluation — scifact

- Date: 2026-09-24
- Model: `sentence-transformers/all-MiniLM-L6-v2` · docs: 5183 · judged queries: 300 · k: 10
- Model revision: `unpinned`
- Code: `8f17ad25d816` · worktree: dirty (uncommitted v1.1 hardening) · Python 3.13.7 · Windows 11 (AMD64)
- Packages: onefind=1.1.0, numpy=2.5.2, sentence-transformers=6.0.0, sqlite-vec=0.1.9, ranx=0.3.21
- Dataset corpus: `sha256:243324b35f03d82bd6d98a5f575966876e86cad7ce16e5333a35b1b793dc4f45`
- Dataset queries: `sha256:1c37956c5dc8b810b60302323c24d1a9e79e26411ba8f5ad9d0888642e2a9034`
- Dataset qrels: `sha256:0864bb985e0ca2367ba217977e72004d549054b2b06666ed9d4825ac7c21284c`
- DB: `data\scifact.db` (5183 vectors)
- Latency: warm index, per query, includes query encoding (ms).

| Configuration | nDCG@10 | MAP@10 | MRR@10 | P@10 | p50 ms | p95 ms |
|---|---|---|---|---|---|---|
| lexical | 0.0467 | 0.0458 | 0.0500 | 0.0050 | 3.7 | 8.6 |
| semantic(float) | 0.6451 | 0.5959 | 0.6047 | 0.0883 | 145.8 | 447.9 |
| semantic(int8) | 0.6465 | 0.5979 | 0.6069 | 0.0883 | 1248.4 | 1819.6 |
| semantic(binary) | 0.5827 | 0.5357 | 0.5481 | 0.0807 | 980.5 | 1702.8 |
| hybrid(float) | 0.6568 | 0.6094 | 0.6203 | 0.0890 | 98.3 | 174.8 |
| hybrid(int8) | 0.6582 | 0.6114 | 0.6225 | 0.0890 | 846.3 | 1662.7 |
| hybrid(binary) | 0.5959 | 0.5509 | 0.5656 | 0.0813 | 870.8 | 1538.6 |
| hybrid(float)+rerank | 0.6451 | 0.5959 | 0.6047 | 0.0883 | 181.6 | 405.1 |

## Notes
- int8/binary precisions are computed application-side over the stored float vectors (ADR-7); see docs/02.
- Float uses sqlite-vec cosine KNN; int8/binary use application-side ranking over stored float vectors (ADR-7).
- Hybrid uses RRF k=60 and leg depth = k. With `+rerank`, each leg retrieves candidate-depth (default 50), and the complete fused pool is scored by full-precision cosine.
