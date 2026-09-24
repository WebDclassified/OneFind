# Demo screenshots

The v1.1 interface is packaged at `src/onefind/static/` and served by:

```bash
onefind index ./sample-data --db demo.db --embed
onefind serve --db demo.db --port 8080
```

Open <http://127.0.0.1:8080/>.

Recommended current captures:

| Filename | State |
|---|---|
| `ui-01-index-ready.png` | Header stats and idle search workspace |
| `ui-02-keyword-results.png` | Keyword mode with highlighted local results |
| `ui-03-semantic-results.png` | Semantic paraphrase and neutral excerpt |
| `ui-04-hybrid-results.png` | Hybrid mode with precision and result metrics |
| `ui-05-no-results.png` | Empty state with mode-specific recovery advice |
| `ui-06-dark-responsive.png` | Dark mode or narrow responsive layout |

Capture at approximately 1440–1920 px wide. Keep the browser chrome, address bar, and operating-system taskbar out of the image when possible. The UI uses no external assets, so screenshots remain deterministic apart from local latency and model-loading text.

The current v1.1 captures are stored as `ui-01-index-ready.png` through `ui-06-mobile-results.png`. The staged `1.png` through `6.png` files came from the earlier prototype and are retained only because they were already staged; they should be removed before committing the v1.1 evidence.
