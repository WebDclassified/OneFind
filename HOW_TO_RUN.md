# How to Run — Step-by-Step Guide

This document walks you through the full reproduction of the scrydb project, from a fresh machine to a working demo, running tests, and producing evaluation reports. Every command is shown explicitly; if you follow it in order, the project will run.

---

## 0. Prerequisites

| Requirement | Minimum | Verified at |
|---|---|---|
| Python | 3.11 or newer (3.13 used during development) | `python --version` |
| pip | bundled with Python | `python -m pip --version` |
| Git | any recent version | `git --version` |
| Internet | for one-time download of the embedding model (~90 MB) and BEIR datasets (~5 MB each) | during the first 10 minutes |
| Disk | ~500 MB free (mostly for the Python virtual environment) | `dir` / `df -h` |
| OS | Windows 10/11, macOS, or Linux | — |

The project was developed and tested on Windows 11 with Python 3.13.7. No GPU is required; everything runs on CPU.

---

## 1. Clone the Repository

```bash
git clone <this repository URL> scrydb
cd scrydb
```

Replace `<this repository URL>` with the actual URL of the project repository (e.g., `https://github.com/<your-username>/scrydb-reproduction.git`).

**Verify**: you should now be inside a directory containing `README.md`, `pyproject.toml`, `docs/`, `src/`, `tests/`, `benchmarks/`, `demo/`, and `sample-data/`.

---

## 2. Create a Python Virtual Environment

Isolating the project in a virtual environment prevents conflicts with other Python projects.

### Windows (PowerShell or Command Prompt)

```powershell

.venv\Scripts\activate
```

### macOS / Linux (bash or zsh)

```bash
python3 -m venv .venv
source .venv/bin/activate
```

**Verify**: your shell prompt should now show `(.venv) ` at the beginning. Confirm with:

```bash
python --version
which python            # on macOS / Linux
where python            # on Windows
```

The Python reported should be inside the `.venv/` directory.

---

## 3. Install the Project and Extras

The project exposes three optional dependency groups: `model` (sentence-transformers), `eval` (ranx + pyarrow), and `serve` (FastAPI + uvicorn). Install all three so every command is available:

```bash
pip install --upgrade pip
pip install -e ".[model,eval,serve]"
```

The `-e` flag installs the project in editable mode, so any code change takes effect immediately without reinstallation. The whole installation typically takes 2–5 minutes, dominated by PyTorch and sentence-transformers.

**Verify**: `pip list | grep scrydb` should show `scrydb 0.1.0 ... /path/to/scrydb`.

---

## 4. Verify the Environment

This runs the layered environment proof. The output should end with `overall: PASS`.

```bash
scrydb check
```

Expected output:

```
scrydb check
  python     : 3.13.7
  sqlite     : 3.50.4
  fts5       : OK   bm25 query ok
  sqlite-vec : OK   vec_version=v0.1.9; knn_rows=2
  model      : not_installed [pip install -e ".[model]"]
  overall    : PASS
```

(The `model: not_installed` line is normal at this step because we did not load the model. To also load the embedding model — confirming it is installed correctly — use the optional `--full` flag.)

```bash
scrydb check --full
```

Expected: the `model` line changes to `model: loaded (dim=384)` after a 1–2 second pause while the model loads.

**Troubleshooting**:
- If `sqlite-vec: FAIL` appears, your Python build may lack extension loading. Install CPython from python.org rather than a system or Conda build.
- If `fts5: FAIL` appears, your SQLite build lacks the FTS5 module. CPython 3.11+ always includes it.
- If `model: not_installed` even after `--full`, the `[model]` extra did not install; re-run `pip install -e ".[model]"` and look for errors during the torch install step.

---

## 5. Run the Test Suite

The test suite has 67 tests organized by phase. Running the full suite takes about 90 seconds on CPU.

```bash
pytest
```

Expected final line:

```
67 passed in ~90s
```

If you see `X passed, Y skipped`, the skipped tests are model-dependent tests that were skipped because the `sentence-transformers` package was not installed. This is normal in a CI environment without the model extra; on a developer machine it should not happen.

**Run only one test file**:

```bash
pytest tests/test_search_lexical.py
```

**Run only one test**:

```bash
pytest tests/test_search_lexical.py::test_relevant_document_ranks_first
```

**Stop on first failure**:

```bash
pytest -x
```

---

## 6. Run a Small Smoke Demo (Sample Data)

This is the fastest way to see the system working. It indexes the four sample `.md` files in `sample-data/` and runs a few searches.

### 6.1 Index the sample corpus

```bash
scrydb index ./sample-data --db demo.db --embed
```

Expected output (after a short pause for the embedding model load):

