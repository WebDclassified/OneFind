# JSON Structure — Edge Cases Record EDGE-004

- **Record ID:** `EDGE-004`
- **Category:** Edge Cases
- **Owner:** Quality Engineer
- **Status:** Approved
- **Version:** `1.3.20`
- **Last reviewed:** `2026-09-07`

## Summary

This edge-case fixture defines the approved json structure practice for Quality Engineer. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record EDGE-004 is synthetic demo data created for OneFind and is not a real company policy.

Literal JSON braces, arrays, booleans, null values, and nested metadata are part of the retrieval challenge.

## Control facts

| Field | Value |
|---|---|
| Record owner | Quality Engineer |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 93 test assertions |
| Related records | `EDGE-005`, `EDGE-006` |

## Procedure

1. Confirm the request against record ID `EDGE-004` and the current version `1.3.20`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 93 test assertions.
4. Link `EDGE-005` and `EDGE-006` when the decision crosses team boundaries.

## Structured configuration

```text
Unicode: café naïve façade — 日本語 — Ελληνικά — 🚀
```

## Decision notes

- Prefer a reproducible local command and committed evidence over an undocumented manual step.
- If evidence is missing, mark the answer as unknown and link the responsible owner.
- Reuse this record's terminology consistently in slides, documentation, and demonstrations.

## Presentation prompt

Ask: “What does `EDGE-004` require for json structure?” The answer should identify the owner, version, threshold, evidence rule, and related records.
