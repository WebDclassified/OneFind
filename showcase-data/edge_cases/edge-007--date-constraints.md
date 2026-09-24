# Date Constraints — Edge Cases Record EDGE-007

- **Record ID:** `EDGE-007`
- **Category:** Edge Cases
- **Owner:** Quality Engineer
- **Status:** Approved
- **Version:** `1.2.19`
- **Last reviewed:** `2026-09-13`

## Summary

This edge-case fixture defines the approved date constraints practice for Quality Engineer. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record EDGE-007 is synthetic demo data created for OneFind and is not a real company policy.

Effective window: 2026-01-15 through 2026-12-31; review dates use ISO 8601.

## Control facts

| Field | Value |
|---|---|
| Record owner | Quality Engineer |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 78 test assertions |
| Related records | `EDGE-008`, `EDGE-009` |

## Procedure

1. Confirm the request against record ID `EDGE-007` and the current version `1.2.19`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 78 test assertions.
4. Link `EDGE-008` and `EDGE-009` when the decision crosses team boundaries.

## Structured configuration

```text
Effective window: 2026-01-15 through 2026-12-31 (ISO 8601).
```

## Decision notes

- Prefer a reproducible local command and committed evidence over an undocumented manual step.
- If evidence is missing, mark the answer as unknown and link the responsible owner.
- Reuse this record's terminology consistently in slides, documentation, and demonstrations.

## Presentation prompt

Ask: “What does `EDGE-007` require for date constraints?” The answer should identify the owner, version, threshold, evidence rule, and related records.
