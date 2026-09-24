# Alpha sweep — scifact (float precision)

- Date: 2026-09-24
- Model: `sentence-transformers/all-MiniLM-L6-v2` @ `unpinned` · judged queries: 300 · k: 10
- Code: `8f17ad25d816` · worktree: dirty (uncommitted v1.1 hardening)
- Dataset corpus: `sha256:243324b35f03d82bd6d98a5f575966876e86cad7ce16e5333a35b1b793dc4f45`
- Dataset queries: `sha256:1c37956c5dc8b810b60302323c24d1a9e79e26411ba8f5ad9d0888642e2a9034`
- Dataset qrels: `sha256:0864bb985e0ca2367ba217977e72004d549054b2b06666ed9d4825ac7c21284c`
- Linear fusion min-max-normalizes each leg's scores to [0, 1] within the
  retrieved k, then blends: alpha * semantic + (1 - alpha) * lexical.
- RRF baseline = the existing `hybrid(float)` config.

## nDCG@10 by alpha

```
linear(α=0.0)      | ==                                       0.0467
linear(α=0.1)      | ==========================               0.6572
linear(α=0.2)      | ==========================               0.6572
linear(α=0.3)      | ==========================               0.6572
linear(α=0.4)      | ==========================               0.6572
linear(α=0.5)      | ==========================               0.6572
linear(α=0.6)      | ==========================               0.6548
linear(α=0.7)      | ==========================               0.6544
linear(α=0.8)      | ==========================               0.6526
linear(α=0.9)      | ==========================               0.6487
linear(α=1.0)      | ==========================               0.6451
RRF baseline       | ==========================               0.6568
```

Best alpha: **0.1** (nDCG@10 = 0.6572);
RRF baseline nDCG@10 = 0.6568.

## Full metrics

| Configuration | nDCG@10 | MAP@10 | MRR@10 | P@10 |
|---|---|---|---|---|
| linear(α=0.0) | 0.0467 | 0.0458 | 0.0500 | 0.0050 |
| linear(α=0.1) | 0.6572 | 0.6101 | 0.6204 | 0.0890 |
| linear(α=0.2) | 0.6572 | 0.6101 | 0.6204 | 0.0890 |
| linear(α=0.3) | 0.6572 | 0.6101 | 0.6204 | 0.0890 |
| linear(α=0.4) | 0.6572 | 0.6101 | 0.6204 | 0.0890 |
| linear(α=0.5) | 0.6572 | 0.6101 | 0.6204 | 0.0890 |
| linear(α=0.6) | 0.6548 | 0.6071 | 0.6165 | 0.0890 |
| linear(α=0.7) | 0.6544 | 0.6067 | 0.6158 | 0.0890 |
| linear(α=0.8) | 0.6526 | 0.6044 | 0.6132 | 0.0890 |
| linear(α=0.9) | 0.6487 | 0.5992 | 0.6080 | 0.0890 |
| linear(α=1.0) | 0.6451 | 0.5959 | 0.6047 | 0.0883 |
| RRF baseline | 0.6568 | 0.6094 | 0.6203 | 0.0890 |
