# Safe Reset — Presentation Record DEMO-008

- **Record ID:** `DEMO-008`
- **Category:** Presentation
- **Owner:** Presenter
- **Status:** Approved
- **Version:** `1.3.12`
- **Last reviewed:** `2026-09-15`

## Summary

This presentation cue defines the approved safe reset practice for Presenter. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record DEMO-008 is synthetic demo data created for OneFind and is not a real company policy.



## Control facts

| Field | Value |
|---|---|
| Record owner | Presenter |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 73 demo steps |
| Related records | `DEMO-009`, `DEMO-010` |

## Procedure

1. Confirm the request against record ID `DEMO-008` and the current version `1.3.12`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 73 demo steps.
4. Link `DEMO-009` and `DEMO-010` when the decision crosses team boundaries.

## Structured configuration

```markdown
- **Safe Reset**
  - Evidence: `benchmarks/reports/`
  - Owner: presentation lead
```

## Decision notes

- Prefer a reproducible local command and committed evidence over an undocumented manual step.
- If evidence is missing, mark the answer as unknown and link the responsible owner.
- Reuse this record's terminology consistently in slides, documentation, and demonstrations.

## Presentation prompt

Ask: “What does `DEMO-008` require for safe reset?” The answer should identify the owner, version, threshold, evidence rule, and related records.
