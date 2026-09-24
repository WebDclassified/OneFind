# Numeric Limits — Edge Cases Record EDGE-008

- **Record ID:** `EDGE-008`
- **Category:** Edge Cases
- **Owner:** Quality Engineer
- **Status:** Approved
- **Version:** `1.3.12`
- **Last reviewed:** `2026-09-15`

## Summary

This edge-case fixture defines the approved numeric limits practice for Quality Engineer. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record EDGE-008 is synthetic demo data created for OneFind and is not a real company policy.

Numeric constraints: minimum 10, maximum 10,000, step 5, and tolerance 0.25.

## Control facts

| Field | Value |
|---|---|
| Record owner | Quality Engineer |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 73 test assertions |
| Related records | `EDGE-009`, `EDGE-010` |

## Procedure

1. Confirm the request against record ID `EDGE-008` and the current version `1.3.12`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 73 test assertions.
4. Link `EDGE-009` and `EDGE-010` when the decision crosses team boundaries.

## Structured configuration

```text
Minimum=10; maximum=10000; step=5; tolerance=0.25.
```

## Decision notes

- Prefer a reproducible local command and committed evidence over an undocumented manual step.
- If evidence is missing, mark the answer as unknown and link the responsible owner.
- Reuse this record's terminology consistently in slides, documentation, and demonstrations.

## Presentation prompt

Ask: “What does `EDGE-008` require for numeric limits?” The answer should identify the owner, version, threshold, evidence rule, and related records.
