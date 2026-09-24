# Release Engineering — Engineering Record ENG-009

- **Record ID:** `ENG-009`
- **Category:** Engineering
- **Owner:** Engineering Lead
- **Status:** Approved
- **Version:** `1.0.5`
- **Last reviewed:** `2026-09-17`

## Summary

This architecture specification defines the approved release engineering practice for Engineering Lead. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record ENG-009 is synthetic demo data created for OneFind and is not a real company policy.



## Control facts

| Field | Value |
|---|---|
| Record owner | Engineering Lead |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 94 p95 latency |
| Related records | `ENG-010`, `ENG-001` |

## Procedure

1. Confirm the request against record ID `ENG-009` and the current version `1.0.5`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 94 p95 latency.
4. Link `ENG-010` and `ENG-001` when the decision crosses team boundaries.

## Structured configuration

```sql
SELECT c.doc_id, -bm25(fts) AS score
FROM fts
WHERE fts MATCH ?
ORDER BY score DESC, c.doc_id ASC
LIMIT 13;
```

## Decision notes

- Prefer a reproducible local command and committed evidence over an undocumented manual step.
- If evidence is missing, mark the answer as unknown and link the responsible owner.
- Reuse this record's terminology consistently in slides, documentation, and demonstrations.

## Presentation prompt

Ask: “What does `ENG-009` require for release engineering?” The answer should identify the owner, version, threshold, evidence rule, and related records.
