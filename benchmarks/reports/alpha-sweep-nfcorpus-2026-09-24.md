# Alpha sweep — nfcorpus (float precision)

- Date: 2026-09-24
- Model: `sentence-transformers/all-MiniLM-L6-v2` @ `unpinned` · judged queries: 323 · k: 10
- Code: `8f17ad25d816` · worktree: dirty (uncommitted v1.1 hardening)
- Dataset corpus: `sha256:f2a1c0b570a5efdf23cfa36f5e573d062cc113c6e7251e22c69b323a33ea895e`
- Dataset queries: `sha256:edd2b878b8130fb3b5e3c4b9428ddc1e67fbd6eedf8ae099fca3b87838ba6b29`
- Dataset qrels: `sha256:f8fba6ef3d4dd9c3a242a8ba4ae38276fc3622fce7dcbae764766d564542fd2a`
- Linear fusion min-max-normalizes each leg's scores to [0, 1] within the
  retrieved k, then blends: alpha * semantic + (1 - alpha) * lexical.
- RRF baseline = the existing `hybrid(float)` config.

## nDCG@10 by alpha

```
linear(α=0.0)      | ========                                 0.2073
linear(α=0.1)      | ==============                           0.3466
linear(α=0.2)      | ==============                           0.3470
linear(α=0.3)      | ==============                           0.3464
linear(α=0.4)      | ==============                           0.3462
linear(α=0.5)      | ==============                           0.3418
linear(α=0.6)      | =============                            0.3373
linear(α=0.7)      | =============                            0.3323
linear(α=0.8)      | =============                            0.3297
linear(α=0.9)      | =============                            0.3261
linear(α=1.0)      | =============                            0.3167
RRF baseline       | ==============                           0.3466
```

Best alpha: **0.2** (nDCG@10 = 0.3470);
RRF baseline nDCG@10 = 0.3466.

## Full metrics

| Configuration | nDCG@10 | MAP@10 | MRR@10 | P@10 |
|---|---|---|---|---|
| linear(α=0.0) | 0.2073 | 0.0835 | 0.3418 | 0.1399 |
| linear(α=0.1) | 0.3466 | 0.1310 | 0.5484 | 0.2601 |
| linear(α=0.2) | 0.3470 | 0.1314 | 0.5513 | 0.2598 |
| linear(α=0.3) | 0.3464 | 0.1312 | 0.5507 | 0.2588 |
| linear(α=0.4) | 0.3462 | 0.1311 | 0.5493 | 0.2585 |
| linear(α=0.5) | 0.3418 | 0.1259 | 0.5431 | 0.2573 |
| linear(α=0.6) | 0.3373 | 0.1223 | 0.5357 | 0.2554 |
| linear(α=0.7) | 0.3323 | 0.1192 | 0.5221 | 0.2548 |
| linear(α=0.8) | 0.3297 | 0.1172 | 0.5176 | 0.2545 |
| linear(α=0.9) | 0.3261 | 0.1144 | 0.5120 | 0.2529 |
| linear(α=1.0) | 0.3167 | 0.1108 | 0.5077 | 0.2433 |
| RRF baseline | 0.3466 | 0.1312 | 0.5473 | 0.2579 |
