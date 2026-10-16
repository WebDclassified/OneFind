# Changelog

All notable changes to OneFind are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [1.2.1] - 2026-10-14

### Added

- **Auto-sync** (`watcher.py`, no new dependency): OneFind can keep a collection's index up to
  date while you edit your notes. A gentle stat-only poll (default every 5 s, slower for large
  trees, at most about 2.5% of one core) honours exclude patterns. Changes are debounced until a
  folder is stable for two polls, confirmed with `scan_changes`, and applied by a normal
  incremental, pruning background job labelled "Auto-sync". It vectorizes only when the collection
  already has vectors. Network folders are never polled, and missing, unreadable or suddenly empty
  folders keep their notes. Errors are retried with backoff, and a manual job always takes
  priority (it pre-empts a running auto-sync job).
- The on/off setting is stored in the collection file (`schema_meta.auto_sync`). Existing
  collections default to off; collections created in the index wizard default to on.
- **API:** `GET/POST /api/autosync`, an `auto_sync` status in `GET /api/stats`, and an optional
  `auto_sync` in `POST /api/index`. `create_app(autosync=, autosync_interval=)`.
- **UI:** the status pill shows "Ready · Auto-sync on" or "Syncing N changes…", Manage → Health
  has an Auto-sync switch with the last-synced time, and the index wizard has a "Keep this
  collection up to date automatically" box. Finished syncs refresh results quietly.
- **CLI:** `onefind watch [--interval S] [--duration S] [--allow-network]` runs auto-sync in the
  foreground until Ctrl+C. `onefind watch --set on|off` saves the setting. `onefind serve` honours
  it (`--no-auto-sync` disables it), and `onefind status` shows it.
- `tools/ui_check.py` has an auto-sync step: enable it in Manage, write a note, and wait for it to
  become searchable.
- **Passage chunking** (`chunking.py`, no new dependency): meaning search now covers long notes.
  The model reads only about 200 words per text, so a note longer than one passage (200 words,
  40 words of overlap, cut at sentence/line boundaries) is split into passages, each with its own
  vector of `title + passage`, and a note ranks by its best passage. Keyword search is unchanged.
  New collections have it on; existing collections keep one vector per note until
  `Index.set_chunking("default")` + `Index.rebuild_vectors()` (or the new
  `JobManager.start_rebuild_vectors` background job) re-split them, batch by batch and
  cancellable. `stats()` reports `chunking`, `passages`, `long_notes` and `vectors_need_rebuild`;
  explanations add `best_passage_seq` ("Closest passage is further down the note"), and snippets
  of meaning matches show the matching passage instead of the note's first lines. On the
  showcase corpus hybrid hit@1/MRR rose from 0.948/0.968 to 0.964/0.977 (hit@3 unchanged at
  0.990); sample-data is unchanged. BEIR evaluation always uses whole documents.
- **Rebuild vectors in the app:** `POST /api/maintenance/rebuild-vectors {"chunking":
  "keep"|"default"|"off"}` starts a background job (`kind: "rebuild_vectors"`, CSRF header,
  409 `collection_busy` while another job runs, pre-empts auto-sync like other manual work).
  Manage → Health shows a gentle "Improve meaning search for long notes" offer for collections
  made before 1.2.1 ("Split long notes" turns passage search on) and a "Rebuild vectors" action
  when stored vectors follow an older passage setting (also in Maintenance). The job runs in the
  usual progress dialog with its own steps (Find long notes → Split long notes → Build vectors →
  Ready) and can be cancelled; finished notes keep their new passages.
- **Passage-aware results:** search results carry `passage` (`{seq, count, text}`) for meaning
  matches in long notes. The preview marks the matched passage ("Closest passage · 5 of 5"),
  scrolls to it, and no longer jumps to an unrelated highlight elsewhere in the note; "Why this
  result" says when the closest passage is further down the note.
- **`/api/stats`** passes through `chunking`, `passages` and `vectors_need_rebuild`; the status
  sentence says when vectors need a rebuild, and the pill reads "Rebuilding 40%" during one.
- **CLI:** `onefind rebuild-vectors [--db|--collection] [--chunking keep|default|off] [-q]` with
  live progress and the same Ctrl+C semantics as `index` (first Ctrl+C stops between batches,
  exit 130; a rerun continues). `onefind status` prints a `passages` line and `status`/`doctor`
  say when a rebuild is needed or recommended, with the exact command (`status --json` adds
  `chunking`, `long_notes`, `vectors_need_rebuild`).
- **CLI folder management:** `onefind folders list [--json]` (ids, note counts, saved
  exclusions, missing folders), `onefind folders remove FOLDER [--keep-documents |
  --delete-documents]` and `onefind folders relocate OLD NEW` (notes keep their ids), mirroring
  `POST /api/roots/remove` and `/api/roots/relocate`. FOLDER is a path or the `[id]` from `list`.