```
loading embedding model: sentence-transformers/all-MiniLM-L6-v2 ...
indexed 4 files (4 documents total, 4 vectors) -> demo.db [model: ...; float storage, app-side int8/bit]
```

### 6.2 Run a lexical search

```bash
scrydb search "chlorophyll" --db demo.db --mode lexical
```

Expected:

```
 1. [0.8729] photosynthesis - How photosynthesis works
    glucose and oxygen inside chloroplasts. [Chlorophyll] pigments absorb red and blue wavelengths ...
```

### 6.3 Run a semantic search (paraphrase)

```bash
scrydb search "how plants turn sunlight into food" --db demo.db --mode semantic
```

Expected: the photosynthesis document is ranked first, even though the query shares no words with it.

### 6.4 Run a hybrid search

```bash
scrydb search "chlorophyll" --db demo.db --mode hybrid --precision float
```

Expected: the photosynthesis document is ranked first via RRF fusion of lexical and semantic signals.

### 6.5 Run an alpha-sweep on the tiny corpus

```bash
scrydb sweep-alpha ./sample-data --db demo.db --alphas 0,0.5,1.0 --k 4
```

Note that sweep-alpha requires a proper qrels file; with the sample-data this will fail with a `DataError`. This command is intended for the BEIR datasets (Section 7). For sample data, skip this step.

### 6.6 Clean up the demo database

```bash
del demo.db demo.db-wal demo.db-shm        # Windows
rm -f demo.db demo.db-wal demo.db-shm     # macOS / Linux
```

---

## 7. Reproduce a BEIR Evaluation

This is the heart of the reproduction. We evaluate the system on the SciFact dataset from BEIR.

### 7.1 First run — SciFact

```bash
scrydb eval scifact --db data/scifact.db
```

What happens:
1. The CLI downloads `BeIR/scifact` corpus/queries parquet files and `BeIR/scifact-qrels/test.tsv` (cached under `data/` for reuse).
2. The CLI loads the embedding model (~2 seconds).
3. The CLI indexes all 5,183 SciFact documents — this is the slow step (~2–3 minutes on CPU).
4. The CLI runs 8 configurations × 300 judged queries, printing a progress line per configuration.
5. The CLI writes a markdown report to `benchmarks/reports/eval-scifact-YYYY-MM-DD.md`.

Expected final lines:

```
  lexical                nDCG@10=0.0467 p95=2.3ms
  semantic(float)        nDCG@10=0.6451 p95=146.2ms
  semantic(int8)         nDCG@10=0.6451 p95=213.5ms
  semantic(binary)       nDCG@10=0.5827 p95=170.3ms
  hybrid(float)          nDCG@10=0.6568 p95=155.6ms
  hybrid(int8)           nDCG@10=0.6568 p95=202.7ms
  hybrid(binary)         nDCG@10=0.5959 p95=166.1ms
  hybrid(float)+rerank   nDCG@10=0.6548 p95=276.4ms
report written: benchmarks\reports\eval-scifact-YYYY-MM-DD.md
```

### 7.2 Second run — NFCorpus

```bash
scrydb eval nfcorpus --db data/nfcorpus.db
```

Expected nDCG@10 (full table in `benchmarks/reports/eval-nfcorpus-YYYY-MM-DD.md`):

| Configuration        | nDCG@10 |
|----------------------|--------:|
| lexical              | 0.1731  |
| semantic (float)     | 0.3167  |
| hybrid (float)       | 0.3249  |
| hybrid + rerank      | **0.3260** |

### 7.3 Run the alpha-sweep extension

This reuses the already-built index from the previous step and sweeps linear-fusion α.

```bash
scrydb sweep-alpha scifact --db data/scifact.db
scrydb sweep-alpha nfcorpus --db data/nfcorpus.db
```

Each command produces a markdown report with a per-α nDCG@10 chart and a comparison row against the RRF baseline.

### 7.4 Optional: use smoke flags for a quick check

To run a 100-document / 50-query smoke test instead of the full BEIR evaluation:

```bash
scrydb eval scifact --db data/scifact-smoke.db --max-docs 100 --limit-queries 50
```

This completes in under 30 seconds and is useful for verifying the pipeline before committing to the full run.

---

## 8. Run the Localhost Demo

The demo is a FastAPI backend serving a single-page web application.

### 8.1 Start the server

```bash
scrydb serve --db data/scifact.db --port 8080
```

Expected:

```
scrydb serving on http://127.0.0.1:8080 (db: data/scifact.db)
```

The server binds to `127.0.0.1` only — it is not reachable from other machines on the network.

### 8.2 Open the browser

Navigate to <http://127.0.0.1:8080/>. You should see:

- A header with the document count, vector count, and model name.
- A search box.
- A mode toggle (lexical / semantic / hybrid).
- A precision toggle (float / int8 / binary), shown only for semantic and hybrid.

