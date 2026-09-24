# Curated Queries — Presentation Record DEMO-003

- **Record ID:** `DEMO-003`
- **Category:** Presentation
- **Owner:** Presenter
- **Status:** Approved
- **Version:** `1.2.7`
- **Last reviewed:** `2026-09-05`

## Summary

This presentation cue defines the approved curated queries practice for Presenter. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record DEMO-003 is synthetic demo data created for OneFind and is not a real company policy.



## Control facts

| Field | Value |
|---|---|
| Record owner | Presenter |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 72 demo steps |
| Related records | `DEMO-004`, `DEMO-005` |

## Procedure

1. Confirm the request against record ID `DEMO-003` and the current version `1.2.7`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 72 demo steps.
4. Link `DEMO-004` and `DEMO-005` when the decision crosses team boundaries.

## Structured configuration

```markdown
- **Curated Queries**
  - Evidence: `benchmarks/reports/`
  - Owner: presentation lead
```

## Decision notes

- Prefer a reproducible local command and committed evidence over an undocumented manual step.
- If evidence is missing, mark the answer as unknown and link the responsible owner.
- Reuse this record's terminology consistently in slides, documentation, and demonstrations.

## Presentation prompt

Ask: “What does `DEMO-003` require for curated queries?” The answer should identify the owner, version, threshold, evidence rule, and related records.