- `GET /api/roots`: the collection's folders with their saved exclude patterns (no disk access).
  The index wizard pre-fills an already indexed folder's saved patterns ("Options · 1 saved
  exclusion"); leaving them untouched keeps them, editing replaces them.
- `POST /api/index` accepts an optional `collection`; when it is not the open collection the
  request is refused with 409 `collection_changed`. The wizard and the queued "Update index" of
  several folders send it, so a queued update never lands in a collection switched to since.
- `tools/ui_check.py` covers the long-notes offer, both rebuild flows, the passage preview and the
  wizard's saved-exclusion pre-fill.

### Changed

- `/api/stats` `long_notes` now means **notes longer than one passage** (`Index.stats()`); the
  old meaning (notes with text past the model's window, `health.long_documents`) moved to
  `notes_over_model_window`. Nothing in the UI read the old key.
- `GET /api/fs/list` marks `.jsonl` datasets `supported` (they can be indexed on their own);
  `supported_files` still counts only the notes that indexing the folder reads. The wizard's
  file-name workaround is gone.
- `onefind smoke` and `evaluate.run_smoke(lexical_match="auto")` use the app's keyword matching
  by default (`--match all|any|auto`; semantic cases always use all). A no-answer case passes
  when nothing strong is returned (no hits, or only weak ones, which the app shows as "No strong
  matches"). Re-baselined: sample-data 1.000 Hit@3, 1.000 MRR@5, 1.000 no-answer; showcase-data
  0.984 Hit@3, 0.967 MRR@5, 1.000 no-answer (identical with `--match all`).

## [1.2.0] - 2026-10-14

This release turns the hybrid-search reproduction into a **private, local notes search app**. The
product spec is [`docs/10-product-vision-and-feature-spec.md`](docs/10-product-vision-and-feature-spec.md),
the binding interfaces are in [`docs/11-implementation-contract.md`](docs/11-implementation-contract.md),
and the new [`docs/12-user-guide.md`](docs/12-user-guide.md) is for end users. Ranking, the BEIR
results and the evaluation harness are unchanged.

### Added

- **Collections library** (`library.py`): named collections, each one portable `.db` file,
  registered in a per-user library (`%APPDATA%\OneFind`, `~/Library/Application Support/OneFind`
  or `~/.local/share/onefind`, overridable with `ONEFIND_HOME`). You can create, open an existing
  file, switch, rename, remove (keeping the file) and delete (with typed confirmation).
- **Library mode for `onefind serve`**: `--db` is optional, and a first run shows a guided
  onboarding screen in the browser. New `--home` and `--no-open` options.
- **Index wizard in the browser**: a folder browser with Places, breadcrumbs, a typed path,
  single-file picking and exclude patterns. Indexing runs as a **background job** (`jobs.py`) with
  live, countable progress (Scan files → Index text → Build vectors → Ready). It can be cancelled
  and keeps running when the window is closed. A shared, thread-safe model cache serves both
  indexing and search.
- **Incremental indexing**: content hashes skip unchanged files. `--prune` removes notes whose
  files were deleted or are now excluded, and `--full` rewrites everything. Exclude globs are
  remembered per folder. Skipped files (too large / binary / empty / unreadable) are reported.
  Each indexing run is recorded, and a change scan reports new, changed and deleted files per
  folder.
- **Front matter**: `title:` and `tags:` are read from a leading `---` block.
- **Filters** by folder, file type, modified date and tag, in the API, CLI and UI. Filtered
  semantic search uses an exact scan of the allowed rows and never post-filters a top-k.
- **"Why this result"**: per-hit explanations with matched and missing words, keyword and meaning
  ranks and scores, the fused score and readable reasons.
- **Match confidence and honest abstention**: Strong, Good and Weak labels calibrated with the real
  MiniLM model, a "Weaker matches" group, and a "No strong matches" notice instead of forcing a
  result.
- **Relaxed keyword matching (`lexical_match="auto"`)**: notes with all the words come first,
  topped up with partial matches when there are too few, and the UI says "No note contains all of
  your keywords…". Stopwords are ignored for matching, highlighting and confidence.
- **Better snippets** (`snippets.py`): a sentence- and paragraph-aware best passage, with
  whole-word and simple-stem highlighting.
- **Streaming search** (`POST /api/search/stream`, NDJSON) drives a staged progress view
  (understanding query → keyword → meaning → combining → preparing) with per-step timings.
- **Result actions**: open in the default app, show in folder, preview with find-in-document, copy
  snippet, bookmark, filter to this folder, and export results as Markdown or JSON.
- **Personal data per collection**: recent searches, saved searches (which keep mode and filters),
  bookmarks and recent documents, all shown on a new home screen.
- **Health and maintenance**: a plain-language status (`ready` / `empty` / `no_index` /
  `needs_attention` / `indexing`), collection size classes with honest scale notes, integrity
  check, repair (rebuilds FTS, drops orphans, re-embeds missing vectors), compact, full rebuild,
  reindex with vectors, and clear.
- **Backup and restore**: online SQLite backups into the library's `backups/` folder, restore as a
  new collection, or replace the current one with a typed confirmation and an automatic safety
  backup.
- **New CLI commands**: `collections {list,create,use,remove,rename}`, `status`, `backup`
  (`--out`, `--list`, `--overwrite`), `restore BACKUP --as NAME`, `doctor [--repair]`, `compact`
  and `remove DOC_ID…`. `index` gains `--collection`, `--exclude`, `--prune`, `--full`, `-q` and
  live progress. `search` gains `--collection`, `--folder`, `--type`, `--tag`, `--since` and
  `--explain`. A first `Ctrl+C` cancels cleanly (exit code 130).
- **New HTTP API**: collections, filesystem listing, index jobs and change scan, documents
  (get/open/remove), history, saved searches, bookmarks, recent documents, facets, backups and
  maintenance. See contract §6.
- **Redesigned web app**: split into vanilla ES modules, still with no build step, CDN or fonts.
  It adds a collection switcher, status pill, Manage panel (Health, Maintenance, Backups,
  Collections, Preferences, Privacy), light/dark/system themes, Reduce motion,
  `prefers-reduced-motion` support, a consistent motion language, a keyboard shortcuts dialog with
  an option to turn off single-key shortcuts, toasts, URL-restorable searches, and responsive
  layouts down to 320 px.
- **Folder management**: Manage → Health lists folders that have gone missing, with "Locate…"
  (points the folder at its new place; every note keeps its id, `POST /api/roots/relocate`) and
  "Remove from collection" (`POST /api/roots/remove`). "Update index" skips missing folders with a
  warning instead of stopping. "Remove from index…" is available on a result card and in the
  preview.
- **Network folders** are never contacted without asking: change scans skip them, and opening a
  note or indexing a folder on another computer asks for confirmation first.
- `onefind search --match all|any|auto` (default `auto`, the same matching as the app);
  `onefind serve --allowed-host NAME` for remote binds.
- **`tools/ui_check.py`**: a live end-to-end walkthrough (real server and Chrome via Playwright,
  more than 300 checks, five widths, both themes) that also produces the release screenshots in
  `screenshots/v1.2/`.
- **Documentation**: a rewritten README and HOW_TO_RUN, the user guide, the product spec, the
  implementation contract, and this changelog.
- Tests: from 112 to more than 850, adding store v3, ingest v3, library, jobs, snippets, search v3
  and quality, serve v3, CLI v3, integration and static-UI contract suites.

### Changed

- **Database schema v3.** v2 databases (v1.1) migrate in place, in one transaction, on first open.
  Documents gain hash, mtime, size, type, folder, tags and root columns. New tables store folders,
  history, saved searches, bookmarks, activity and indexing runs. `Index.clear()` is now a full
  reset of those user tables as well.
- **Multi-folder collections**: when two indexed folders contain the same relative path, the
  second file gets a root-qualified id (`~<root_id>/<path>`) instead of overwriting the first.
  Single-folder collections keep their ids.
- Read and maintenance CLI commands fall back to the library's active collection when
  `./onefind.db` does not exist, and never create an empty database. `search` against a missing
  database now exits 4 with a hint. `index` still defaults to `./onefind.db`.
- `onefind serve --db FILE` behaves as in v1.1 and does not write to the per-user library unless
  `--home` or `ONEFIND_HOME` is set.
- `/api/stats` adds status, a status sentence, the collection name, the size class and note, the
  active job, and the missing-vector count. It never exposes the database path.
- Search responses add `explain`, `confidence`, `folder`, `ext`, `modified_at`, `tags`,
  `bookmarked` and `duplicates` per result, and `stages`, `abstained`, `weak_count`, `relaxed`,
  `strict_count` and `filters` in `meta`. All v1.1 fields are kept.
- The API's error envelope now always carries `code`, `message` and `retryable`, plus an optional
  `action` hint (`index`, `reindex`, `embed`, `retry`, `switch_mode`, `repair`, `open_collection`).
  The UI turns these into plain messages with a next-step button.
- `SentenceEmbedder` serializes encoding with an internal lock so one model can be shared safely.
- Snippet logic moved from `serve.py` to `snippets.py`.
- CI runs the new model-free v3 suites.
- Recent searches count only strong results, so a search with only weak matches is listed as
  "0 results".
- Hybrid results that tie exactly are ordered by their meaning rank. In "auto" keyword matching,
  stopwords no longer count against the "contains all your words" pass ("what is wild yeast"
  finds the sourdough note as a full match).
- Binary-precision confidence labels use the stored float vectors of the returned notes, so
  off-topic questions abstain at binary precision too.
- Snippets show readable text instead of raw Markdown (tables, lists, links, bold) and show body
  text, not just the title, for title-only matches.
- Text files that are not UTF-8 are decoded as Windows-1252/Latin-1 instead of showing
  replacement characters.

### Fixed

- Indexing a second folder could silently overwrite notes with the same relative path from the
  first folder (see multi-folder ids above).
- A failed or cancelled index run no longer leaves an ambiguous state. Committed batches stay
  searchable, a new folder with nothing indexable is unregistered again, and the previous
  collection stays usable.
- A single common word no longer prevents off-topic questions from abstaining (relaxed-match
  confidence rules).
- The integrity check refreshes FTS5 state first, so it no longer reports a false "malformed
  inverted index" after another connection (an indexing job) wrote to the collection.
- The semantic read path binds the cached model without backfilling vectors, so searches never
  write to the database.
- Front-end modules and assets are served with `Cache-Control: no-cache`, so an upgrade never
  mixes old and new modules.

- Re-indexing a subfolder or a single file of an indexed folder updates it in place instead of
  registering a second folder, and indexing a parent folder merges the folders it contains.
- Building vectors no longer holds the database write lock while the model runs, and can be
  cancelled between batches; a check during a running job reports "busy" instead of damage.
- Folders that cannot be listed (permissions, a disconnected drive) never cause their notes to be
  pruned. Runs left "running" by a crashed process are marked as interrupted.
- A renamed or moved note keeps its bookmark and open history.
- A cached embedding model loads without any network request.
- Searches no longer fail with "vector index changed" when an indexing job commits at the same
  time.
- `onefind serve --host ::1` works; `onefind backup --out NEW_FOLDER` creates the folder instead of
  writing a file without an extension; CLI commands suggested by `status` and `doctor` name the
  collection; `collections list` leaves no `-wal`/`-shm` files behind.

### Security

- **CSRF guard**: every state-changing endpoint added in v1.2 requires `X-OneFind-Request: 1`
  (otherwise `403 csrf_check_failed`). Search and the legacy reset endpoint keep their v1.1
  behaviour.
- **Opening files** (`/api/documents/open`) works only for loopback requests, only for files that
  resolve inside a registered indexed folder and still exist (`path_outside_root`, `file_missing`),
  and can be disabled with `--no-open` (`open_disabled`).
- Backup and restore paths are validated as real OneFind databases (schema version checked) before
  use. Restore copies through the SQLite backup API and never opens a user-supplied file in place.
  Destructive actions (delete a collection file, replace from backup, clear) require the exact
  collection name.
- `/api/stats` never returns the collection's database path. The folder browser omits hidden
  entries and caps listings at 500.
- The CSP stays strict (`script-src 'self'`, `style-src 'self'`, no inline script or style), with
  no `innerHTML`/`insertAdjacentHTML` in the frontend. API responses send `Cache-Control:
  no-store`, and the OpenAPI schema endpoint is disabled.
- The server stays loopback-only unless `--allow-remote` is given. A remote bind keeps the Host
  check (loopback names, the bind address and `--allowed-host` values), binding to all interfaces
  requires `--allowed-host`, and opening files is turned off for any non-loopback bind.
- Requests a browser labels as cross-site or same-site (`Sec-Fetch-Site`) are refused on every
  `/api/` endpoint, and the folder listing also requires the `X-OneFind-Request` header. The
  folder picker refuses network and device paths. Only supported note types can be opened from
  the app.
- Reset tokens must be 16-256 characters and are compared in constant time.

### Known limitations

- One embedding per note (whole-document, about 256 model tokens). Long notes are represented by
  their opening for meaning search, while keyword search covers the full text. Health reports how
  many notes are longer than the model's window.
- `onefind smoke` still uses all-words keyword matching; the app and `onefind search` use "auto".
- Comfortable up to about 50,000 notes per collection. Filtered, int8 and binary meaning searches
  scan every vector.
- Plain-text formats only (no PDF, Office or OCR). No real-time folder watching.
- int8 and binary keep float-level quality but do not reproduce the paper's quantized speed-up.

## [1.1.0] - 2026-09-24

Corrected reproduction of arXiv:2608.24060. BM25 ordering (strongest first), rerank scores and
fusion were fixed, and the full SciFact and NFCorpus evaluations were re-run (the `2026-08-26`
reports are superseded). It shipped a 20-document sample corpus with 26 gold queries
(`onefind smoke`), the CSP-protected localhost UI with Playwright browser evidence (`sshot/`),
atomic manifest-bound evaluation databases, and 112 tests.
