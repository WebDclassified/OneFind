# 03 · App Flow & State Map

Project: OneFind reproduction · Version: v0.1 (draft) · Status: Proposed
Scope note: V1 surface is CLI-first (the paper's artifact is a library). The web demo is Phase 6; its states are specified now so UI work can't redefine behavior later.

## Screen/command inventory

### SURFACE: CLI  ROUTE: `OneFind <command>`
| Command | Purpose | Allowed roles | Key states |
|---|---|---|---|
| `OneFind check` | Verify FTS5 + sqlite-vec loadable, print versions | anyone | ok / missing-extension (names fix) |
| `OneFind index <path>` | Ingest corpus into DB | anyone | progress → done summary; empty-folder error; partial-failure rollback note |
| `OneFind search "<q>" --mode m --precision p` | Query an index | anyone | results table / no-results / validation error |
| `OneFind eval --dataset d` | Run harness, write `benchmarks/reports/<name>.md` | anyone | per-config progress bar → report path |
| `OneFind serve --db x.db` | Launch local demo UI | anyone | URL printed, Ctrl-C clean shutdown |

Exit codes: `0` success · `2` usage/validation · `3` environment (missing extension/model) · `4` data error.

## Journeys

**JOURNEY: First successful search**
1. `pip install -e .` → 2. `OneFind check` (env validated) → 3. `index ./sample-data` (progress, doc count echoed) → 4. `search "..." --mode hybrid` → 5. ranked table with ids/scores/snippets.
Recovery path: step 2 fails → message names exact missing wheel; step 4 empty index → instructs to run index first.
Testable success: fresh venv to hybrid result ≤15 min including model download.

**JOURNEY: Reproduce evaluation**
1. `eval --dataset scifact` → 2. dataset cached under `data/` (reuses if present) → 3. configurations run sequentially with live config label → 4. report written + path echoed → 5. numbers pasted into README table by author.
Permission edge case: n/a (local tool). Testable success: rerunning produces byte-identical effectiveness numbers given pinned model/seeds.

**JOURNEY: Demo page (Phase 6)** — SCREEN `/`
Purpose: interactive proof the engine works. Entry conditions: DB loaded, else empty-state card offering `index` instructions.
Actions: submit query; toggle mode lexical/semantic/hybrid; adjust k.
States: **empty-index**, **loading**, **results** (rank list: title, score, highlighted snippet), **no-results**, **error** (engine exception banner), each defined below:

| State | Trigger | Display |
|---|---|---|
| Empty index | 0 docs | CTA card with copy-paste index command |
| Loading | request in flight | disabled input + spinner ≤300 ms then skeleton rows |
| No results | 0 hits | "Nothing matched — try another mode" suggestion row |
| Error | 5xx/exception | red banner, retry button, error id |

Redirects/back: none beyond browser default; destructive confirm required for "reset index" button (Phase 6 optional).
Mobile: single column, toggle wraps. Analytics events: none (local tool).
