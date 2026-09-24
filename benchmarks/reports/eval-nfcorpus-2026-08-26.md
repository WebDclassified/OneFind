# Evaluation — nfcorpus

> **Superseded (2026-08-26 run).** The v1.0 lexical ordering and rerank-score defects make these historical numbers unsuitable for current claims. Use the corrected 2026-09-24 report.

- Date: 2026-08-26
- Model: `sentence-transformers/all-MiniLM-L6-v2` · docs: 3633 · judged queries: 323 · k: 10
- DB: `data/nfcorpus.db` (3633 vectors)
- Latency: warm index, per query, includes query encoding (ms).

| Configuration | nDCG@10 | AP | RR | P@10 | p50 ms | p95 ms |
|---|---|---|---|---|---|---|
| lexical | 0.1731 | 0.0783 | 0.3174 | 0.1050 | 1.2 | 5.9 |
| semantic(float) | 0.3167 | 0.1108 | 0.5077 | 0.2433 | 91.9 | 136.6 |
| semantic(int8) | 0.3156 | 0.1097 | 0.5065 | 0.2427 | 127.4 | 160.5 |
| semantic(binary) | 0.2761 | 0.0918 | 0.4654 | 0.2093 | 100.8 | 156.8 |
| hybrid(float) | 0.3249 | 0.1277 | 0.5278 | 0.2406 | 97.2 | 190.3 |
| hybrid(int8) | 0.3241 | 0.1268 | 0.5265 | 0.2399 | 144.9 | 218.1 |
| hybrid(binary) | 0.2951 | 0.1130 | 0.5037 | 0.2146 | 99.3 | 134.3 |
| hybrid(float)+rerank | 0.3260 | 0.1270 | 0.5337 | 0.2406 | 137.8 | 157.7 |

## Notes
- int8/binary precisions are computed application-side over the stored float vectors (ADR-7); see docs/02.
- Hybrid uses RRF k=60, leg depth = k (ADR-8). `+rerank` rescores fused candidates by full-precision cosine.
