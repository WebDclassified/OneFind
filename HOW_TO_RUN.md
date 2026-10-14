# How to Run OneFind (v1.2)

OneFind runs locally on CPU and is free to use. It needs no cloud account, paid API, GPU or hosted
vector database. For a non-technical walkthrough of the app, see
[`docs/12-user-guide.md`](docs/12-user-guide.md).

## 1. Prerequisites

- Python 3.11 or newer (an official CPython build, so SQLite can load extensions)
- Git
- Internet only for the first model download and for BEIR datasets
- About 500 MB of free disk space for the environment and caches
- Google Chrome or Chromium, only for the browser walkthrough (`tools/ui_check.py`)

## 2. Clone and create an environment

```bash
git clone https://github.com/WebDclassified/OneFind.git
cd OneFind
python -m venv .venv
```

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

```bash
# macOS / Linux
source .venv/bin/activate
```

## 3. Install

| You want | Install |
|---|---|
| The app with keyword search only | `pip install -e ".[serve]"` |
| The app with meaning (semantic/hybrid) search | `pip install -e ".[model,serve]"` |
| Everything: app, evaluations, tests | `pip install -e ".[model,eval,serve,dev]"` |
| The live browser walkthrough (plus the above) | `pip install -e ".[browser]"` |

```bash
python -m pip install --upgrade pip
pip install -e ".[model,eval,serve,dev]"
onefind --version        # onefind 1.2.1
```

The command is lowercase `onefind` on Windows, macOS and Linux. `python -m onefind.cli …` works
too.

## 4. Verify the runtime

```bash
onefind check            # SQLite FTS5 + sqlite-vec
onefind check --full     # also downloads/loads the MiniLM model (CPU)
```

## 5. Run the app (library mode)

```bash
onefind serve
```

