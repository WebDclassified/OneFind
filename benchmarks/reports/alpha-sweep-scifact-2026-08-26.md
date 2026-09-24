# Alpha sweep — scifact (float precision)

> **Superseded (2026-08-26 run).** The v1.0 lexical ordering defect affected candidate retrieval. Use the corrected 2026-09-24 sweep.

- Date: 2026-08-26
- Model: `sentence-transformers/all-MiniLM-L6-v2` · judged queries: 300 · k: 10
- Linear fusion min-max-normalizes each leg's scores to [0, 1] within the
  retrieved k, then blends: alpha * semantic + (1 - alpha) * lexical.
- RRF baseline = the existing `hybrid(float)` config.

## nDCG@10 by alpha

```
linear(α=0.0)      | ==================                       0.4396
linear(α=0.1)      | ==========================               0.6572
linear(α=0.2)      | ==========================               0.6572
linear(α=0.3)      | ==========================               0.6572
linear(α=0.4)      | ==========================               0.6572
linear(α=0.5)      | ==========================               0.6572
linear(α=0.6)      | ==========================               0.6548
linear(α=0.7)      | ==========================               0.6544
linear(α=0.8)      | ==========================               0.6526
linear(α=0.9)      | ==========================               0.6487
linear(α=1.0)      | ==========================               0.6460
RRF baseline       | ==========================               0.6568
```

Best alpha: **0.1** (nDCG@10 = 0.6572);
RRF baseline nDCG@10 = 0.6568.

## Full metrics

| Configuration | nDCG@10 | AP | RR | P@10 |
|---|---|---|---|---|
| linear(α=0.0) | 0.4396 | 0.3300 | 0.3425 | 0.0887 |
| linear(α=0.1) | 0.6572 | 0.6101 | 0.6204 | 0.0890 |
| linear(α=0.2) | 0.6572 | 0.6101 | 0.6204 | 0.0890 |
| linear(α=0.3) | 0.6572 | 0.6101 | 0.6204 | 0.0890 |
| linear(α=0.4) | 0.6572 | 0.6101 | 0.6204 | 0.0890 |
| linear(α=0.5) | 0.6572 | 0.6101 | 0.6204 | 0.0890 |
| linear(α=0.6) | 0.6548 | 0.6071 | 0.6165 | 0.0890 |
| linear(α=0.7) | 0.6544 | 0.6067 | 0.6158 | 0.0890 |
| linear(α=0.8) | 0.6526 | 0.6044 | 0.6132 | 0.0890 |
| linear(α=0.9) | 0.6487 | 0.5992 | 0.6080 | 0.0890 |
| linear(α=1.0) | 0.6460 | 0.5963 | 0.6051 | 0.0887 |
| RRF baseline | 0.6568 | 0.6094 | 0.6203 | 0.0890 |
