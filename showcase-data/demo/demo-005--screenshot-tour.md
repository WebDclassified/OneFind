# Screenshot Tour — Presentation Record DEMO-005

- **Record ID:** `DEMO-005`
- **Category:** Presentation
- **Owner:** Presenter
- **Status:** Approved
- **Version:** `1.0.13`
- **Last reviewed:** `2026-09-09`

## Summary

This presentation cue defines the approved screenshot tour practice for Presenter. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record DEMO-005 is synthetic demo data created for OneFind and is not a real company policy.



## Control facts

| Field | Value |
|---|---|
| Record owner | Presenter |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 88 demo steps |
| Related records | `DEMO-006`, `DEMO-007` |

## Procedure

1. Confirm the request against record ID `DEMO-005` and the current version `1.0.13`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 88 demo steps.
4. Link `DEMO-006` and `DEMO-007` when the decision crosses team boundaries.

## Structured configuration

```markdown
- **Screenshot Tour**
  - Evidence: `benchmarks/reports/`
  - Owner: presentation lead
```

## Decision notes

- Prefer a reproducible local command and committed evidence over an undocumented manual step.
- If evidence is missing, mark the answer as unknown and link the responsible owner.
- Reuse this record's terminology consistently in slides, documentation, and demonstrations.

## Presentation prompt

Ask: “What does `DEMO-005` require for screenshot tour?” The answer should identify the owner, version, threshold, evidence rule, and related records.
