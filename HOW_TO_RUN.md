# How to Run OneFind

OneFind is free to run locally on CPU. It does not require a cloud account, paid API, GPU, or hosted vector database.

## 1. Prerequisites

- Python 3.11 or newer
- Git
- Internet only for the first model/dataset download
- About 500 MB free disk space for the development environment and caches

## 2. Clone and create an environment

```bash
git clone https://github.com/WebDclassified/OneFind.git
cd OneFind
python -m venv .venv
```

Activate it:

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

```bash
# macOS / Linux
source .venv/bin/activate
```

## 3. Install OneFind

For the complete local demo, evaluations, and tests:

```bash
python -m pip install --upgrade pip
pip install -e ".[model,eval,serve,dev]"
```

The command name is lowercase `onefind` on Windows, macOS, and Linux.

## 4. Verify the runtime

```bash
onefind check
onefind check --full
```

The first command checks SQLite FTS5 and sqlite-vec. `--full` also loads the local MiniLM model. The model uses CPU by default.

## 5. Build the expanded offline demo

```bash
onefind index ./sample-data --db demo.db --embed
onefind smoke --db demo.db --queries sample-data/queries.jsonl
```

The bundle contains 20 short documents across science, health, finance, technology, nature, music, operations, and more. The gold file contains 26 queries covering lexical, semantic, hybrid, disambiguation, and no-answer cases.

## 6. Start the polished local UI

```bash
onefind serve --db demo.db --port 8080
```

Open <http://127.0.0.1:8080/>.

The UI is packaged inside the Python wheel. It has no CDN, web-font, analytics, paid API, or frontend build step. It includes:

- keyword, semantic, and hybrid modes;
- float, int8, and binary precision controls when supported;
- capability-aware empty and unavailable states;
- safe rendering of untrusted document text;
- keyboard focus shortcuts (`/` and `Ctrl/Cmd+K`);
- responsive and dark-mode layouts;
- result source, score metric, latency, and highlighted snippets.

The server binds to loopback by default. A non-loopback bind is refused unless `--allow-remote` is explicitly supplied. The demo has no account system, so only use remote binding behind a trusted firewall or authenticated reverse proxy.

## 7. Run the tests

```bash
pytest
```

The current suite contains 112 tests. It uses a small real-model integration suite plus fake embedders for fast edge-case coverage. No paid service is used.

For only mandatory model-free tests:

```bash
pytest -q \
  tests/test_fusion.py \
  tests/test_search_edges.py \
  tests/test_search_lexical.py \
  tests/test_ingest.py \
  tests/test_envcheck.py \
  tests/test_cli.py \
  tests/test_store.py \
  tests/test_sample_corpus.py
```

## 8. Reproduce the BEIR evaluations

The corrected harness builds into a temporary database and publishes it only after a successful run. Existing databases are never destructively cleared in place.

```bash
onefind eval scifact --db data/scifact.db
onefind eval nfcorpus --db data/nfcorpus.db
```

Each run evaluates eight configurations and writes a provenance-rich report under `benchmarks/reports/`. Reports include:

- nDCG@10, MAP@10, MRR@10, and Precision@10;
- p50 and p95 query latency;
- model name and resolved revision;
- corpus/query/qrels SHA-256 fingerprints;
- package versions, Python, OS, and Git commit;
- database health and configuration manifest.

For a quick pipeline check:

```bash
onefind eval scifact --db data/scifact-smoke.db --max-docs 100 --limit-queries 50
```

Capped runs are intended for pipeline validation; use full runs for reportable effectiveness comparisons.

## 9. Run the fusion extension

```bash
onefind sweep-alpha scifact --db data/scifact.db
onefind sweep-alpha nfcorpus --db data/nfcorpus.db
```

The sweep verifies that the database manifest matches the exact dataset before loading the model. At `alpha=0`, linear fusion is an exact keyword passthrough; at `alpha=1`, it is an exact semantic passthrough.

## 10. Useful CLI examples

```bash
# Keyword search
onefind search "chlorophyll" --db demo.db --mode lexical

# Semantic paraphrase
onefind search "how plants turn sunlight into food" --db demo.db --mode semantic

# Hybrid RRF
onefind search "vitamin" --db demo.db --mode hybrid

# Hybrid with 50 candidates per leg and cosine reranking
onefind search "money after a surprise repair" --db demo.db \
  --mode hybrid --rerank --candidate-depth 50

# Weighted fusion
onefind search "vitamin" --db demo.db \
  --mode hybrid --fusion linear --alpha 0.5
```

## 11. HTTP API

- `GET /api/stats` — readiness, capabilities, counts, size, model, and health.
- `POST /api/search` — validated keyword, semantic, or hybrid search.
- `GET /health` — lightweight service health.
- `POST /api/reset` — disabled unless the server receives an explicit reset token.

Example:

```bash
curl -X POST http://127.0.0.1:8080/api/search \
  -H "Content-Type: application/json" \
  -d '{"query":"compost","mode":"hybrid","precision":"float","k":10}'
```

## 12. Troubleshooting

### Schema version mismatch

Schema-v1 databases from the earlier prototype are intentionally rejected. Rebuild them:

```bash
onefind eval scifact --db data/scifact.db
```

### `onefind: command not found`

Activate the environment and confirm:

```bash
python -m pip show onefind
onefind --version
```

### Model download fails

Check internet access and free cache space, then retry:

```bash
onefind check --full
```

The project never sends document or query text to a hosted model service.

### Vector extension is unavailable

Run:

```bash
onefind check
```

Use an official CPython build with SQLite extension loading enabled. Keyword-only search still works without the model, but semantic/hybrid modes require vectors.

## 13. Reproduce the browser evidence

The repository includes a comprehensive Playwright scenario runner. It uses an already-installed Chrome/Chromium binary and does not download a browser or call a hosted service.

```bash
pip install -e ".[browser]"
python tools/browser_check.py --output sshot
```

The command starts temporary indexed, lexical-only, no-index, and hostile-content fixtures on ports 8090–8093, drives the real UI, captures 21 screenshots, validates retrieval/result/security assertions, and stops the services. See `sshot/README.md` and `sshot/manifest.json`.

## 14. Zero-cost operating model

- CPU-only MiniLM embeddings
- local SQLite and sqlite-vec
- local FTS5 and NumPy
- vanilla HTML/CSS/JavaScript
- no telemetry
- no paid APIs
- no hosted services
- datasets and model cache are reusable

See `README.md` for architecture and current results.
