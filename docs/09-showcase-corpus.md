# Showcase Data and Retrieval Protocol

The `showcase-data/` corpus is original synthetic material for a free, local OneFind presentation. It is not scraped data, private company data, or a claim about real policy.

## Scale and coverage

- 120 Markdown records.
- 12 domains with 10 records each.
- 202 gold queries.
- 120 combined JSONL documents.
- SHA-256 manifest for every Markdown record.

| Domain | Purpose |
|---|---|
| Product | Product decisions, feedback, metrics, onboarding |
| Engineering | Architecture, schema, ranking, fusion, testing |
| Operations | Deployment, rollback, monitoring, backup, recovery |
| Data | Corpus, privacy, retention, manifests, evaluation |
| Security | Threat model, CSP, reset, remote binding, XSS |
| Support | Troubleshooting, model, FTS, UI, API, escalation |
| Finance | Budget, local compute cost, licensing, ROI |
| Research | Reproduction, MiniLM, BEIR, quantization, limits |
| Presentation | Live-demo cues and jury questions |
| Governance | Ownership, ADRs, review, risk, approval |
| Team | Roles, handoffs, training, collaboration |
| Edge Cases | Unicode, long lines, operators, versions, dates, HTML |

## Complexity intentionally included

- Nested directories and canonical relative IDs.
- Markdown headings, tables, lists, and cross-references.
- SQL, JSON, HTTP, shell, Markdown, and plain-text code blocks.
- Stable record IDs, semantic versions, ISO dates, and numeric thresholds.
- Long unbroken text for wrapping and layout testing.
- Unicode and multilingual terms.
- Literal FTS operators that must be safe user input.
- Similar titles disambiguated by record ID.
- HTML payloads that must render as inert text.
- Lexical, semantic, hybrid, multi-relevant, and no-answer gold queries.

## Gold query classes

| Class | Coverage |
|---|---|
| Lexical identity | Exact record IDs and structured terms |
| Semantic paraphrase | Meaning-focused questions with ownership/evidence language |
| Hybrid complex | ID plus semantic context |
| Multi-relevant | Category-level questions with several relevant records |
| No-answer | Deliberately unmatched lexical tokens |

## Quality gate

```bash
onefind index ./showcase-data --db data/showcase.db --embed
onefind smoke --db data/showcase.db --queries showcase-data/queries.jsonl --k 5
```

The model-backed test requires at least 0.90 Hit@3, 0.90 MRR@5, and 1.00 no-answer accuracy. The actual result must be reported, not assumed.

## Regeneration

The corpus is deterministic and can be rebuilt with:

```bash
python tools/build_showcase_corpus.py --output showcase-data --clean
```

After regeneration:

1. Run `tests/test_showcase_corpus.py`.
2. Run `tests/test_showcase_smoke.py`.
3. Rebuild `data/showcase.db`.
4. Update this reference if query/document counts or feature coverage changes.
5. Re-run the browser suite if the UI is changed.
