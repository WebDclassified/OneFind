# Demo screenshots / visual evidence

The project ships a localhost web demo at `scrydb serve --db <index>`.

## How to capture screenshots for evaluation submission

1. **Build an index** (one-time, ~3 minutes for SciFact on CPU):
   ```bash
   scrydb eval scifact --db data/scifact.db
   ```
   This also generates the markdown reports in `benchmarks/reports/`.

2. **Start the server**:
   ```bash
   scrydb serve --db data/scifact.db --port 8080
   ```

3. **Open the browser** to <http://127.0.0.1:8080/> and capture the
   following screens. Replace the placeholder filenames below with
   your actual screenshots (PNG or JPEG, 1–2 MB each is plenty):

| Recommended filename | What to capture |
|---|---|
| `01-home-empty-or-indexed.png` | The default state (header with stats, query box, mode toggle) |
| `02-search-lexical-results.png` | Mode = "lexical", a query like `"vitamin C supplementation"`, the highlighted-snippet result list |
| `03-search-semantic-results.png` | Mode = "semantic", a paraphrase query like `"does vitamin C help with colds"`, top-K results |
| `04-search-hybrid-results.png` | Mode = "hybrid" (default), another query |
| `05-search-no-results.png` | A deliberately out-of-corpus query showing the empty state |
| `06-eval-report.png` | Open `benchmarks/reports/eval-scifact-2026-08-26.md` in your editor or as rendered HTML |

4. **Optional**: a short screen recording (1–2 minutes) walking
   through the modes for a faster first impression.

5. **Embed images** in `REPORT.md` like this:
   ```markdown
   ![Lexical results](screenshots/02-search-lexical-results.png)
   ```

## Notes

- The first `scrydb serve` call with a `mode=semantic` or `mode=hybrid`
  query will take 1–2 seconds (MiniLM model load + 5K-doc distance
  compute). Subsequent queries are sub-150 ms p95.
- The UI is single-page vanilla JS — no build step, no node_modules,
  works in any modern browser.