Open <http://127.0.0.1:8080/>. With no `--db`, the server runs in **library mode**. It opens the
active collection from your per-user library, or shows the first-run welcome screen ("Index a
folder") when there is none. Index a folder from the browser and watch the progress. Collections
created in the app are stored under the library folder:

| OS | Default library folder |
|---|---|
| Windows | `%APPDATA%\OneFind` |
| macOS | `~/Library/Application Support/OneFind` |
| Linux | `$XDG_DATA_HOME/onefind` or `~/.local/share/onefind` |

```text
<library>/
  collections.json        # names, files, active collection
  collections/<slug>.db   # one SQLite file per collection
  backups/<slug>-YYYYmmdd-HHMMSS.db
```

Useful `serve` options:

| Option | Effect |
|---|---|
| `--home DIR` | Use a different library folder (same as setting `ONEFIND_HOME=DIR`) |
| `--db FILE` | Serve one database file, as in v1.1 (it is not added to your library unless `--home`/`ONEFIND_HOME` is set) |
| `--port 8080` / `--host 127.0.0.1` | Bind address. The server is loopback-only by default |
| `--allow-remote` | Required for a non-loopback `--host`. There is no login, so use only behind a firewall or authenticated proxy |
| `--no-open` | Disable *Open* / *Show in folder* from the web app |
| `--reset-token TOKEN` | Enable the legacy `POST /api/reset` |

**Try it in a throwaway library** (nothing touches your real library):

```powershell
# Windows PowerShell
$env:ONEFIND_HOME = "$env:TEMP\onefind-try"
onefind index .\sample-data --collection Sample --embed
onefind serve
```

```bash
# macOS / Linux
export ONEFIND_HOME=/tmp/onefind-try
onefind index ./sample-data --collection Sample --embed
onefind serve
```

Once the model is cached, OneFind loads it from the local cache without contacting the Hugging
Face Hub; it only downloads when the model is missing. Set `HF_HUB_OFFLINE=1` as well if you want
the Hugging Face libraries to refuse network access outright.

## 6. Collections from the terminal

```bash
onefind collections create Notes              # empty collection in <library>/collections/notes.db
onefind index ~/Notes --collection Notes --embed --exclude drafts --exclude "archive/*"
onefind collections create Work               # another one
onefind collections use Work                  # make it active (the app and CLI fall back to it)
onefind collections list                      # * marks the active collection; --json available
onefind collections rename Work "Work 2026"   # the file keeps its name
onefind collections remove "Work 2026"        # forget it; the .db file is kept
onefind collections remove Old --delete-file --yes   # also delete the file
```

`onefind index --collection NAME` creates the collection if it does not exist yet.

**Which collection does a command use?** `--db PATH` wins, then `--collection NAME`, then
`./onefind.db` in the current folder. `search`, `status`, `doctor`, `compact`, `backup` and
`remove` fall back to the library's active collection when `./onefind.db` does not exist, and
never create an empty database. `index` without `--db`/`--collection` writes `./onefind.db`, as
in v1.1.

## 7. Index, search and keep up to date

```bash
# First index (vectors for meaning search; exclusions are remembered for this folder)
onefind index ~/Notes --collection Notes --embed --exclude drafts

# Later: incremental update. Only new/changed files are read; --prune drops deleted/excluded ones
onefind index ~/Notes --collection Notes --prune

# Re-read everything
onefind index ~/Notes --collection Notes --full

# Hybrid search (default mode) with filters and explanations
onefind search "undo a bad deploy" --collection Notes
onefind search "rollback owner" --mode lexical --explain
onefind search "budget" --folder work --type txt --since 2026-01-01
onefind search "release" --tag ops

# Health, changed files per folder, last run
onefind status                 # --no-scan skips the folder scan; --json for scripts

# Remove a note from the index (the file is untouched; re-indexing adds it back unless excluded)
onefind remove drafts/old-idea.md

# The folders a collection indexes (ids, note counts, saved exclusions, missing folders)
onefind folders list
onefind folders relocate ~/OldNotes ~/Notes      # a folder moved: notes keep their ids (OLD may be its [id])
onefind folders remove ~/Archive                 # forget a folder and drop its notes (files untouched)
onefind folders remove ~/Archive --keep-documents  # forget it but keep its notes searchable
```

`index` prints live progress to stderr (`-q` silences it) and a summary such as
`changes : 3 added, 0 updated, 0 unchanged, 0 removed, 0 skipped`. Press `Ctrl+C` once to cancel
cleanly between files or batches (exit code 130). Committed batches stay searchable.

Supported files: `.md`, `.markdown`, `.txt`, `.text`, `.rst`, `.org`, plus `.jsonl` datasets.
Files over 5 MB, binary files and empty files are skipped and reported. Hidden folders,
`node_modules`, `__pycache__`, `.venv` and `venv` are always excluded.

## 8. Backup, restore, and moving a collection

```bash
onefind backup                                 # active collection -> <library>/backups/<slug>-<timestamp>.db
onefind backup --collection Notes --out ~/Backups            # into a folder (created if needed)
onefind backup --collection Notes --out ~/Backups/notes.db   # to a file (--overwrite to replace)
onefind backup --list                          # backups in the library's backups folder
onefind restore notes-20261014-023257.db --as "Notes restored"   # a name from backups/ or a full path
```

`--out` treats a path as a folder when it is an existing folder, ends with a slash, or is a new
path without an extension (the folder is created); the backup is then named
`<collection>-<timestamp>.db` inside it. Any other path is the backup file name. Backups use SQLite's online backup API, so they are
consistent even while the app is running.
`restore` always copies the backup into a **new** collection (`collections/<slug>.db`) and never
opens the backup in place. In the web app, **Manage → Backups → Restore… → Replace current…**
replaces the active collection after a typed confirmation and an automatic safety backup.

**Move to another computer:** copy a backup (or the `.db`, while OneFind is closed) to the new
machine, then register it there:

```bash
onefind collections create Notes --db ~/Moved/notes-copy.db    # Windows: --db D:\Moved\notes-copy.db
```

In the app, use the collection menu → **Open existing collection file…**. Search and preview work
immediately. *Open file* needs the original folder at the same path, or re-index from its new
location.

## 9. Maintenance

```bash
onefind doctor                 # integrity check: database file, keyword index, vectors (exit 4 if problems remain)
onefind doctor --repair        # rebuild the keyword index, drop leftovers, re-embed missing vectors
onefind compact                # checkpoint the WAL and VACUUM; prints before -> after size

# Long notes (docs/11 section 9): split notes over 200 words into passages and rebuild their vectors
onefind rebuild-vectors --chunking default   # turn passage search on (collections made before 1.2.1)
onefind rebuild-vectors                      # finish a pending or cancelled rebuild (keeps the setting)
onefind rebuild-vectors --chunking off       # back to one vector per note
```

All of these take `--db` / `--collection`. `rebuild-vectors` shows live progress (`-q` silences
it); the first `Ctrl+C` stops between batches of notes (exit 130) and a rerun continues. `status`
prints a `passages` line, and `status`/`doctor` say when a rebuild is needed or recommended. The
app has the same actions under **Manage → Health / Maintenance**, plus *Full rebuild*, *Reindex
with vectors* and *Clear collection…*.

**Exit codes:** `0` success, `2` usage/validation, `3` environment, `4` data error (missing
collection, failed integrity check), `130` cancelled.

## 10. Run the tests

```bash
pytest
```

The suite has more than 850 tests and runs offline after the first model download. It includes a
small real-MiniLM integration suite, and fake embedders cover edge cases. To run only the
model-free tests (as CI does):

```bash
pytest -q \
  tests/test_fusion.py tests/test_search_edges.py tests/test_search_lexical.py \
  tests/test_ingest.py tests/test_envcheck.py tests/test_cli.py tests/test_store.py \
  tests/test_sample_corpus.py tests/test_store_v3.py tests/test_ingest_v3.py \
  tests/test_library.py tests/test_jobs.py tests/test_snippets.py tests/test_search_v3.py \
  tests/test_integration_v3.py tests/test_static_ui.py tests/test_cli_v3.py
```

## 11. Live browser walkthrough (`tools/ui_check.py`)

`tools/ui_check.py` is the end-to-end check for the v1.2 app. It:

1. builds a realistic notes folder and an empty library in a scratch folder;
2. starts `onefind serve --no-open --port <port>` with `ONEFIND_HOME=<scratch>/home`;
3. drives an installed Chrome with Playwright through first run, the index wizard, live search,
   every result state (results, weak matches, no results, error), filters, Explain, preview,
   saved searches, bookmarks, export, collections, backup and restore, maintenance, keyboard use,
   themes, reduced motion, five window widths, and the server stopping mid-session;
4. asserts more than 300 checks and saves screenshots.

```bash
pip install -e ".[browser]"
python tools/ui_check.py                              # scratch: <temp>/onefind-ui, port 8765
python tools/ui_check.py --scratch C:\tmp\uicheck --port 8799 --headed
python tools/ui_check.py --release screenshots/v1.2   # also write the numbered release screenshots
```

| Option | Effect |
|---|---|
| `--scratch DIR` | Working folder for notes, library home and logs (default `<temp>/onefind-ui`) |
| `--out DIR` | Screenshot folder (default `<scratch>/shots`) |
| `--port N` | Server port (default 8765) |
| `--chrome PATH` | Chrome/Chromium executable (or set `ONEFIND_CHROME`); auto-detected otherwise |
| `--headed` / `--slow MS` | Watch the run / slow it down for debugging |
| `--release DIR` | Capture the light/dark × 1280/375 release set (see `screenshots/v1.2/README.md`) |

It prints `N checks passed, M problems` and exits `0` only when every check passed. It never
downloads a browser and touches nothing outside the scratch folder.

The v1.1 evidence runner is still available: `python tools/browser_check.py --output sshot`.

## 12. Reproduce the BEIR evaluations (research)

The harness builds into a temporary database and publishes it only after a successful run. It
never clears an existing database in place.

```bash
onefind eval scifact --db data/scifact.db
onefind eval nfcorpus --db data/nfcorpus.db
```

Each run evaluates eight configurations and writes a provenance-rich report under
`benchmarks/reports/`. The report covers nDCG@10, MAP@10, MRR@10 and P@10; p50/p95 latency; model
name and revision; corpus/query/qrels SHA-256; package versions, Python, OS and Git commit; and
database health and the configuration manifest.

A quick pipeline check (not for reportable numbers):

```bash
onefind eval scifact --db data/scifact-smoke.db --max-docs 100 --limit-queries 50
```

Fusion extension (alpha sweep; α=0 is an exact keyword passthrough, α=1 an exact semantic
passthrough):

```bash
onefind sweep-alpha scifact --db data/scifact.db
onefind sweep-alpha nfcorpus --db data/nfcorpus.db
```

Sample-corpus gold queries:

```bash
onefind index ./sample-data --db demo.db --embed
onefind smoke --db demo.db --queries sample-data/queries.jsonl
```

`smoke` uses the app's keyword matching (`--match auto`, the default; `all` or `any` to compare).
A no-answer case passes when nothing strong is returned. Re-baselined 2026-10-14: sample-data
1.000 Hit@3, 1.000 MRR@5, 1.000 no-answer; `showcase-data` (index `./showcase-data`, queries
`showcase-data/queries.jsonl`) 0.984 Hit@3, 0.967 MRR@5, 1.000 no-answer, identical with
`--match all`.

## 13. Research search flags

```bash
onefind search "chlorophyll" --db demo.db --mode lexical
onefind search "how plants turn sunlight into food" --db demo.db --mode semantic
onefind search "vitamin" --db demo.db --mode hybrid --precision int8
onefind search "money after a surprise repair" --db demo.db --mode hybrid --rerank --candidate-depth 50
onefind search "vitamin" --db demo.db --mode hybrid --fusion linear --alpha 0.5
```

## 14. HTTP API (summary)

The web app uses a JSON API on the same port. The full contract is in
[`docs/11-implementation-contract.md`](docs/11-implementation-contract.md) §6. Every
state-changing endpoint added in v1.2 requires the header `X-OneFind-Request: 1`. Errors use
`{"error": {"code", "message", "retryable", "action"?}}`.

| Area | Endpoints |
|---|---|
| Status | `GET /health`, `GET /api/stats` (status, status sentence, size class, active job) |
| Search | `POST /api/search`, `POST /api/search/stream` (NDJSON stages, then the result), `GET /api/facets` |
| Collections | `GET/POST /api/collections`, `POST /api/collections/{open,active,rename,remove}` |
| Indexing | `GET /api/fs/list`, `POST /api/index` (202 + job; optional `collection` guard), `GET /api/jobs[/{id}]`, `POST /api/jobs/{id}/cancel`, `GET /api/index/changes`, `GET /api/roots`, `POST /api/roots/{remove,relocate}`, `GET/POST /api/autosync` |
| Documents | `GET /api/documents/{doc_id}`, `POST /api/documents/open`, `POST /api/documents/remove`, `GET /api/recent-documents` |
| Personal | `GET/POST /api/history`, `POST /api/history/clear`, `GET/POST /api/saved`, `DELETE /api/saved/{id}`, `GET/POST /api/bookmarks`, `DELETE /api/bookmarks/{doc_id}` |
| Maintenance | `POST /api/maintenance/{backup,restore,check,repair,compact,clear}`, `POST /api/maintenance/rebuild-vectors` (202 + job), `GET /api/backups` |

```bash
curl -X POST http://127.0.0.1:8080/api/search \
  -H "Content-Type: application/json" \
  -d '{"query":"compost","mode":"hybrid","precision":"float","k":10}'
```

## 15. Troubleshooting

**`onefind: command not found`**: activate the environment, then run `python -m pip show onefind`
and `onefind --version`.

**`error: no OneFind collection at onefind.db`** (exit 4): there is no `./onefind.db` and no active
collection. Index a folder first, or pass `--collection NAME` / `--db FILE`.

**The app shows "No index ready" although you indexed with the CLI**: the CLI wrote `./onefind.db`
(no `--collection`). Either `onefind serve --db onefind.db`, or index with `--collection NAME` so
the library knows about it.

**Schema version mismatch**: schema-v1 databases from the earliest prototype are rejected. Rebuild
them. v2 databases (v1.1) migrate to v3 automatically the first time they are opened.

**Model download fails**: check internet access and free cache space, then retry with
`onefind check --full`. Document and query text is never sent to a hosted model service.

**Vector extension unavailable**: run `onefind check` and use an official CPython build with SQLite
extension loading. Keyword search keeps working without it.

**A collection is "busy"**: another OneFind job, a backup tool or a sync client is writing to the
`.db`. Wait and retry. Avoid putting live collection files inside a syncing folder. Sync the
backups instead.

## 16. Zero-cost operating model

- CPU-only MiniLM embeddings, cached locally
- Local SQLite, FTS5 and sqlite-vec
- Vanilla HTML/CSS/JavaScript (no CDN, fonts or build step)
- No telemetry, paid APIs or hosted services
