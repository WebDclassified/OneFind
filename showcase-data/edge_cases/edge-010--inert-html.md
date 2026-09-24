# Inert HTML — Edge Cases Record EDGE-010

- **Record ID:** `EDGE-010`
- **Category:** Edge Cases
- **Owner:** Quality Engineer
- **Status:** Approved
- **Version:** `1.1.18`
- **Last reviewed:** `2026-09-19`

## Summary

This edge-case fixture defines the approved inert html practice for Quality Engineer. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record EDGE-010 is synthetic demo data created for OneFind and is not a real company policy.

Literal text <img src=x onerror="alert(1)"> and <script>alert(2)</script> must render inert.

## Control facts

| Field | Value |
|---|---|
| Record owner | Quality Engineer |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 89 test assertions |
| Related records | `EDGE-001`, `EDGE-002` |

## Procedure

1. Confirm the request against record ID `EDGE-010` and the current version `1.1.18`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 89 test assertions.
4. Link `EDGE-001` and `EDGE-002` when the decision crosses team boundaries.

## Structured configuration

```text
Literal <img src=x onerror="alert(1)"> and <script>alert(2)</script> text must remain inert.
```

## Decision notes

- Prefer a reproducible local command and committed evidence over an undocumented manual step.
- If evidence is missing, mark the answer as unknown and link the responsible owner.
- Reuse this record's terminology consistently in slides, documentation, and demonstrations.

## Presentation prompt

Ask: “What does `EDGE-010` require for inert html?” The answer should identify the owner, version, threshold, evidence rule, and related records.
