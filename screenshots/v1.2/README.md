# OneFind v1.2 screenshots

Captured from the live app (real `onefind serve` + Chrome) by
`tools/ui_check.py --release screenshots/v1.2`. Each state appears four times:
`light` / `dark` theme at `1280` (1280x860) and `375` (375x780) px wide, named
`NN-state-THEME-WIDTH.png`.

| # | State | Caption |
|---|-------|---------|
| 01 | `01-onboarding-*` | An empty collection: the welcome card says what to do ("Add a folder to …"); search modes and filters stay hidden until there is something to search. |
| 02 | `02-index-folder-*` | Index wizard, step 1: Places, a scrolling folder list, breadcrumbs scrolled to the current folder, and the "Build vectors" choice in view. |
| 03 | `03-index-progress-*` | Indexing progress: a stepper that updates in place, countable progress, and "Saving and building vectors…" instead of a stuck 100% bar. |
| 04 | `04-ready-*` | Ready home screen: collection status, recent searches (one row per query) and recent documents; empty cards are hidden. |
| 05 | `05-loading-*` | A new search in progress: the staged pipeline (understanding query → keyword → meaning → combining → preparing) above card-shaped placeholders. |
| 06 | `06-results-*` | Results: summary ("1 result · 9 weaker · Hybrid · 34 ms ▾" opens per-step timings), compact cards with metadata left and Open / Preview / Explain / More right. |
| 07 | `07-explain-*` | "Explain" opens "Why this result" inline: reasons, matched words, how each search leg ranked it. |
| 08 | `08-preview-*` | Preview: a side pane at 1280 (the previewed card is highlighted), a full-height drawer on phones with metadata on one compact line. |
| 09 | `09-filters-*` | Filters opened from the Filters button, one folder filter active as a removable chip; the summary counts the filter. |
| 10 | `10-weak-*` | No strong matches: an honest, neutral notice, a summary that agrees ("No strong matches · 10 weaker"), one show/hide control, no save/export. |
| 11 | `11-no-results-*` | No results in Keyword mode: plain explanation and next steps (Search by meaning, Try Hybrid, Edit search). |
| 12 | `12-error-*` | The local service stopped: "OneFind is not responding", how to restart it, the query kept, Try again; controls that cannot work are hidden. |
| 13 | `13-manage-*` | Manage panel: the current section tab is marked, health and maintenance with labelled icons, "Vectors complete" as a status line. |
| 14 | `14-collections-menu-*` | Collection switcher: collections with document counts; New / Open existing file / Manage stay reachable at the bottom. |
| 15 | `15-shortcuts-*` | Keyboard shortcuts dialog, noting that single-key shortcuts can be turned off in Preferences. |
