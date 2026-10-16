# OneFind v1.2 — Implementation Contract

> Binding interface agreement for the v1.2 "product polish" work described in
> [`docs/10-product-vision-and-feature-spec.md`](10-product-vision-and-feature-spec.md).
> Every module, endpoint, and UI piece below is built against this file so
> independently written parts fit together. If you must deviate, keep the
> deviation backward compatible and note it in the [Deviations](#deviations)
> section at the end of this file. **Where a Deviation and an earlier section
> disagree, the Deviation describes the shipped behaviour.**
>
> Status: sections 0–8 shipped in **1.2.0**; sections 9–10 (passage chunking, auto-sync) and
> their app wiring shipped in **1.2.1** (see [`CHANGELOG.md`](../CHANGELOG.md)).
> End-user documentation: [`docs/12-user-guide.md`](12-user-guide.md).

**Sections:** [0 Ground rules](#0-ground-rules) ·
[1 Storage](#1-storage--schema-v3-srconefindstorepy) ·
[2 Ingestion](#2-ingestion-srconefindingestpy) ·
[3 Retrieval](#3-retrieval-srconefindsearchpy-new-srconefindsnippetspy) ·
[4 Collections](#4-collections-registry-srconefindlibrarypy-new) ·
[5 Jobs](#5-background-jobs--model-cache-srconefindjobspy-new) ·
[6 HTTP API](#6-http-api-srconefindservepy) ·
[7 CLI](#7-cli-srconefindclipy) ·
[8 Frontend](#8-frontend-srconefindstatic) ·
[9 Passage chunking](#9-passage-chunking-v121) ·
[10 Auto-sync](#10-auto-sync-v121) ·
[Deviations](#deviations)

## 0. Ground rules

- **Local-first, no new runtime dependencies.** Core stays `sqlite-vec` only;
  `serve` extra stays FastAPI/pydantic/uvicorn; the frontend stays vanilla
  HTML/CSS/JS with no CDN, fonts, or frameworks.
- **Backward compatibility.** Every existing test in `tests/` must keep
  passing. Existing public functions keep their signatures (new parameters are
  keyword-only with defaults). Existing v2 databases open and auto-migrate.
- **Python ≥ 3.11**, match the existing code style (type hints, `from __future__
  import annotations`, typed errors from `onefind.errors`, docstrings on public
  functions, comments only where the "why" is non-obvious).
- **Security stays strict:** CSP unchanged in spirit (`script-src 'self'`,
  `style-src 'self'`, no inline script/style, no `innerHTML` /
  `insertAdjacentHTML` in JS), loopback-only by default, untrusted document
  text is always rendered as text.
- Run tests with the project venv: `.venv/Scripts/python -m pytest` (Windows)
  from the repo root.

## 1. Storage — schema v3 (`src/onefind/store.py`)

`SCHEMA_VERSION = 3`. `Index.open()` migrates a v2 database to v3 in place,
inside one transaction (ALTER TABLE + CREATE TABLE IF NOT EXISTS), then
updates `schema_meta.schema_version` and `PRAGMA user_version`. Unknown
versions (≠2, ≠3) still raise `DataError`. A brand-new database is created
directly at v3.

### 1.1 `documents` — new nullable columns

| column        | type    | meaning |
|---------------|---------|---------|
| `content_hash`| TEXT    | sha256 hex of the raw source bytes (files) or of the body (JSONL) |
| `mtime`       | REAL    | source file modification time, epoch seconds; NULL for JSONL |
| `size_bytes`  | INTEGER | source size in bytes |
| `ext`         | TEXT    | lowercase suffix incl. dot (`.md`), NULL for JSONL rows |
| `folder`      | TEXT    | posix parent of `source` relative to its root; `''` for root-level files |
| `tags_json`   | TEXT    | JSON array of lowercase tag strings (from front matter), NULL if none |
| `root_id`     | INTEGER | `collection_roots.root_id` the document came from, NULL if unknown |

Indexes: `idx_documents_folder(folder)`, `idx_documents_mtime(mtime)`,
`idx_documents_hash(content_hash)`.

### 1.2 New tables

```sql
collection_roots(root_id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE,
                 exclude_json TEXT, added_at TEXT NOT NULL, last_indexed_at TEXT)
search_history(id INTEGER PRIMARY KEY, query TEXT NOT NULL, mode TEXT NOT NULL,
               precision TEXT, filters_json TEXT, result_count INTEGER NOT NULL,
               searched_at TEXT NOT NULL)          -- capped at 200 rows
saved_queries(id INTEGER PRIMARY KEY, name TEXT NOT NULL, query TEXT NOT NULL,
              mode TEXT NOT NULL, precision TEXT, filters_json TEXT,
              created_at TEXT NOT NULL)
bookmarks(doc_id TEXT PRIMARY KEY REFERENCES documents(doc_id) ON DELETE CASCADE,
          note TEXT, created_at TEXT NOT NULL)
doc_activity(doc_id TEXT PRIMARY KEY REFERENCES documents(doc_id) ON DELETE CASCADE,
             last_opened_at TEXT NOT NULL, open_count INTEGER NOT NULL DEFAULT 0)
index_runs(id INTEGER PRIMARY KEY, root_path TEXT, started_at TEXT NOT NULL,
           finished_at TEXT, status TEXT NOT NULL,   -- running|succeeded|failed|cancelled
           added INTEGER DEFAULT 0, updated INTEGER DEFAULT 0, unchanged INTEGER DEFAULT 0,
           removed INTEGER DEFAULT 0, skipped INTEGER DEFAULT 0, error TEXT)
```

`schema_meta` gains optional keys `collection_name` and `created_at`.
`Index.clear()` also empties the user tables above (history, saved, bookmarks,
activity, roots, runs) — it is a full reset.

### 1.3 `Document` dataclass — new optional fields (all default `None`)

`content_hash: str | None`, `mtime: float | None`, `size_bytes: int | None`,
`ext: str | None`, `folder: str | None`, `tags: list[str] | None`,
`root_id: int | None`. `add_documents()` persists them (upsert updates them).

### 1.4 New `Index` methods

```python
add_documents(docs, *, progress=None) -> int
    # progress(event: dict) is called after each committed batch with
    # {"phase": "write" | "embed", "done": int, "total": int | None}
remove_documents(doc_ids: Iterable[str]) -> int          # index-only; files untouched
fingerprints(root_id: int | None = None) -> dict[str, str | None]   # doc_id -> content_hash
ensure_root(path: str, exclude: list[str] | None = None) -> int     # upsert, returns root_id
roots() -> list[dict]          # {root_id, path, exclude, added_at, last_indexed_at, documents}
mark_root_indexed(root_id: int) -> None
get_document(doc_id: str) -> dict | None
    # {doc_id,title,body,source,folder,ext,mtime,size_bytes,tags,content_hash,
    #  root_id,created_at,updated_at,bookmarked}
duplicates_of(doc_id: str) -> list[str]   # other doc_ids sharing content_hash
duplicate_groups(limit: int = 50) -> list[list[str]]
facets() -> dict
    # {"folders":[{"folder":str,"count":int}], "types":[{"ext":str,"count":int}],
    #  "tags":[{"tag":str,"count":int}], "modified":{"min":float|None,"max":float|None}}
# history / saved / bookmarks / activity
record_search(query, mode, precision, filters: dict | None, result_count) -> None
recent_searches(limit=10) -> list[dict]   # de-duplicated by (query, mode), newest first
clear_history() -> int
save_query(name, query, mode, precision=None, filters=None) -> dict
saved_queries() -> list[dict]
delete_saved_query(query_id: int) -> bool
add_bookmark(doc_id, note=None) -> dict   ; remove_bookmark(doc_id) -> bool
bookmarks() -> list[dict]                 ; is_bookmarked(doc_id) -> bool
record_open(doc_id) -> None               ; recent_documents(limit=8) -> list[dict]
# runs
start_run(root_path) -> int ; finish_run(run_id, status, counts: dict, error=None) -> None
last_run() -> dict | None
# maintenance
backup_to(dest: str | Path, *, overwrite=False) -> dict      # sqlite3 backup API; {path, bytes}
integrity_check() -> dict
    # {"ok": bool, "sqlite": "ok" | message, "fts": "ok" | message,
    #  "health": health(), "problems": [str], "suggestions": [str]}
repair() -> dict        # rebuild FTS from documents, drop orphan vectors,
                        # re-embed missing vectors when an embedder is attached
compact() -> dict       # wal_checkpoint(TRUNCATE) + VACUUM; {"before": int, "after": int}
```

All user-table writes commit immediately. `stats()` additionally returns
`"last_indexed_at"`, `"roots"` (count), `"collection_name"`.

## 2. Ingestion (`src/onefind/ingest.py`)

```python
SUPPORTED_SUFFIXES = {".txt", ".md", ".markdown", ".rst", ".org", ".text"}
DEFAULT_EXCLUDES = [".*", "node_modules", "__pycache__", ".venv", "venv"]
MAX_FILE_BYTES = 5 * 1024 * 1024

ingest_path(index, corpus_path, *, exclude=None, prune=False, incremental=True,
            progress=None, cancel=None) -> dict
```

- `exclude`: fnmatch globs matched against each relative posix path **and**
  each path component (so `drafts` or `archive/*` or `*.log.md` work). Defaults
  are always applied in addition to user patterns.
- Front matter: a leading `---\n...\n---` block is parsed for `title:`,
  `tags:` (`[a, b]` or `- a` lines, or comma list) and stripped from the body.
  A `# Heading` after front matter still becomes the title if no `title:`.
- `incremental=True`: files whose `content_hash` matches the stored hash are
  not rewritten (counted as `unchanged`). `incremental=False` rewrites all.
- `prune=True`: documents belonging to this root whose file no longer exists
  (or is now excluded) are removed from the index (`removed`).
- `progress(event)` events: `{"phase": "scan"|"read"|"write"|"embed"|"prune"|"done",
  "done": int, "total": int | None, "message": str}`.
- `cancel()` returning True aborts between files/batches by raising
  `OperationCancelled` (new subclass of `OneFindError` in `errors.py`,
  exit_code 130). Committed batches stay committed.
- Skips (not errors): files over `MAX_FILE_BYTES`, undecodable/binary files
  (NUL byte in first 8 KB), empty files. Reported in `skipped`.
- Return dict (superset of today's): `files_indexed` (= added + updated +
  unchanged — *files considered*; keeps old tests valid), `total_documents`,
  `db`, `root`, `added`, `updated`, `unchanged`, `removed`,
  `skipped: [{"path": str, "reason": str}]`, `duplicates: [[doc_id, ...]]`.
- A run is recorded via `start_run`/`finish_run`; the root is registered via
  `ensure_root` (folders only; single files register their parent folder with
  an exclude-everything-else semantics is NOT required — single files simply
  get `root_id = ensure_root(parent)`).
- `scan_changes(index, root, exclude=None, limit=20000) -> dict` returns
  `{"root": str, "exists": bool, "new": int, "changed": int, "deleted": int,
  "unchanged": int, "truncated": bool}` using mtime+size first and hashing only
  when they differ.

## 3. Retrieval (`src/onefind/search.py`, new `src/onefind/snippets.py`)

```python
@dataclass(frozen=True)
class SearchFilters:
    folders: tuple[str, ...] = ()       # prefix match on documents.folder ("" = root)
    exts: tuple[str, ...] = ()          # ".md", ".txt" …
    tags: tuple[str, ...] = ()          # any-of
    modified_after: float | None = None # epoch seconds, inclusive
    modified_before: float | None = None
    def is_empty(self) -> bool
    @classmethod
    def from_dict(cls, data: dict | None) -> "SearchFilters"   # validates, raises UsageError
    def to_dict(self) -> dict

Hit(doc_id, title, score, snippet, source=None, explain: dict | None = None)

search(index, query, mode="hybrid", k=10, precision="float", rrf_k=60,
       rerank=False, fusion="rrf", alpha=0.5, candidate_depth=50,
       *, filters: SearchFilters | None = None, explain: bool = False,
       on_stage: Callable[[str, str, float], None] | None = None) -> list[Hit]

search_detailed(index, query, **same) -> SearchOutcome
@dataclass
class SearchOutcome:
    hits: list[Hit]
    stages: list[dict]          # [{"name": str, "ms": float}] in execution order
    abstained: bool             # True when every hit is "weak" (see below)
    weak_count: int
```

- **Stage names** (emitted in order via `on_stage(name, "start"|"done", ms)`):
  `parse`, `lexical`, `embed`, `semantic`, `fuse`, `rerank`. Only stages that
  actually run are emitted (`lexical` mode: parse, lexical).
- **Filters**: lexical adds `AND fts.doc_id IN (SELECT doc_id FROM documents
  WHERE …)`; semantic with filters uses an exact block scan restricted to
  allowed row ids (never post-filters a KNN top-k, which would lose results).
- **Explain** (when `explain=True`, every Hit gets):
  ```json
  {"mode": "hybrid", "precision": "float",
   "matched_terms": ["rollback"], "missing_terms": ["owner"],
   "lexical_rank": 2, "lexical_score": 7.31,
   "semantic_rank": 1, "semantic_score": 0.62,
   "fused_score": 0.0325, "confidence": "strong" | "moderate" | "weak",
   "reasons": ["Matched 1 of 2 keywords", "Very close in meaning (0.62)",
               "Found by both keyword and meaning search"]}
  ```
  `matched_terms` uses the same porter/unicode61 notion as FTS (a query term
  matches if the FTS row matches that single quoted term).
- **Confidence / abstention**: semantic similarity (float/int8 cosine; binary
  converted to `1 - 2*hamming/dim`) ≥ `STRONG_SIM` → strong, ≥ `WEAK_SIM` →
  moderate, else weak. Lexical hit with all terms matched → strong, some →
  moderate. Hybrid: present in both legs → strong; otherwise the better of the
  two leg confidences. Thresholds are module constants calibrated on
  `sample-data` + `sample-data/queries.jsonl` with the real MiniLM model.
  Weak hits are still returned (the UI groups them as "Weaker matches");
  `abstained=True` when all returned hits are weak. Ranking is unchanged.
- **Determinism**: unchanged ordering rules (score desc, doc_id asc).

`snippets.py` owns all snippet logic (moved out of `serve.py`, which then
imports it): `segments_from_fts(snippet)`, `segments_with_terms(text, query)`,
`best_passage(body, query, *, max_chars=320) -> str` (sentence/paragraph-aware
window with the most distinct query-term hits, cut at word boundaries, `…`
markers), and `build_snippet(hit, body, query) -> list[{"text", "highlight"}]`.
Highlighting matches whole words and simple stems (query term prefix ≥ 4
chars, e.g. "running" highlights "run…" forms) case-insensitively.

## 4. Collections registry (`src/onefind/library.py`, new)

```python
def default_home() -> Path   # $ONEFIND_HOME, else %APPDATA%\OneFind (Windows),
                             # ~/Library/Application Support/OneFind (macOS),
                             # $XDG_DATA_HOME/onefind or ~/.local/share/onefind
class Library:
    def __init__(self, home: str | Path | None = None)
    home: Path ; collections_dir: Path (home/"collections") ; backups_dir: Path (home/"backups")
    def list(self) -> list[dict]      # [{"name","db","created_at","exists","active"}]
    def get(self, name) -> dict       # raises DataError if unknown (case-insensitive)
    def create(self, name, db_path=None) -> dict   # default db: collections_dir/<slug>.db
    def register(self, name, db_path) -> dict      # existing file; validates it opens
    def remove(self, name, *, delete_file=False) -> None
    def rename(self, old, new) -> dict
    def set_active(self, name) -> dict ; def active(self) -> dict | None
    def backup_path_for(self, name) -> Path        # backups_dir/<slug>-YYYYmmdd-HHMMSS.db
    def list_backups(self, name=None) -> list[dict] # {"path","bytes","created_at","collection"}
```

Stored as `home/collections.json` `{"version": 1, "active": str | null,
"collections": [{"name", "db", "created_at"}]}` written atomically (temp file
+ `os.replace`). Names: 1–64 chars, trimmed, unique case-insensitively, must
contain a letter or digit. Slugs: lowercase, `[a-z0-9-]`.

## 5. Background jobs & model cache (`src/onefind/jobs.py`, new)

```python
class EmbedderCache:            # process-wide; one SentenceEmbedder per model name
    def get(self, model_name: str) -> SentenceEmbedder   # thread-safe lazy load
    def loaded(self, model_name: str) -> bool
class JobManager:
    def __init__(self, embedders: EmbedderCache)
    def start_index(self, db_path, root, *, exclude=None, embed=None, model_name=None,
                    prune=True, incremental=True, label=None) -> dict
        # one running job per db_path, else UsageError("an indexing job is already running")
        # embed=None -> embed if the index already has vectors OR the model stack is importable
    def get(self, job_id) -> dict ; def list(self, active_only=False) -> list[dict]
    def cancel(self, job_id) -> dict
```

Job dict: `{"id", "kind": "index", "db", "root", "status": "queued"|"running"|
"succeeded"|"failed"|"cancelled", "phase": "scan"|"read"|"write"|"embed"|
"prune"|"model"|"done", "done": int, "total": int|None, "message": str,
"started_at", "finished_at", "result": ingest dict|None, "error": {"code",
"message"}|None, "steps": [{"name": "scan"|"index"|"vectors"|"ready",
"status": "pending"|"active"|"done"|"skipped"|"failed"}]}`.
Jobs open their **own** `Index` connection (WAL lets the server keep reading).
`SentenceEmbedder` gets an internal `threading.Lock` around encoding
(`embed.py`) so the cached model is safe to share between a job and search.

## 6. HTTP API (`src/onefind/serve.py`)

`create_app(index_path=None, *, reset_token=None, allowed_hosts=None,
home=None, allow_open=True)`.
- `index_path` given → that db is the active collection (registered in the
  library under its file stem if not already present, unless the library
  cannot be written — then it runs as an unlisted ad-hoc collection).
- `index_path=None` → library mode: active collection from `Library(home)`.
- All existing endpoints/fields keep working (see `tests/test_serve.py`).
- **CSRF guard:** every mutating endpoint added in v1.2 requires header
  `X-OneFind-Request: 1` → otherwise 403 `{"error":{"code":"csrf_check_failed"}}`.
  `/api/search`, `/api/search/stream`, and the legacy `/api/reset` are exempt.
- Static assets: serve any existing file in `static/` whose name matches
  `^[a-z0-9][a-z0-9-]*\.(js|css|svg)$` (frontend may split into modules).
- Error envelope everywhere: `{"error": {"code", "message", "retryable",
  "action"?}}` where `action` is an optional machine hint for the UI:
  `"index"|"reindex"|"embed"|"retry"|"switch_mode"|"repair"|"open_collection"`.

| Method & path | Body / query | Response |
|---|---|---|
| GET `/api/stats` | – | existing fields + `collection` `{name}`, `status` (`"ready"`, `"empty"`, `"no_index"`, `"needs_attention"`, `"indexing"`), `status_detail` (one plain sentence), `model_loaded`, `last_indexed_at`, `roots` (count), `size_class` (`"small"`<5k docs, `"medium"`<50k, `"large"`), `size_note` (str or null), `job` (active job or null), `vectors_missing` (int). **Must not** contain the db path. |
| GET `/api/collections` | – | `{"active": name|null, "collections": [{"name","db","exists","active","documents","vectors","db_bytes","created_at"}], "home": str}` |
| POST `/api/collections` | `{"name", "db_path"?}` | created collection (empty db created); 409 `name_taken` |
| POST `/api/collections/open` | `{"db_path", "name"?}` | registers an existing .db file (e.g. moved from another machine) and activates it |
| POST `/api/collections/active` | `{"name"}` | switches; returns `/api/stats` payload |
| POST `/api/collections/rename` | `{"name","new_name"}` | updated entry |
| POST `/api/collections/remove` | `{"name","delete_file": bool, "confirm_name"?}` | `delete_file` requires `confirm_name == name`; cannot remove the active collection while a job runs |
| GET `/api/fs/list` | `?path=` (default: home dir) | `{"path","parent","entries":[{"name","path","kind":"dir"|"file","supported":bool}],"places":[{"label","path"}],"supported_files":int}` — directories first, names only, hidden entries omitted, max 500 entries |
| POST `/api/index` | `{"path", "exclude": [str]?, "embed": bool|null, "prune": bool=true, "full": bool=false}` | 202 job dict |
| GET `/api/jobs` | `?active=1` | `{"jobs":[...]}` |
| GET `/api/jobs/{id}` | – | job dict |
| POST `/api/jobs/{id}/cancel` | – | job dict |
| GET `/api/index/changes` | – | `{"roots":[scan_changes(...)], "stale": bool}` |
| POST `/api/search` | existing + `filters`?, `explain`=true, `record`=true | existing shape; each result adds `explain`, `folder`, `ext`, `modified_at` (ISO or null), `tags`, `bookmarked`, `duplicates` (list), `confidence`; `meta` adds `stages`, `abstained`, `weak_count`, `filters` |
| POST `/api/search/stream` | same as `/api/search` | `application/x-ndjson`: `{"type":"stage","name","status":"start"|"done","ms"}`… then `{"type":"stage","name":"build",…}` and finally `{"type":"result", ...search response}` or `{"type":"error","error":{…}}` |
| GET `/api/facets` | – | `Index.facets()` |
| GET `/api/documents/{doc_id:path}` | – | `get_document` (+`modified_at` ISO, `duplicates`, body capped at 200 000 chars, `truncated`) ; 404 `not_found` |
| POST `/api/documents/open` | `{"doc_id","reveal": false}` | opens with OS default app (`os.startfile` / `open` / `xdg-open`; reveal = select in file manager). Path must resolve inside a registered root and exist. Records activity. 403 `open_disabled` when not loopback/disabled; 404 `file_missing` with action `reindex` |
| POST `/api/documents/remove` | `{"doc_ids":[...]}` | `{"removed": n}` |
| GET/POST `/api/history`, POST `/api/history/clear` | – | `{"items":[...]}` |
| GET/POST `/api/saved`, DELETE `/api/saved/{id}` | `{"name","query","mode","precision"?,"filters"?}` | saved entry / list |
| GET/POST `/api/bookmarks`, DELETE `/api/bookmarks/{doc_id:path}` | `{"doc_id","note"?}` | |
| GET `/api/recent-documents` | `?limit=8` | `{"items":[{doc_id,title,source,folder,last_opened_at|null,updated_at}]}` |
| POST `/api/maintenance/backup` | `{}` | `{"path","bytes","created_at"}` written to `Library.backup_path_for(active)` |
| GET `/api/backups` | – | `{"items": Library.list_backups(active)}` |
| POST `/api/maintenance/restore` | `{"backup_path", "mode": "new"|"replace", "name"?, "confirm_name"?}` | `new`: copy into a new collection and activate; `replace`: requires `confirm_name` == active name, backs up current first, then swaps file |
| POST `/api/maintenance/check` | – | `integrity_check()` |
| POST `/api/maintenance/repair` | – | `repair()` |
| POST `/api/maintenance/compact` | – | `compact()` |
| POST `/api/maintenance/clear` | `{"confirm_name"}` | `Index.clear()` when it equals the active collection name |

Backup/restore paths accepted from the client must be inside
`Library.backups_dir` or end in `.db` and pass `Index.open` validation.

## 7. CLI (`src/onefind/cli.py`)

- `index PATH` gains `--exclude PATTERN` (repeatable), `--prune`, `--full`,
  `--collection NAME`; prints live progress to stderr and a richer summary
  (added/updated/unchanged/removed/skipped).
- `search` gains `--folder`, `--type`, `--tag` (repeatable), `--since
  YYYY-MM-DD`, `--explain`, `--collection`.
- New: `collections {list,create,use,remove,rename}`, `backup [--out]`,
  `restore BACKUP --as NAME`, `doctor [--repair]`, `compact`, `remove DOC_ID…`,
  `status` (health + stale scan + last run).
- `serve`: `--db` becomes optional (library mode when omitted), adds
  `--home`, `--no-open`.
- `--db` keeps its default `onefind.db` when neither `--db` nor
  `--collection` is given (backward compatible).

## 8. Frontend (`src/onefind/static/`)

- Vanilla ES modules allowed (`<script type="module" src="/assets/app.js">`),
  files named per §6 static rule. Must keep: `id="search-input"`, reference to
  `/assets/app.js`, no inline handlers, no `innerHTML`/`insertAdjacentHTML`,
  uses `replaceChildren` and `AbortController`.
- Mutating `fetch` calls send `X-OneFind-Request: 1`.
- Search uses `/api/search/stream` (NDJSON via `response.body.getReader()`)
  to animate real stages, falling back to `/api/search` if streaming fails.
- Debounced live search (≈250 ms, min 2 chars) + Enter to search immediately;
  stale requests aborted.
- Themes: `data-theme="light"|"dark"` on `<html>`, default follows
  `prefers-color-scheme`, manual toggle persisted in `localStorage`
  (wrapped in try/catch).
- Motion tokens in CSS (`--ease-out`, `--dur-1/2/3`); everything gated by
  `prefers-reduced-motion` and a user "Reduce motion" toggle
  (`data-motion="reduced"`).

## 9. Passage chunking (v1.2.1)

The embedding model (all-MiniLM-L6-v2) reads only the first 256 word pieces
(about 200 words) of a text. Before v1.2.1 every note had **one** vector of
`compose_embed_text(title, body)`, so meaning search could not see anything
after a note's first ~200 words. With passage chunking a long note is split
into overlapping passages, each passage gets its own vector, and a note is
ranked by its best passage. Keyword search (FTS5/BM25) is unchanged: still one
FTS row per note.

### 9.1 The setting (`schema_meta`)

| key | value |
|---|---|
| `chunking` | JSON `{"version": 1, "max_words": 200, "overlap_words": 40}` or `"off"`; **absent = off** (one whole-note passage per note, the pre-1.2.1 behaviour) |
| `chunking_built` | the setting every stored note is known to follow; `"mixed"` when that is unknown. While it differs from `chunking` and some note is affected, a rebuild is pending |

- **New collections default to on** (`onefind.chunking.DEFAULT_CHUNKING`):
  `Index.open` writes both keys when it *creates* a database, so
  `Library.create`, `onefind index` on a new file and jobs all get it.
- **Existing collections keep what they have** (pre-1.2.1 files have no key,
  i.e. off). Nothing is re-split behind the user's back; see 9.5.
- **Evaluation stays whole-document:** `evaluate.run_eval` calls
  `index.set_chunking(None)` on its fresh staging index (one vector per BEIR
  document, published numbers stay reproducible) and records
  `"chunking": null` in `eval_manifest`; `run_alpha_sweep` refuses an index
  whose documents were split. `run_smoke` evaluates whatever the index uses.
- `clear()` keeps `chunking` (it is an identity key now) and resets
  `chunking_built` to it.

Default calibrated on 2026-10-14 with the real model (hybrid uses
`lexical_match="auto"` like the server; k = 10; answerable queries only):

| corpus / mode | whole notes (off) hit@1 · hit@3 · MRR | 160/40 + break before headings | **200/40 (default)** |
|---|---|---|---|
| sample-data (20 short notes) hybrid | 1.000 · 1.000 · 1.000 | identical | 1.000 · 1.000 · 1.000 |
| sample-data semantic | 1.000 · 1.000 · 1.000 | identical | 1.000 · 1.000 · 1.000 |
| showcase-data (120 notes, ~250 words) hybrid | 0.948 · 0.990 · 0.968 | 0.943 · 0.969 · 0.960 | **0.964 · 0.990 · 0.977** |
| showcase-data semantic | 0.922 · 0.974 · 0.952 | 0.932 · 0.958 · 0.948 | **0.943 · 0.979 · 0.963** |

Off reproduces the pre-change numbers exactly. Confidence thresholds
(`STRONG_SIM`/`WEAK_SIM`) are unchanged: with passages the weakest relevant
best-passage cosine on showcase rose from 0.357 to 0.428 and the strongest
off-topic top-1 from 0.241 to 0.282 (21 off-topic questions; sample-data
0.330 either way), all below `WEAK_SIM = 0.34`, so every off-topic question
still abstains in float, int8 and binary. Long-note proof
(`tests/test_chunking.py`): a 740-word note whose answer is in its last
lines ranks #1 by meaning (cosine 0.46, "moderate", passage 5 of 5) with
chunking on, and #4 at 0.18 ("weak", query abstains) with it off.

### 9.2 Splitting (`src/onefind/chunking.py`, no dependencies)

`split_passages(body, config) -> list[str]`:
- `config is None`, or the body has at most `max_words` words → `[body]`
  (byte-identical to before; the empty body too).
- Otherwise passages are **exact slices of the body**, in order, cut only
  between words. Units are sentences of prose paragraphs and whole lines for
  headings, list items, quotes, table rows and fenced code; a unit longer than
  `max_words / 2` (run-on or unpunctuated text) is cut into word-aligned
  pieces of `max_words / 8` words. Units are packed greedily up to
  `max_words`; the next passage starts with trailing whole units of up to
  `overlap_words` words (none when the next unit is a heading). A passage
  never ends on a heading when text follows. At most `MAX_PASSAGES = 1000`
  passages per note (~160k words; the rest is still found by keywords).
- A "word" is a run of non-space characters, except that CJK ideographs,
  kana and hangul count one word each (`count_words`). CRLF is handled; one
  huge "word" is never cut.
- Helpers: `normalize_chunking(value)` (None/False/`"off"` → None;
  True/`"on"`/`"default"` → default; dict or JSON text validated, raises
  `UsageError`), `chunking_to_meta`, `chunking_from_meta` (unreadable or
  newer-version values read as off).

### 9.3 Storage (`store.py`)

- `chunks` holds `seq 0..n-1` per note with the passage text; `vec_float`
  holds one vector per chunk `row_id` (as before). Each passage is embedded
  as `compose_embed_text(title, passage)` (= `title + "\n" + passage`), so a
  one-passage note gets exactly the text it always had.
- `add_documents` splits with the setting read once per call and replaces
  a note's passages **and** vectors in the same transaction as its document
  row: kept seqs keep their `row_id` (seq 0 never moves), rows past the new
  end are deleted with their vectors. Unchanged passages of multi-passage
  notes reuse their stored vectors, so editing a long note re-embeds only the
  passages that changed. Encoding still runs with no transaction open.
- `remove_documents`, `forget_root`, `clear` remove every passage and vector.
- `repair()` re-splits (under the current setting) every note whose passages
  are missing or match neither the current nor the built setting, drops their
  vectors and re-embeds when a model is attached; `chunks_fixed` = orphan
  passages + re-split notes; `vectors_missing` counts notes.
- `integrity_check()`: "N documents have out-of-date search data" counts
  notes with missing passages or passages matching no accepted setting (a
  pending rebuild is **not** damage); new problem "N search passages belong
  to documents that were removed".

### 9.4 `Index` API (new)

```python
chunking_config() -> dict | None
set_chunking(config) -> dict | None      # None/"off" | "default"/True | {"max_words", "overlap_words"}
    # stored notes keep their passages; vectors_need_rebuild() reports the work.
    # If no stored note is affected (empty or only short notes) it is marked built at once.
vectors_need_rebuild() -> {"needed": bool, "documents": int, "reason": str,
                           "chunking": dict | None, "long_notes": int, "recommended": bool}
    # documents = notes rebuild_vectors() will re-split; recommended = passage search is
    # off but notes longer than DEFAULT max_words exist (never for eval indexes);
    # reason = one plain sentence ("" when nothing to say). Cached per data_version.
rebuild_vectors(*, progress=None, cancel=None) -> {"documents", "rebuilt", "passages",
        "vectors_embedded", "skipped", "chunking", "complete": True}
    # re-splits only notes that differ, 16 notes per committed batch: a note is
    # always fully old or fully new; cancel() -> OperationCancelled between
    # batches; a rerun continues. Needs the model attached when the collection
    # has vectors (UsageError), works without one for keyword-only collections.
    # progress events {"phase": "scan"|"embed"|"write"|"done", "done", "total", "message"}
    # (counts are notes). Marks chunking_built when nothing is left.
passage_info(doc_ids) -> {doc_id: number_of_passages}
vectors_for_docs(doc_ids)  # one-passage note -> [dim] (unchanged); multi-passage note ->
                           # [n_passages, dim] matrix, row i = seq i; notes with a gap are left out
```

### 9.5 `health()` / `stats()` (existing keys kept)

`health()` keys that callers read as **notes** stay note-level, so serve's and
the CLI's `documents == chunks == fts` check keeps working with long notes:
`chunks` = notes that have passages, `vectors` = notes with at least one
vector, `missing_vectors` = notes with a passage lacking a vector,
`orphan_vectors` = vector rows without a passage (rows). `ok` = every note
has ≥ 1 passage, no passage without a note, one FTS row per note, and with
vectors every passage has one and no vector is orphaned. New keys:
`passages` (rows), `documents_without_passages`, `orphan_passages`,
`chunking`, and with vectors `passage_vectors` (rows) and
`missing_passage_vectors` (rows). `long_documents` now counts notes with a
*passage* longer than the model window (~4 chars/token), i.e. normally 0 with
chunking on.

`stats()`: `chunks` and `vectors` stay **row** counts (`count("chunks")`,
`count("vectors")`); new keys `passages` (= `chunks`), `chunking`,
`long_notes` (notes longer than one passage; with chunking off, notes over
the default 200 words, approximated in SQL) and `vectors_need_rebuild` (the
dict above). All are cheap: the census is cached per `PRAGMA data_version`
and the connection's own change count.

### 9.6 Retrieval (`search.py`, `snippets.py`)

- **Semantic**: float KNN runs over passages and keeps each note's best
  passage (max similarity). One-passage collections fetch exactly `k` rows
  as before; otherwise `ceil(k * passages / notes) + k` rows, doubling until
  `k` distinct notes are found or the index is exhausted; above sqlite-vec's
  KNN limit (4096) it switches to the exact block scan. int8, binary and
  filtered block scans aggregate the same way (rows visited best-first per
  block until `k` distinct notes, ties included). Order stays score desc,
  `doc_id` asc; within a note the lowest seq wins ties.
- **Hybrid** fuses the note-level semantic ranking (unchanged RRF/linear).
  **Rerank** scores each candidate by its best passage. **Confidence** labels
  use the best passage's cosine (binary: the best passage's float cosine);
  thresholds unchanged (9.1).
- `Hit` gains `passage_seq`, `passage_count`, `passage` (text of the best
  passage, only for multi-passage notes) — `compare=False` presentation
  fields like `explain`; filled for semantic/hybrid hits found by meaning.
  `semantic_search(..., _with_passages=True)` is a private switch.
- **Explain** adds `best_passage_seq` (0 = beginning; None for keyword-only
  hits) and `passages` (count, None in lexical mode), plus the reason
  "Closest passage is further down the note (passage i of n)" when
  `best_passage_seq > 0`.
- **Snippets**: `build_snippet(hit, body, query, *, max_chars=320,
  passage=None)`; `passage` defaults to `hit.passage`. Order: highlighted
  window of the matched passage → highlighted window of the body → FTS
  `snippet()` with markers → unhighlighted matched passage (with `…` where the
  note continues) → as before. serve.py needs no change to benefit.

### 9.7 Jobs (`jobs.py`)

```python
JobManager.start_rebuild_vectors(db_path, *, model_name=None, chunking="keep", label=None) -> dict
```
Same job dict shape as `start_index` with `"kind": "rebuild_vectors"`,
`"root": None`, `"options": {"chunking": "keep" | setting}`. `chunking` first
changes the setting (`"default"`/True = on, None/`"off"` = off, or a dict;
invalid → `UsageError` before the job starts). Steps: `scan` (finding notes
that need new passages; `counts.found`), `index` (writing passages;
`counts.read/saved`), `vectors` (embedding; `counts.embedded`; `skipped` for
a keyword-only collection), `ready`. Phases `model`, `scan`, `write`/`embed`,
`done`; counts are notes. The collection's model loads through the shared
cache (`model_name` matters only without one). One job per collection,
shared with indexing jobs (`UsageError("an indexing job is already
running")`). Cancelled: `"Rebuilding vectors was cancelled. Notes finished so
far keep their new passages; run it again to finish."`. Errors use the same
codes as index jobs (`model_unavailable`, `model_mismatch`,
`vectors_unavailable`, `collection_unreadable` for a missing file, …).
`jobs.JOB_KINDS = ("index", "rebuild_vectors")`, `jobs.KEEP_CHUNKING = "keep"`.

### 9.8 For the serve/UI integrator (wired in 1.2.1 — see the last Deviations entry)

The checklist below was the hand-over note; every item is implemented as described in the
2026-10-14 "chunking wired into the app" deviation (`vectors_need_rebuild` keeps its store name
in `/api/stats`, and `long_notes` switched to `stats()["long_notes"]`).

- `GET /api/stats`: pass `snapshot["vectors_need_rebuild"]` through (e.g. as
  `vectors_rebuild`), plus `chunking`; show "Rebuild vectors" when
  `needed`, and an "Improve meaning search for long notes" offer when
  `recommended` (it sends `chunking: "default"`). Keep the existing
  `long_notes` (= `health.long_documents`) or switch it to
  `stats()["long_notes"]` deliberately — they now mean different things.
- New endpoint, e.g. `POST /api/maintenance/rebuild-vectors {"chunking":
  "keep"|"default"|"off"}` → 202 job dict from
  `jobs.start_rebuild_vectors(db, chunking=...)` (CSRF header; `UsageError`
  "already running" → 409 `collection_busy`; other `UsageError` → 400). Strip
  `db` before embedding the job in `/api/stats`, as for index jobs; the UI
  polls it like an index job (`kind` tells them apart).
- Results: `explain.best_passage_seq` / `explain.passages` can label "match
  found further down"; snippets already show the matched passage.

## 10. Auto-sync (v1.2.1)

Keeps a collection's index up to date while the user's note files change.
**No new dependency** (no `watchdog`): efficient polling in
`src/onefind/watcher.py`, with the actual work done by ordinary `JobManager`
index jobs. `ingest.py` and `jobs.py` are used unchanged.

### 10.1 Preference

`schema_meta` key `auto_sync` = `"on"` | `"off"` (absent = off), read/written
with `watcher.get_preference(index)` / `watcher.set_preference(index, bool)`
(`Index.get_meta`/`set_meta`). It lives in the collection file, so it travels
with a backup or a copied `.db`. Existing collections default to **off**;
the UI wizard sends `auto_sync: true` by default for new (or still empty)
collections. `Index.clear()` drops it (with every folder), like all
non-identity meta keys.

### 10.2 `watcher.AutoSync`

```python
AutoSync(job_manager, *, interval=None, max_interval=120.0, cooldown=10.0,
         allow_network=False, on_event=None, stop_timeout=10.0)
start(db_path, roots=None, *, interval=None, allow_network=None) -> status
    # roots: [{"root_id", "path", "exclude", "allow_network"?}] fixed list;
    # None = re-read Index.roots() on every poll (folders added later are picked up)
    # idempotent: at most one watcher thread per db path ("onefind-autosync-N", daemon)
stop(db_path, timeout=None) -> status      # cancels an in-flight auto-sync job, joins
shutdown(timeout=None)                     # stop all; start() then raises UsageError
status(db_path) -> dict
running(db_path) -> bool ; watched() -> [db path] ; poke(db_path) ; blank_status()
preempt(db_path, *, timeout=15.0, cooldown=None) -> bool
    # make way for a manual action: no new auto-sync job for `cooldown` s
    # (>= 2 poll intervals), cancel + wait for a running one
```

Status (never contains the database path):
`{"enabled": bool, "state": "idle"|"scanning"|"syncing"|"paused"|"error",
"last_scan_at": iso|null, "last_sync_at": iso|null,
"pending": {"new", "changed", "deleted"}, "message": str, "interval_s": float,
"syncs": int, "last_changes": {"added","updated","removed"}|null,
"progress": int|null, "skipped_roots": int}`. `syncs` counts finished runs
that changed something (the UI refreshes quietly when it grows); `progress`
is the running job's percent; `skipped_roots` counts network folders left
alone. `on_event(event)` gets `{"type": "started"|"changes"|"sync_started"|
"synced"|"paused"|"error"|"stopped", "db", ...}` (`changes`/`sync_started`
carry `root,new,changed,deleted`; `synced` carries
`root,added,updated,removed,seconds`); `paused`/`error` fire on transitions only.

### 10.3 One poll

1. Collection file gone: state `idle` with a message, and the thread ends (self-stop).
2. In a `preempt` cooldown, or while **any** job is active for the db
   (`JobManager.active_for`): `paused` ("Waiting for indexing to finish.").
   Afterwards every folder is compared against the index again.
3. Open the db read-only (`Index.open(readonly=True)`, falling back to a
   normal open for old schemas), read roots and `has_vectors()`, close.
4. Per folder: skip network folders (UNC `\\host\share`, `\\?\`, and on
   Windows mapped drives via `GetDriveTypeW == DRIVE_REMOTE`, no I/O) unless
   `allow_network`; take a **stat-only snapshot** (`snapshot_root`: `os.scandir`
   walk, relative posix path to `(mtime_ns, size)`, `SUPPORTED_SUFFIXES`
   only, `DEFAULT_EXCLUDES` plus the folder's saved patterns via
   `ingest.is_excluded`, excluded dirs never entered; `files_only` roots stat
   only their indexed files). A missing or unlistable folder is kept as is,
   with a plain message. A folder that suddenly has **no** notes while the
   index holds 5 or more of them is left alone too (sync client, unplugged
   disk); the message says to update from Manage if that was intended.
5. **Debounce:** a folder is synced only when its snapshot equals the
   previous poll's (stable for 2 consecutive polls) and differs from the
   snapshot last synced. Until then the state is `scanning` with stat-level
   `pending` counts.
6. `scan_changes(index, root, exclude)` confirms (hash compare, so a touched
   but unchanged file runs no job). Any new/changed/deleted file starts
   `JobManager.start_index(db, root, exclude=..., embed=index.has_vectors(),
   prune=True, incremental=True, label="Auto-sync", refresh_files=files_only)`.
   That is the manual-reindex embed policy: a keyword-only collection never
   loads the model. The watcher waits for the job (state `syncing`), then
   records the snapshot as synced. A cancelled job (preempt/stop) means `paused`.
7. **Interval:** `interval_s = clamp(max(base, 40 x walk seconds), base,
   max(base, max_interval))`, about 2.5% of one core at most, so large trees
   are polled less often (the message says so). Base = the `interval`
   argument, else `$ONEFIND_AUTOSYNC_INTERVAL` (seconds, >= 0.02), else 5 s.
8. **Errors** (unreadable db, failed scan, failed job, anything unexpected)
   give state `error` with a plain message and a retry after
   `min(300, interval x 2^failures)`. The thread never dies from an error.

### 10.4 HTTP API (`serve.py`)

`create_app(..., autosync=True, autosync_interval=None)`; `app.state.autosync`.
There is one `AutoSync` per app, created with the runtime and shut down in the
lifespan **before** `JobManager.shutdown`. It follows the active collection:
startup, every switch (`switch_to`, open, restore-as-new, remove then next
active) and a lazily opened index start the watcher when the collection's
preference is on, and stop every other one. Restore-replace, delete-file /
remove and the legacy reset stop the watcher before touching the file; clear
re-evaluates (the preference is gone).

| Method & path | Body | Response |
|---|---|---|
| GET `/api/autosync` | – | status (10.2) with `enabled` = the saved preference, plus `available` (a collection is open). Preference on but no watcher: `state: "paused"` with a message (with `--no-auto-sync`: "Auto-sync is turned off for this server"). |
| POST `/api/autosync` (CSRF header) | `{"enabled": bool}` | saves the preference in the active collection, starts/stops watching, returns the status; 503 `no_index` without a collection |
| GET `/api/stats` | – | adds `"auto_sync"`: the same status. An auto-sync job is **not** reported as `job` and does not make `status` `"indexing"` (background upkeep keeps the collection `"ready"`). |
| POST `/api/index` | adds `"auto_sync": bool \| null` | a bool saves the preference after the job started (the watcher waits for that job); null leaves it alone |

Every endpoint that calls `ensure_not_busy` (index, roots remove/relocate,
collection remove, maintenance check/repair/compact/clear/restore, reset)
first calls `AutoSync.preempt`, so **a manual action always wins**: the
auto-sync job is cancelled (finished batches stay) and no new one starts
during the cooldown. Status messages never contain the database path (it is
replaced by "the collection file").

### 10.5 CLI

- `onefind watch [--db FILE | --collection NAME] [--interval S] [--duration S]
  [--allow-network]` runs auto-sync in the foreground (whatever the saved
  preference), printing one timestamped line per change, sync, pause or error
  until Ctrl+C (exit 0) or `--duration`. It exits 4 when the collection has no
  folders or its file disappears. `watch --set on|off` only saves the preference.
- `onefind serve` honors each collection's saved preference;
  `serve --no-auto-sync` never watches.
- `onefind status` prints `auto-sync: on|off` (`--json`: `"auto_sync"`).

### 10.6 Frontend

- Header pill: `#status-label` keeps its text ("Ready"); a quiet
  `#status-sync` suffix shows "· Auto-sync on" / "· Auto-sync paused" /
  "· Auto-sync problem" (warn tone) and is hidden below 640 px. While a sync
  runs, or settled changes are pending, the pill reads "Syncing N changes…"
  with the busy pulse. It is never announced to screen readers and never
  opens the job dialog.
- Manage → Health: the `#autosync-toggle` switch with the plain explanation
  and an `#autosync-status` line (last synced time, notes changed, pause or
  error text).
- Index wizard: `#index-autosync` "Keep this collection up to date
  automatically", on for a new or empty collection and otherwise the
  collection's current setting; sent as `auto_sync` (`wizardIndexBody({autoSync})`).
- Polling: `/api/stats` every 5 s while auto-sync is on and every 1.5 s while
  it is scanning/syncing (15 s otherwise). When `auto_sync.syncs` grows for
  the same collection, the UI refreshes facets, the home lists and an open
  Manage panel. It re-runs visible results and shows a 2.5 s "Index updated ·
  N notes changed" info toast **only** when the person has not typed or
  clicked for 8 s and no dialog is open.

### 10.7 Integration notes

- Behaviour change: with auto-sync on, a note removed with "Remove from
  index" comes back the next time something in its folder changes (the same
  as "Update index"). Exclude patterns keep it out.
- `jobs.py` needs no hook: the watcher uses `start_index`, `active_for`,
  `wait`, `get`, `cancel` and the job `label`. Changing those signatures, or
  the meaning of `label`, breaks auto-sync.
- `tools/ui_check.py` sets `ONEFIND_AUTOSYNC_INTERVAL=0.5`, unticks the
  wizard box for "My Notes" (the walkthrough edits that folder on purpose to
  check stale detection) and has its own auto-sync step.

## Deviations

_(Implementers: append a dated line per deviation.)_
- 2026-10-13 (library.py, additive): registry entries and `list()/get()` dicts carry an extra `"slug"` (unique, stable across renames; names the default db file and backups). Extra API: `Library.find_by_db(path) -> dict | None`, `Library.registry_path`, module helpers `validate_name()` / `slugify()`, and errors `NameTakenError(UsageError)` (map to 409 `name_taken`) and `AlreadyRegisteredError(UsageError)` (`.existing` = name already using that db). `register(None, path)` uses the file stem as the name. `create()`/`register()` make the new collection active when none is active. A `collections.json` with `version` > 1 raises `DataError` and is left untouched; malformed individual entries are dropped after copying the original to `collections.json.corrupt-<ts>`.
- 2026-10-13 (store.py, additive): extra `Index` API `get_root(path) -> dict | None`, `remove_root(root_id) -> bool` (docs keep, `root_id` set NULL), `document_manifest(root_id=None)` (doc_id -> hash/mtime/size/root_id/title/source/meta_json, no bodies), `update_metadata(rows)` (mtime/size/root/folder/ext refresh without re-indexing), `last_indexed_at()`; module helpers `normalize_tags()`, `USER_TABLES`, `HISTORY_LIMIT`/`RUNS_LIMIT`. `duplicate_groups(limit, *, root_id=None)` gains a keyword-only root filter. Extra index `idx_documents_root(root_id)`. `get_documents()` dicts additionally carry `folder`, `ext`, `mtime`, `tags`. `add_documents(progress=)` emits ONE event per committed batch whose phase is `"embed"` when an embedder is attached, else `"write"`. `record_search` ignores blank queries; `save_query` with an existing name (case-insensitive) replaces that entry (same id); `add_bookmark`/`record_open` raise `DataError` for unknown doc_ids; `index_runs` is capped at 200 finished rows. `clear()` keeps the identity keys `schema_version`, `collection_name`, `created_at` in `schema_meta`. `repair()` returns `{fts_rebuilt, chunks_fixed, orphan_rows_removed, orphan_vectors_removed, vectors_embedded, vectors_missing (None when no vectors), health, ok}`; `integrity_check()['health']` is None when sqlite-vec cannot load for a vector index. Migration also sets `schema_meta.created_at` (oldest document `created_at`).
- 2026-10-13 (ingest.py, additive): `ingest_path` result also has `root_id` and `run_id`. `exclude=None` reuses the patterns stored on that root (a list, even `[]`, replaces them). Skip reasons are the fixed strings `"too large"`, `"binary"`, `"empty"`, `"unreadable"`; with `prune=True`, files now skipped as too large/binary/empty are pruned too, unreadable ones are kept. A heading-only note is still indexed (empty body). Exclude matching is case-insensitive and also matches parent-directory prefixes (`notes/drafts`). Invalid paths raise before a run is recorded; a folder with nothing indexable raises `CorpusNotFoundError` (run = failed, a newly created root is unregistered) unless `prune=True` removed documents. Extra helpers: `normalize_excludes`, `is_excluded`, `list_files`, `parse_front_matter`, `read_text_document`; `load_documents(root, exclude=None)` applies `DEFAULT_EXCLUDES` and silently drops skipped files. `tests/test_ingest.py::test_reindex_is_idempotent` now ignores `stats()['last_indexed_at']` (a timestamp that legitimately moves on re-run).
- 2026-10-13 (jobs.py + embed.py, additive): job dicts also carry `label`, `embed` (requested value, then the decided bool), `model` (name or null), `options` `{exclude, prune, incremental}`, `counts` `{found, read, saved, embedded}` (countable progress for the UI; `done`/`total` mirror the current ingest phase and are monotonic per phase, not across phases), and `health` (`Index.health()` after success, else null). `error` is `{"code","message","retryable","action"?}` (codes: `path_not_found`, `nothing_to_index`, `permission_denied`, `model_unavailable` (action `switch_mode`), `vectors_unavailable`, `model_mismatch`, `collection_busy`, `collection_unreadable` (action `repair`), `read_failed`, `invalid_request`, `indexing_failed`, `internal_error`); cancelled jobs have `error: null` and their interrupted step returns to `pending` (failed jobs mark it `failed`). Phase is `model` while the embedder loads/attaches (step `scan` shows `active` then). `JobManager(embedders=None)` creates its own cache; extra API: `JobManager.wait(job_id, timeout=None)`, `active_for(db_path)`, `shutdown(timeout=5.0)`; `get/cancel/wait` raise `jobs.JobNotFoundError(DataError)` for unknown ids (map to 404). `EmbedderCache(factory=None)`, `get(model_name=None)`/`loaded(model_name=None)` default to `DEFAULT_MODEL`; extra `peek()`, `put()`, `available()`, `names()`, `clear()`; failed loads are not cached. Module helper `model_stack_available()` (find_spec, no import). `embed=None` also requires sqlite-vec (`index.vec_ready`) before choosing vectors. `start_index` raises `UsageError` for a blank db/root or a non-bool `embed`. Note: the job dict contains `db` (the path) per section 5 — strip it before embedding a job in `/api/stats`.
- 2026-10-13 (frontend, additive): live keystroke searches send `record: false`; once the user pauses (~1.5 s) on a live result the UI records it with `POST /api/history` body `{"query", "mode", "precision", "filters", "result_count"}` (the arguments of `Index.record_search`). Enter / clicks / mode changes send `record: true` as before. A 404/405/422 from `POST /api/history` is ignored silently, so the endpoint is optional but recommended.
- 2026-10-13 (frontend): the UI is split into ES modules (`app.js` entry + `api.js`, `store.js`, `search.js`, `view.js`, `views-*.js`, …) loaded with `<script type="module">`; serving every `^[a-z0-9][a-z0-9-]*\.(js|css|svg)$` file with a JavaScript content type (§6) is required, otherwise the page cannot start. `tests/test_static_ui.py::test_server_serves_every_module` xfails until `create_app` gains the v1.2 `home` parameter, then enforces it. Recommend `Cache-Control: no-cache` on `/assets/*` so upgraded modules are never mixed with cached ones.
- 2026-10-13 (frontend): list endpoints are read tolerantly (`{"items": [...]}`, `{"collections": [...]}` or a bare array). `POST /api/index` is sent with `embed: null` when "Build vectors" is ticked (auto per §5) and `embed: false` otherwise; "Reindex with vectors" sends `embed: true` for every root listed by `GET /api/index/changes`, one job after another. The wizard's "new collection" path calls `POST /api/collections` then `POST /api/collections/active` before `POST /api/index`.
- 2026-10-13 (search.py + snippets.py, additive): `SearchFilters` normalizes in `__post_init__` (direct construction is validated too); `from_dict` rejects unknown keys, accepts a single string for a list field, and treats `None`/`""` dates as unset; date-only strings are *local* calendar days and `modified_before` with a date includes that whole day; `folders` match case-insensitively (SQL `LIKE`), `""` = root-level documents only; rows with NULL metadata (JSONL / pre-v3) never match a filter of that kind. `to_dict()` is compact (only set fields). `filters=` also accepts a plain dict everywhere, and `lexical_search`/`semantic_search`/`hybrid_search` gain keyword-only `filters=` (hybrid also `explain=`, `on_stage=`). `SearchOutcome` has an extra `confidences: list[str]` aligned with `hits` (filled even when `explain=False`); `abstained` is False when there are no hits. Explain adds `fusion`, `reranked`, `rerank_score` (hybrid) and `hamming_distance` (binary); for binary `semantic_score` is `1 - 2*hamming/dim` while `Hit.score` stays `-hamming`. Binary precision uses its own calibrated thresholds `BINARY_STRONG_SIM`/`BINARY_WEAK_SIM` (sign-bit similarities run ~0.6x cosine). Hybrid docs found by only one leg take that leg's confidence. Unfiltered int8/binary now scan through the same linear block iterator as filtered search (`search._vector_blocks`) instead of `Index.iter_vector_blocks`. Extra helpers: `query_terms`, `semantic_confidence`, `lexical_confidence`, `binary_similarity`, `SEARCH_MODES`, `PRECISIONS`, `STAGE_NAMES`. `snippets.build_snippet(..., max_chars=320)` prefers the body passage and falls back to the FTS `snippet()` markers when the passage has no highlightable match; extra exports `FTS_START`, `FTS_END`, `ELLIPSIS`, `collapse_whitespace`, `stem_variants`, `MAX_HIGHLIGHTS`.
- 2026-10-13 (integration check: ingest.py, store.py, jobs.py; backward compatible): **multi-root doc_ids.** A file's doc_id is still its relative path, except when that id already belongs to a *different, still-registered* root in the same collection (two folders both holding `readme.md`); it then gets `ingest.qualified_doc_id(root_id, relative)` = `"~<root_id>/<relative>"`. A file keeps whatever id it already has in its root, and rootless (pre-v3/JSONL) or unregistered-root documents are taken over in place, so single-root collections and v2 migrations are unchanged. Before this, indexing a second folder silently overwrote the first folder's same-named notes. `source`/`folder` stay plain relative paths, so serve must locate a file as `roots()[root_id].path / source`, **never** from `doc_id`. `scan_changes` matches files by `source`. `Index.attach_embedder(embedder, *, progress=None)` reports the backfill of notes that have no vectors as `{"phase": "embed", "done", "total"}` per batch; jobs show it as phase `embed`, with `counts.embedded` and the `vectors` step active.
- 2026-10-14 (search.py + snippets.py, additive; search-quality pass): **stopwords + relaxed keyword matching.** New module constants `STOPWORDS` (compact English function words; deliberately excludes content-prone words such as "may", "will", "us", "won", "d") and `LEXICAL_MATCHES = ("all", "any", "auto")`; helpers `is_stopword()`, `content_terms()`, `keyword_terms()`, `any_terms_fts_query()`. Explain `matched_terms`/`missing_terms`/keyword reasons and keyword confidence count only non-stopword terms (all terms when the query is only stopwords); `query_terms()` itself is unchanged. Snippet highlighting and `best_passage` scoring ignore stopwords on the same rule, and the FTS-marker fallback in `build_snippet` un-highlights stopwords. New keyword-only `lexical_match="all"` on `lexical_search`, `hybrid_search`, `search`, `search_detailed` (and `Index.search` via `**options`); the default `"all"` is byte-identical to before (verified: same rankings on sample-data and showcase-data). `"any"` = OR of the quoted non-stopword whitespace tokens, BM25-ranked. `"auto"` = all-words hits first, topped up with any-words hits when fewer than k (the hybrid lexical leg uses `fetch_depth`); appended hits' BM25 scores are rescaled to sit just below the weakest all-words score so scores stay monotonic for linear fusion; the OR query is skipped when it could not add rows (single token). `SearchOutcome` gains `relaxed: bool` (auto topped up with partial matches) and `strict_count: int | None` (all-words hits in the lexical leg; None for semantic mode or `"any"`) so the UI can say "No note contains all your words" only when `strict_count == 0`. Explain gains `keyword_coverage` (`"all"|"some"|"none"|None`) and, for lexical/hybrid, `lexical_match`. Confidence for hits from the relaxed pass: `lexical_confidence(matched, total, partial=True)` is moderate only when more than half of the keywords match (2 of 3 -> moderate, 1 of 2 -> weak), and a relaxed hybrid hit in both legs is strong only if it matches every keyword, otherwise it takes the better leg's label; without this, a lone common word kept off-topic questions from abstaining. An invalid `lexical_match` raises `UsageError` in every mode. serve.py does not pass `lexical_match` yet; the UI wiring should use `"auto"`.
- 2026-10-14 (cli.py, backward compatible): read/maintenance commands (`search`, `status`, `doctor`, `compact`, `backup`, `remove`) take `--db`/`--collection` and, when neither is given and `./onefind.db` does not exist, fall back to the library's active collection (with a stderr note); they never create an empty database, so `search` against a missing `onefind.db` now exits 4 with a hint instead of silently creating the file. `index` keeps creating `./onefind.db` (stderr hint when an active collection exists); `index --collection NAME` creates an unknown collection. `serve` passes `home`/`allow_open` to `create_app` only when needed (no `--db`, `--home`, or `--no-open`) and exits 2 in library mode while `create_app` lacks `home`. Extras: `index -q/--quiet`; `search` prints a note when every hit is weak and a filter hint on empty filtered results; `collections list --json`; `collections create NAME --db EXISTING.db` registers an existing file; `status --json`/`--no-scan`; `backup --out FILE|DIR [--overwrite]` and `backup --list` (default target: `Library.backup_path_for(name)` for registered collections, else `<stem>-backup-<ts>.db` beside the file); `restore` accepts a bare file name from the backups folder and copies the backup via the SQLite backup API into `collections/<slug>.db` (the backup itself is never opened in place). Exit codes: `doctor` returns 4 while problems remain; `OperationCancelled`/Ctrl+C -> 130 (first Ctrl+C during `index` cancels between files/batches, a second one aborts); an uncaught `sqlite3.Error` -> 4. `tests/test_envcheck.py` subcommand assertions updated for the new subcommands (`serve` no longer requires arguments).
- 2026-10-14 (serve.py, additive): `create_app` gains keyword-only `opener=None` (`opener(path: Path, reveal: bool)`, default `serve.default_opener`; also replaceable at runtime via `app.state.opener`), `embedders=None` (shared `EmbedderCache`; tests inject a fake-model cache) and `register=None`. **Registration of `index_path`:** it is added to the library (under its `collection_name` meta or file stem, `" 2"`… on a name clash) only when `register=True`, or by default when `home` is given or `$ONEFIND_HOME` is set — so `create_app(db)` / `serve --db X` never edits the per-user registry (keeps `tests/test_serve.py` from writing to `%APPDATA%`); otherwise the db is an unlisted ad-hoc collection (`GET /api/collections` lists it as the active entry with `"registered": false`; backups go to `backups_dir/<slug>-<ts>.db`). The server keeps its own active collection: startup with `index_path` does not change `collections.json`'s `active`; `POST /api/collections/active` and `/open` and restore-as-new set both. `app.state.runtime/jobs/embedders/opener/allow_open` are exposed. Semantic search binds the cached model with `index.embedder = …` (after a `model_dim` check) instead of `attach_embedder`, so the read path never backfills/writes vectors; the model loads outside the index lock. Response details: `/api/search` adds top-level `query`; `meta.stages` ends with `{"name": "build"}`; `meta.filters` is the compact applied filter dict (`{}` when none); `explain` is null when `explain=false` but `confidence` is always set. NDJSON errors carry `status` both inside `error` and at the top level (`api.js` reads the top-level one). Error codes beyond the contract: `invalid_filters` (400), `search_error` (400), `vectors_unavailable`/`model_unavailable` (503, action `switch_mode`), `model_mismatch` (409, action `reindex`), `collection_busy` (409, also for a second `POST /api/index` on a busy collection and for repair/compact/clear/restore-replace/reset while a job runs), `confirmation_required` (400), `path_outside_root` (403) for `documents/open`, `open_failed` (500), `already_registered`/`invalid_collection`/`not_registered`, `invalid_backup` (400), `unsupported_file`/`invalid_path` (400), `data_error` (400). `POST /api/collections` returns 201 and requires a `.db`/`.sqlite`/`.sqlite3` `db_path`; it switches the server to the new collection only when none is active. `POST /api/index` with no active collection creates one named after the folder. `GET /api/fs/list` also returns `total` and `truncated`; non-loopback is not restricted there (only `documents/open`). `GET /api/history` takes `?limit=` (default 10); `POST /api/history` returns `{"items": recent_searches(10)}`; `/api/history/clear` returns `{"cleared": n, "items": []}`; `DELETE /api/saved/{id}` → `{"deleted": true, "id"}` or 404; `DELETE /api/bookmarks/{doc_id}` → `{"removed": bool, "doc_id"}`; `/api/recent-documents` items replace `mtime` with `modified_at`; `/api/documents/open` → `{"opened": true, "doc_id", "reveal"}`; `/api/collections/remove` → `{"removed", "deleted_file", "active"}`; restore → `{"restored": true, "mode", "collection"}` (new) or `{"restored": true, "mode", "safety_backup": {path, bytes}|null, "stats"}` (replace); clear → `{"cleared": true, "stats"}`. `/api/index/changes` root reports add `root_id`, `documents`, `last_indexed_at`. `/` and `/assets/*` send `Cache-Control: no-cache`, `/api/*` `no-store`; `openapi_url` is disabled. Workaround: `POST /api/maintenance/check` first reads `fts` because a long-lived connection's cached FTS5 structure makes `PRAGMA integrity_check` report "malformed inverted index" after another connection (an index job) wrote — `Index.integrity_check()` itself should do this refresh. `tests/test_static_ui.py::test_server_serves_every_module` now asserts the `home` parameter instead of xfailing.
- 2026-10-14 (serve.py + frontend, additive; live browser integration): `/api/search` and `/api/search/stream` now call `search_detailed(..., lexical_match="auto")` for `lexical` and `hybrid` modes (`"all"` for `semantic`), and `meta` gains `relaxed` (bool, partial-match top-up happened) and `strict_count` (int|null, all-words hits in the keyword leg). The UI shows "No note contains all of your words…" (`#results-note`) only when `relaxed && strict_count === 0` in keyword/hybrid mode, and an empty keyword result with a known `strict_count` says no word matched at all. The index wizard's completion copy reads vectors from the finished job's `health.vectors` (stats are refreshed afterwards). Toasts are moved into the topmost open modal `<dialog>` (`dialogs.placeToasts()`), because a modal makes the rest of the page inert and covers it. Manage → Maintenance gains "Add a folder…" (opens the wizard for the active collection). `tools/ui_check.py` is the live end-to-end walkthrough (real server with `--no-open` + Chrome); it polls with `page.evaluate` because Playwright's `wait_for_function` uses `eval`, which the app's CSP blocks.
- 2026-10-14 (frontend polish, UI-only; no API change): applied the four v1.2 design reviews. Behaviour other code/tests may rely on: result cards show Open / Preview / Explain (`.why-button`) / More; Copy snippet, Bookmark, Show in folder and Filter to this folder moved to one shared `#card-menu` (keyboard o/c/b/e unchanged); bookmark state shows as `.meta-bookmark` on the card. Filter pickers are folded behind `#filters-toggle` at every width (active chips stay visible) and apply a keyboard selection on Enter/blur, not on every arrow key. A refresh of visible results (filter/mode/count change, refined query) keeps the dimmed results (`.is-refreshing`) and switches to the loading pipeline only after 8 s; a single typed character no longer leaves the home screen. `body[data-view]` mirrors the visible view (`starting` before the first `/api/stats`). Toasts with an action stay until used or dismissed; repeated messages are de-duplicated. New per-viewer preference `onefind.singleKeys` (localStorage via `store.storage`) turns off single-key shortcuts. Dialogs animate opacity + inset (never transform) so toasts moved into them stay in the window corner. `views-results.js` exports `exportName()` (Unicode-safe file names) and `markPreviewed()`. `tools/ui_check.py` gains `--release DIR` (numbered light/dark x 1280/375 screenshot set, see `screenshots/v1.2/README.md`) and a 700 px responsive pass; its collection-row selectors are now `button[aria-label^='Delete file']`. serve.py was not touched.
- 2026-10-14 (review-fix pass: store.py, ingest.py, jobs.py, serve.py, library.py, embed.py; additive unless noted): **re-indexing inside a root** — a folder or file inside a registered root is indexed as part of that root (same doc_ids, rows updated in place, no second root, prune limited to that subfolder); indexing a parent of registered roots merges them into it (`Index.adopt_root`, doc_ids kept); `scan_changes` treats subfolder paths the same way and counts a file as new only if ingest would index it. New `Index.relocate_root`, `Index.forget_root`, endpoints `POST /api/roots/relocate {root_id, path}` and `POST /api/roots/remove {root_id | root, delete_documents}`. New optional column `collection_roots.files_only` (added on open; a read-only file works without it) for roots registered by indexing a single file: change detection looks only at the indexed files, and `POST /api/index` gains `scope` (`"auto"` default = refresh only those files; `"folder"` = index the whole folder and clear the flag; the wizard sends `"folder"` when a folder is picked). `attach_embedder(..., cancel=)` encodes with no transaction open and commits per batch (a cancel/error rolls back only the batch in flight); the CLI and jobs pass their cancel flag. `integrity_check` reads FTS first (the serve.py workaround is gone) and lock contention raises -> 409 `collection_busy`; `/api/maintenance/check` refuses while a job runs. Vectors present but sqlite-vec unloadable -> stats `ready`, keyword-only, `vectors_unavailable: true`. `clear()` drops the vector table and detaches the model. Creating a collection on an existing file -> 409 `path_exists` (action `open_collection`). `/api/documents/open` only opens supported note types (checked before and after resolving symlinks), else 403 `unsupported_file`. Network roots (UNC) are never scanned automatically (`/api/index/changes` reports `network: true`); opening a note there or indexing a network path returns 409 `confirmation_required` (action `confirm_network`) until the request sends `confirm_network: true` (the UI asks, then resends); `/api/collections/open` returns `network_roots`. The model loads from the local cache with no network requests (download only when missing). Folders that cannot be listed are recorded and their notes never pruned; an unlistable root fails the run before writes. `start_run` marks runs left `running` by a dead process as failed ("interrupted"). Renamed/moved notes keep bookmarks and open history when content is identical. Non-UTF-8 text decodes as cp1252, then latin-1. `collections.json` writes take a cross-process lock (`collections.json.lock`). `Index.open(path, readonly=True)`. Model window size stored; `health()` reports `long_documents` and stats `long_notes` (whole-document embedding is unchanged: real chunking is still open). `save_query` replacing an existing name also refreshes `created_at` (relative date presets are worked out from the latest save). **Changed:** search history `result_count` counts strong (non-weak) results only, from both `record: true` searches and the UI's deferred `POST /api/history`.
- 2026-10-14 (review-fix pass, security): the API guard returns 403 `csrf_check_failed` for any `/api/` request with `Sec-Fetch-Site: cross-site|same-site`, and `GET /api/fs/list` now also requires `X-OneFind-Request: 1` (`api.js` sends it on every request). The folder picker refuses UNC and device paths before touching the file system (400 `invalid_path`). Reset tokens are compared as bytes and `create_app`/`serve --reset-token` reject tokens outside 16-256 characters (`UsageError`). `serve` never defaults to `allowed_hosts=["*"]`: loopback names, the bind address (IPv6 also bracketed, so `--host ::1` works) and repeatable `--allowed-host NAME` are accepted; `0.0.0.0`/`::` without `--allowed-host` exits 2; any non-loopback bind disables opening files. Database files are validated/copied with a `mode=rw` read connection (fallback `mode=ro`) so no `-wal`/`-shm` files are left behind (CLI and serve).
- 2026-10-14 (review-fix pass: search.py + snippets.py): (a) hybrid RRF exact ties are broken by semantic rank, then `doc_id` (`rrf_fuse(prefer=)`; without `prefer` the old tie-break is unchanged). (b) For `lexical_match="auto"`, the strict pass and `strict_count`/`relaxed` mean "contains every keyword, stopwords excluded" (`all_keywords_fts_query`); `"all"` is unchanged; the UI copy says "No note contains all of your keywords…". (c) Binary-precision confidence labels use the float cosine of the returned hits' stored vectors (explain adds `float_similarity`); `BINARY_STRONG_SIM`/`BINARY_WEAK_SIM` are only the fallback when a float vector is missing; ranking is unchanged. Vector block scans (`search._vector_blocks` and `Index.iter_vector_blocks`) hold one read transaction so concurrent commits no longer fail a search; if the safety-net version check still trips, `/api/search` returns a retryable 409 `index_changed`. Snippets skip a title-only FTS fallback when a body passage exists and strip Markdown syntax (table pipes -> "Field: Value", list/quote/task markers, links, bold, backticks).
- 2026-10-14 (review-fix pass: cli.py): `search --match all|any|auto` (default `auto` for lexical/hybrid, `all` for semantic, same as the app; a partial-match note goes to stderr). `smoke --match` is passed through only when `evaluate.run_smoke` accepts `lexical_match` (it does not yet, so `smoke` keeps `"all"`). A missing/unloadable model is exit 3 with a message (hybrid search falls back to keyword with a note; an `index --embed` run that just created an empty collection removes it). Every fix command printed by `status`/`doctor` includes `--collection NAME` or `--db <absolute path>`. `remove` prints the exact re-index command with the folder's saved `--exclude` patterns. `backup --out PATH` treats PATH as a folder when it exists, ends with a slash, or is a new path without an extension (created on demand).
- 2026-10-14 (auto-sync, additive; new `watcher.py`, serve.py, cli.py, frontend): section 10 describes the shipped behaviour. Beyond section 6: `GET/POST /api/autosync`, `/api/stats` gains `auto_sync` and hides auto-sync jobs from `job`/`status`, `POST /api/index` accepts `auto_sync`, `create_app` gains `autosync`/`autosync_interval`, and `ensure_not_busy` preempts auto-sync. CLI: new `watch`, `serve --no-auto-sync`, and an `auto-sync` line in `status`. `ingest.py`/`jobs.py` are unchanged.
- 2026-10-14 (passage chunking, additive; new `chunking.py`, store.py, search.py, snippets.py, jobs.py, evaluate.py): section 9 describes the shipped behaviour. Deviations from earlier sections: `health()["chunks"]`, `["vectors"]` and `["missing_vectors"]` now count **notes** (they equalled row counts while every note had one chunk), so `documents == chunks == fts` stays the consistency check; row counts moved to new keys (`passages`, `passage_vectors`, `missing_passage_vectors`). `health()["long_documents"]` counts notes with a passage longer than the model window (it used the whole body). `repair()["vectors_missing"]` counts notes. `vectors_for_docs()` returns a `[n_passages, dim]` matrix for multi-passage notes. `Index.open` writes `schema_meta.chunking`/`chunking_built` when it creates a database; `clear()` keeps `chunking`. `_metadata_for_rows` (private) returns `(doc_id, title, source, seq)`. `Hit` gains `passage_seq`/`passage_count`/`passage` (not compared). The "real chunking is still open" note in the 2026-10-14 review-fix entry is resolved. health()'s missing-vector count uses `row_id NOT IN (SELECT rowid FROM vec_float)` instead of a LEFT JOIN on vec0 (~15x faster `stats()` on 5,000 notes). serve.py/static are not changed by this work (see 9.8).
- 2026-10-14 (chunking wired into the app + approved leftovers; serve.py, cli.py, evaluate.py, frontend; additive unless noted): **`/api/stats`** adds `chunking`, `passages` and `vectors_need_rebuild` (the `Index.vectors_need_rebuild()` dict, null without a collection). **Changed:** `long_notes` now means notes longer than one passage (`stats()["long_notes"]`); the previous meaning (`health.long_documents`, text past the model window) moved to `notes_over_model_window`. Nothing read the old key. With vectors and `needed`, `status_detail` says "Vectors need a rebuild for N notes"; a running rebuild job makes `status` `"indexing"` with "Rebuilding vectors… N%". **`POST /api/maintenance/rebuild-vectors {"chunking": "keep"|"default"|"off"}`** (CSRF) → 202 job without `db` (`kind: "rebuild_vectors"`, label = collection name); calls `ensure_not_busy` first (pre-empts auto-sync), 409 `collection_busy` while any job runs, 503 `no_index` without an open collection, 422 `validation_error` for another value. **Search results** add `passage`: `{"seq", "count", "text"}` for meaning matches in multi-passage notes, else null. **`GET /api/roots`** → `{"roots": [{root_id, path, exclude, documents, added_at, last_indexed_at, files_only, network}]}` (no disk access). **`POST /api/index`** accepts `collection`; when it is set and differs (case-insensitively) from the open collection → 409 `collection_changed` and no job starts. **`GET /api/fs/list`** marks `.jsonl` entries `supported: true`; `supported_files` still counts `SUPPORTED_SUFFIXES` only. **Frontend:** Manage → Health shows `#passage-callout` (`data-kind="improve"`: "Improve meaning search for long notes — N notes are longer than one passage", button *Split long notes* → `chunking: "default"`, only when the collection has vectors; `data-kind="rebuild"`: *Rebuild vectors*, also in Maintenance) and a *Long notes* row; rebuild jobs reuse the index dialog with their own step labels (Find long notes / Split long notes / Build vectors / Ready), *Cancel rebuild*, and done/failed copy; the header pill reads "Rebuilding N%". The preview marks the passage of a result with `passage` (`.preview-paragraph.is-passage` + `#preview-passage-label` "Closest passage · i of n") and jumps to its first highlight or label, never to a highlight elsewhere. The wizard loads `GET /api/roots` and pre-fills an indexed folder's saved patterns without counting them as an edit (`#index-options-summary` "Options · N saved exclusions"); `wizardIndexBody({collection})` and queued folder updates send `collection`. `isPickableFile` relies on the server's `supported` flag. **CLI:** `rebuild-vectors [--db|--collection] [--chunking keep|default|off] [-q]` (loads the collection's model before changing the setting; Ctrl+C as `index`, exit 130); `folders list [--json] | remove FOLDER [--keep-documents|--delete-documents] | relocate OLD NEW` (FOLDER/OLD = path or `[id]`; unknown folder or missing NEW → exit 4); `status` prints `passages` (+ JSON `chunking`, `long_notes`, `vectors_need_rebuild`) and `status`/`doctor` print a rebuild needed/recommended hint (not counted as damage). **`evaluate.run_smoke(..., *, lexical_match="auto")`** ("all" for semantic cases; result adds `lexical_match`) and **changed:** a no-answer case passes when no hits or only weak ones come back (`SearchOutcome.abstained`), because "auto" tops a lone common word up with weak partial hits; `onefind smoke --match` defaults to `auto`. Re-baseline: sample-data 1.000/1.000/1.000, showcase-data Hit@3 0.984, MRR@5 0.967, no-answer 1.000 (same with `all`).
