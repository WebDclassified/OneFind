# Alpha sweep — nfcorpus (float precision)

- Date: 2026-08-26
- Model: `sentence-transformers/all-MiniLM-L6-v2` · judged queries: 323 · k: 10
- Linear fusion min-max-normalizes each leg's scores to [0, 1] within the
  retrieved k, then blends: alpha * semantic + (1 - alpha) * lexical.
- RRF baseline = the existing `hybrid(float)` config.

## nDCG@10 by alpha

```
linear(α=0.0)      | ===========                              0.2763
linear(α=0.1)      | =============                            0.3166
linear(α=0.2)      | =============                            0.3187
linear(α=0.3)      | =============                            0.3188
linear(α=0.4)      | =============                            0.3196
linear(α=0.5)      | =============                            0.3217
linear(α=0.6)      | =============                            0.3229
linear(α=0.7)      | =============                            0.3210
linear(α=0.8)      | =============                            0.3221
linear(α=0.9)      | =============                            0.3212
linear(α=1.0)      | =============                            0.3170
RRF baseline       | =============                            0.3249
```

Best alpha: **0.6** (nDCG@10 = 0.3229);
RRF baseline nDCG@10 = 0.3249.

## Full metrics

| Configuration | nDCG@10 | AP | RR | P@10 |
|---|---|---|---|---|
| linear(α=0.0) | 0.2763 | 0.1018 | 0.4097 | 0.2285 |
| linear(α=0.1) | 0.3166 | 0.1264 | 0.5236 | 0.2316 |
| linear(α=0.2) | 0.3187 | 0.1272 | 0.5247 | 0.2341 |
| linear(α=0.3) | 0.3188 | 0.1271 | 0.5236 | 0.2341 |
| linear(α=0.4) | 0.3196 | 0.1270 | 0.5207 | 0.2359 |
| linear(α=0.5) | 0.3217 | 0.1226 | 0.5290 | 0.2384 |
| linear(α=0.6) | 0.3229 | 0.1204 | 0.5340 | 0.2384 |
| linear(α=0.7) | 0.3210 | 0.1178 | 0.5188 | 0.2409 |
| linear(α=0.8) | 0.3221 | 0.1162 | 0.5166 | 0.2440 |
| linear(α=0.9) | 0.3212 | 0.1141 | 0.5110 | 0.2458 |
| linear(α=1.0) | 0.3170 | 0.1114 | 0.5083 | 0.2433 |
| RRF baseline | 0.3249 | 0.1277 | 0.5278 | 0.2406 |
