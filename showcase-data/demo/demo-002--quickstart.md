# Quickstart — Presentation Record DEMO-002

- **Record ID:** `DEMO-002`
- **Category:** Presentation
- **Owner:** Presenter
- **Status:** Approved
- **Version:** `1.1.14`
- **Last reviewed:** `2026-09-03`

## Summary

This presentation cue defines the approved quickstart practice for Presenter. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record DEMO-002 is synthetic demo data created for OneFind and is not a real company policy.



## Control facts

| Field | Value |
|---|---|
| Record owner | Presenter |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 77 demo steps |
| Related records | `DEMO-003`, `DEMO-004` |

## Procedure

1. Confirm the request against record ID `DEMO-002` and the current version `1.1.14`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 77 demo steps.
4. Link `DEMO-003` and `DEMO-004` when the decision crosses team boundaries.

## Structured configuration

```markdown
- **Quickstart**
  - Evidence: `benchmarks/reports/`
  - Owner: presentation lead
```

## Decision notes

- Prefer a reproducible local command and committed evidence over an undocumented manual step.
- If evidence is missing, mark the answer as unknown and link the responsible owner.
- Reuse this record's terminology consistently in slides, documentation, and demonstrations.

## Presentation prompt

Ask: “What does `DEMO-002` require for quickstart?” The answer should identify the owner, version, threshold, evidence rule, and related records.