### 8.3 Try queries

| Query | Expected behaviour |
|---|---|
| `vitamin C supplementation` (lexical) | Returns 3–4 medical papers with `[highlighted]` snippets |
| `does vitamin C help with colds` (semantic) | Returns "Cold-related respiratory symptoms" as #1 |
| `b12 deficiency` (hybrid) | Combines lexical and semantic results |
| `asdfqwer nonexistent` (any mode) | "no results" empty state |

### 8.4 Stop the server

Press `Ctrl-C` in the terminal where the server is running.

---

## 9. Common Issues and Troubleshooting

### "scrydb: command not found"

The CLI is installed via `pip install -e ".[...]"`. If the command is not found:
- Check that the virtual environment is activated (`(.venv)` prefix in the prompt).
- Check that the `[model]`, `[eval]`, or `[serve]` extra was installed (whichever command you tried to run). For example, `scrydb serve` requires the `[serve]` extra.
- On some Linux systems, the CLI script may be installed to `~/.local/bin`; add this to your `PATH` if needed.

### "Cannot operate on a closed database" during serve

This happens if the SQLite connection was opened in one thread and used in another. The codebase uses `check_same_thread=False` to avoid this; if you have modified `Index.open`, restore that argument.

### "no such column: temp.probe_fts" during check

The internal FTS5 probe uses an unqualified table name on the throwaway `:memory:` connection. If you have modified `envcheck.fts5_available`, do not pass a schema-qualified name as the `bm25()` argument; SQLite rejects it in expression position.

### "Inserted vector for the embedding column is expected to be of type int8, but a float32 vector was provided"

This is the symptom of the `sqlite-vec` 0.1.9 limitation (ADR-7). The codebase handles it by computing int8 and binary rankings in NumPy over the stored float32 vectors. If you have removed the int8/binary branches from `search.semantic_search`, you will see this error. Restore them.

### The first run is very slow

The first `scrydb eval` run downloads the embedding model (~90 MB) and the BEIR datasets (~5 MB). Subsequent runs use the cached files. The first query after the model loads is ~1 second; subsequent queries are sub-150 ms p95 on CPU.

### Tests pass individually but fail in some order

If you see failures only when running tests in a specific order, it is most likely a test-isolation issue. Each test should use a fresh `tmp_path` fixture. If a test stores state on the class or module that is not reset between tests, fix it by moving the state into the fixture.

### "Address already in use" when starting `scrydb serve`

Another process is using port 8080. Either stop that process, or pick a different port: `scrydb serve --port 8090 --db data/scifact.db`.

---

## 10. Expected Outputs Summary

After completing Sections 5–8 of this guide, the project will have produced:

| File | Section | What it contains |
|---|---|---|
| `data/scifact.{parquet,tsv}` | §7.1 | Cached BEIR dataset files |
| `data/scifact.db` | §7.1 | The SciFact SQLite index (5,183 vectors) |
| `benchmarks/reports/eval-scifact-YYYY-MM-DD.md` | §7.1 | SciFact evaluation report |
| `data/nfcorpus.db` | §7.2 | The NFCorpus SQLite index (3,633 vectors) |
| `benchmarks/reports/eval-nfcorpus-YYYY-MM-DD.md` | §7.2 | NFCorpus evaluation report |
| `benchmarks/reports/alpha-sweep-scifact-YYYY-MM-DD.md` | §7.3 | SciFact alpha-sweep report |
| `benchmarks/reports/alpha-sweep-nfcorpus-YYYY-MM-DD.md` | §7.3 | NFCorpus alpha-sweep report |
| `benchmarks/reports/env-YYYY-MM-DD.md` | §4 | Environment proof report |
| `screenshots/*.png` | §8.3 (you create) | Demo UI screenshots for the formal report |

---

## 11. What If I Just Want to Look?

If you only want to read the project, not run it:

- `README.md` — overview, quickstart, results.
- `FORMAL_PROJECT_REPORT.md` — full academic report.
- `REPORT.md` — earlier, shorter academic report.
- `VIVA_QUESTIONS.md` — anticipated defense questions with model answers.
- `TEAM_PREPARATION.md` — project briefing for the 3 teammates.
- `docs/01-prd.md` … `docs/07-references.md` — the living specification.
- `benchmarks/reports/*.md` — generated evaluation reports.
- `src/scrydb/` — the implementation.
- `tests/` — the test suite.

The code is heavily commented and the spec documents explain every design decision. You can understand the whole system without running it.


directly run the project 
scrydb eval scifact --db data/scifact.db        # reproduce 8 configs on 5.2K docs
scrydb sweep-alpha scifact --db data/scifact.db # extension alpha sweep
scrydb serve --db data/scifact.db --port 8080  # live demo UI
pytest                                         # 67 tests