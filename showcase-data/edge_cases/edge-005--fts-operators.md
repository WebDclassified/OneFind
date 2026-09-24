# FTS Operators — Edge Cases Record EDGE-005

- **Record ID:** `EDGE-005`
- **Category:** Edge Cases
- **Owner:** Quality Engineer
- **Status:** Approved
- **Version:** `1.0.13`
- **Last reviewed:** `2026-09-09`

## Summary

This edge-case fixture defines the approved fts operators practice for Quality Engineer. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record EDGE-005 is synthetic demo data created for OneFind and is not a real company policy.

FTS syntax words AND OR NOT NEAR appear literally and must remain safe user input.

## Control facts

| Field | Value |
|---|---|
| Record owner | Quality Engineer |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 88 test assertions |
| Related records | `EDGE-006`, `EDGE-007` |

## Procedure

1. Confirm the request against record ID `EDGE-005` and the current version `1.0.13`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 88 test assertions.
4. Link `EDGE-006` and `EDGE-007` when the decision crosses team boundaries.

## Structured configuration

```text
FTS syntax words AND OR NOT NEAR appear literally and must remain safe user input.
```

## Decision notes

- Prefer a reproducible local command and committed evidence over an undocumented manual step.
- If evidence is missing, mark the answer as unknown and link the responsible owner.
- Reuse this record's terminology consistently in slides, documentation, and demonstrations.

## Presentation prompt

Ask: “What does `EDGE-005` require for fts operators?” The answer should identify the owner, version, threshold, evidence rule, and related records.
