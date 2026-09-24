# Duplicate Titles — Edge Cases Record EDGE-009

- **Record ID:** `EDGE-009`
- **Category:** Edge Cases
- **Owner:** Quality Engineer
- **Status:** Approved
- **Version:** `1.0.5`
- **Last reviewed:** `2026-09-17`

## Summary

This edge-case fixture defines the approved duplicate titles practice for Quality Engineer. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record EDGE-009 is synthetic demo data created for OneFind and is not a real company policy.

Two unrelated records may share a human title; stable record IDs must disambiguate them.

## Control facts

| Field | Value |
|---|---|
| Record owner | Quality Engineer |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 94 test assertions |
| Related records | `EDGE-010`, `EDGE-001` |

## Procedure

1. Confirm the request against record ID `EDGE-009` and the current version `1.0.5`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 94 test assertions.
4. Link `EDGE-010` and `EDGE-001` when the decision crosses team boundaries.

## Structured configuration

```text
Human titles may collide; stable record IDs must disambiguate them.
```

## Decision notes

- Prefer a reproducible local command and committed evidence over an undocumented manual step.
- If evidence is missing, mark the answer as unknown and link the responsible owner.
- Reuse this record's terminology consistently in slides, documentation, and demonstrations.

## Presentation prompt

Ask: “What does `EDGE-009` require for duplicate titles?” The answer should identify the owner, version, threshold, evidence rule, and related records.
