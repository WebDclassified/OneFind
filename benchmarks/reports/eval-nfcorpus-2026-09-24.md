# Evaluation — nfcorpus

- Date: 2026-09-24
- Model: `sentence-transformers/all-MiniLM-L6-v2` · docs: 3633 · judged queries: 323 · k: 10
- Model revision: `unpinned`
- Code: `8f17ad25d816` · worktree: dirty (uncommitted v1.1 hardening) · Python 3.13.7 · Windows 11 (AMD64)
- Packages: onefind=1.1.0, numpy=2.5.2, sentence-transformers=6.0.0, sqlite-vec=0.1.9, ranx=0.3.21
- Dataset corpus: `sha256:f2a1c0b570a5efdf23cfa36f5e573d062cc113c6e7251e22c69b323a33ea895e`
- Dataset queries: `sha256:edd2b878b8130fb3b5e3c4b9428ddc1e67fbd6eedf8ae099fca3b87838ba6b29`
- Dataset qrels: `sha256:f8fba6ef3d4dd9c3a242a8ba4ae38276fc3622fce7dcbae764766d564542fd2a`
- DB: `data\nfcorpus.db` (3633 vectors)
- Latency: warm index, per query, includes query encoding (ms).

| Configuration | nDCG@10 | MAP@10 | MRR@10 | P@10 | p50 ms | p95 ms |
|---|---|---|---|---|---|---|
| lexical | 0.2073 | 0.0835 | 0.3418 | 0.1399 | 2.5 | 9.6 |
| semantic(float) | 0.3167 | 0.1108 | 0.5077 | 0.2433 | 82.3 | 189.5 |
| semantic(int8) | 0.3160 | 0.1103 | 0.5104 | 0.2427 | 689.1 | 1188.1 |
| semantic(binary) | 0.2761 | 0.0918 | 0.4654 | 0.2093 | 565.9 | 820.3 |
| hybrid(float) | 0.3466 | 0.1312 | 0.5473 | 0.2579 | 82.5 | 218.3 |
| hybrid(int8) | 0.3465 | 0.1309 | 0.5489 | 0.2582 | 831.9 | 1298.6 |
| hybrid(binary) | 0.3169 | 0.1158 | 0.5280 | 0.2316 | 471.5 | 647.1 |
| hybrid(float)+rerank | 0.3167 | 0.1108 | 0.5077 | 0.2433 | 85.5 | 130.7 |

## Notes
- int8/binary precisions are computed application-side over the stored float vectors (ADR-7); see docs/02.
- Float uses sqlite-vec cosine KNN; int8/binary use application-side ranking over stored float vectors (ADR-7).
- Hybrid uses RRF k=60 and leg depth = k. With `+rerank`, each leg retrieves candidate-depth (default 50), and the complete fused pool is scored by full-precision cosine.
