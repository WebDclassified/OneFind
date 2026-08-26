# Evaluation — scifact

- Date: 2026-08-26
- Model: `sentence-transformers/all-MiniLM-L6-v2` · docs: 5183 · judged queries: 300 · k: 10
- DB: `data/scifact.db` (5183 vectors)
- Latency: warm index, per query, includes query encoding (ms).

| Configuration | nDCG@10 | AP | RR | P@10 | p50 ms | p95 ms |
|---|---|---|---|---|---|---|
| lexical | 0.0467 | 0.0458 | 0.0500 | 0.0050 | 1.1 | 2.3 |
| semantic(float) | 0.6451 | 0.5959 | 0.6047 | 0.0883 | 124.1 | 146.2 |
| semantic(int8) | 0.6451 | 0.5959 | 0.6047 | 0.0883 | 177.1 | 213.5 |
| semantic(binary) | 0.5827 | 0.5357 | 0.5481 | 0.0807 | 139.2 | 170.3 |
| hybrid(float) | 0.6568 | 0.6094 | 0.6203 | 0.0890 | 129.1 | 155.6 |
| hybrid(int8) | 0.6568 | 0.6094 | 0.6203 | 0.0890 | 177.6 | 202.7 |
| hybrid(binary) | 0.5959 | 0.5509 | 0.5656 | 0.0813 | 137.2 | 166.1 |
| hybrid(float)+rerank | 0.6548 | 0.6069 | 0.6169 | 0.0890 | 189.8 | 276.4 |

## Notes
- int8/binary precisions are computed application-side over the stored float vectors (ADR-7); see docs/02.
- Hybrid uses RRF k=60, leg depth = k (ADR-8). `+rerank` rescores fused candidates by full-precision cosine.
