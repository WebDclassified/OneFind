# Version Ranges — Edge Cases Record EDGE-006

- **Record ID:** `EDGE-006`
- **Category:** Edge Cases
- **Owner:** Quality Engineer
- **Status:** Approved
- **Version:** `1.1.6`
- **Last reviewed:** `2026-09-11`

## Summary

This edge-case fixture defines the approved version ranges practice for Quality Engineer. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record EDGE-006 is synthetic demo data created for OneFind and is not a real company policy.

Accepted versions include 1.0 through 2.9; rejected examples include 3.0-alpha and 0.8-rc1.

## Control facts

| Field | Value |
|---|---|
| Record owner | Quality Engineer |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 83 test assertions |
| Related records | `EDGE-007`, `EDGE-008` |

## Procedure

1. Confirm the request against record ID `EDGE-006` and the current version `1.1.6`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 83 test assertions.
4. Link `EDGE-007` and `EDGE-008` when the decision crosses team boundaries.

## Structured configuration

```text
Accepted: 1.0 through 2.9. Rejected: 3.0-alpha and 0.8-rc1.
```

## Decision notes

- Prefer a reproducible local command and committed evidence over an undocumented manual step.
- If evidence is missing, mark the answer as unknown and link the responsible owner.
- Reuse this record's terminology consistently in slides, documentation, and demonstrations.

## Presentation prompt

Ask: “What does `EDGE-006` require for version ranges?” The answer should identify the owner, version, threshold, evidence rule, and related records.
