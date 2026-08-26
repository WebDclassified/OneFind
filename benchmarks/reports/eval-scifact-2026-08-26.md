# Evaluation — scifact

- Date: 2026-08-26
- Model: `sentence-transformers/all-MiniLM-L6-v2` · docs: 5183 · judged queries: 300 · k: 10
- DB: `data/scifact.db` (5183 vectors)
- Latency: warm index, per query, includes query encoding (ms).

| Configuration | nDCG@10 | AP | RR | P@10 | p50 ms | p95 ms |
|---|---|---|---|---|---|---|
| lexical | 0.0467 | 0.0458 | 0.0500 | 0.0050 | 7.7 | 18.6 |
| semantic(float) | 0.6451 | 0.5959 | 0.6047 | 0.0883 | 180.3 | 312.8 |
| semantic(int8) | 0.6451 | 0.5959 | 0.6047 | 0.0883 | 174.0 | 223.4 |
| semantic(binary) | 0.5827 | 0.5357 | 0.5481 | 0.0807 | 127.1 | 169.3 |
| hybrid(float) | 0.6568 | 0.6094 | 0.6203 | 0.0890 | 144.0 | 218.5 |
| hybrid(int8) | 0.6568 | 0.6094 | 0.6203 | 0.0890 | 193.6 | 259.7 |
| hybrid(binary) | 0.5959 | 0.5509 | 0.5656 | 0.0813 | 137.9 | 190.9 |
| hybrid(float)+rerank | 0.6548 | 0.6069 | 0.6169 | 0.0890 | 190.4 | 264.7 |

## Notes
- int8/binary precisions are computed application-side over the stored float vectors (ADR-7); see docs/02.
- Hybrid uses RRF k=60, leg depth = k (ADR-8). `+rerank` rescores fused candidates by full-precision cosine.
