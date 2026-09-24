# OneFind browser evidence

This folder contains a comprehensive Playwright run against the real packaged OneFind UI. It uses the installed Chrome browser and local services only—no paid API, cloud service, or downloaded browser binary.

Run it again from the repository root:

```bash
pip install -e ".[browser]"
python tools/browser_check.py --output sshot
```

## Screenshot index

| # | Screenshot | Use case |
|---:|---|---|
| 01 | `01-indexed-idle.png` | Indexed service, ready state, all retrieval capabilities enabled |
| 02 | `02-first-semantic-loading.png` | First semantic query while the CPU model loads |
| 03 | `03-semantic-float-results.png` | Semantic float results |
| 04 | `04-keyword-results.png` | Keyword/BM25 results |
| 05 | `05-semantic-int8-results.png` | Semantic int8 results |
| 06 | `06-semantic-binary-results.png` | Semantic binary results |
| 07 | `07-hybrid-float-results.png` | Hybrid float RRF results |
| 08 | `08-hybrid-int8-results.png` | Hybrid int8 RRF results |
| 09 | `09-hybrid-binary-results.png` | Hybrid binary RRF results |
| 10 | `10-no-results.png` | Empty/no-result recovery state |
| 11 | `11-results-k5.png` | Five-result layout |
| 12 | `12-results-k30.png` | Thirty-result layout |
| 13 | `13-shareable-url-state.png` | Query/mode/precision/k restored from URL |
| 14 | `14-dark-hybrid-results.png` | Dark color scheme |
| 15 | `15-mobile-hybrid-results.png` | 430×932 touch/mobile layout |
| 16 | `16-lexical-only-capabilities.png` | Semantic/hybrid controls disabled without vectors |
| 17 | `17-lexical-only-results.png` | Valid lexical search on a lexical-only index |
| 18 | `18-no-index.png` | No-index state with disabled search |
| 19 | `19-network-error.png` | Browser offline/network failure state |
| 20 | `20-validation-error.png` | API validation error for an overlong query |
| 21 | `21-xss-rendered-as-text.png` | Untrusted HTML displayed inertly as text |

`manifest.json` records the timestamp, browser executable, viewport/base URLs, file sizes, SHA-256 hashes, assertions, CSP headers, and console results. The only recorded console messages are the deliberately induced offline and 422 errors; unexpected browser errors fail the run.
