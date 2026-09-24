# Multilingual Terms — Edge Cases Record EDGE-002

- **Record ID:** `EDGE-002`
- **Category:** Edge Cases
- **Owner:** Quality Engineer
- **Status:** Approved
- **Version:** `1.1.14`
- **Last reviewed:** `2026-09-03`

## Summary

This edge-case fixture defines the approved multilingual terms practice for Quality Engineer. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record EDGE-002 is synthetic demo data created for OneFind and is not a real company policy.

Terms appear in English, español, français, Deutsch, 日本語, and Ελληνικά.

## Control facts

| Field | Value |
|---|---|
| Record owner | Quality Engineer |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 77 test assertions |
| Related records | `EDGE-003`, `EDGE-004` |

## Procedure

1. Confirm the request against record ID `EDGE-002` and the current version `1.1.14`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 77 test assertions.
4. Link `EDGE-003` and `EDGE-004` when the decision crosses team boundaries.

## Structured configuration

```text
Unicode: café naïve façade — 日本語 — Ελληνικά — 🚀
```

## Decision notes

- Prefer a reproducible local command and committed evidence over an undocumented manual step.
- If evidence is missing, mark the answer as unknown and link the responsible owner.
- Reuse this record's terminology consistently in slides, documentation, and demonstrations.

## Presentation prompt

Ask: “What does `EDGE-002` require for multilingual terms?” The answer should identify the owner, version, threshold, evidence rule, and related records.
