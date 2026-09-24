# Testing Strategy — Engineering Record ENG-008

- **Record ID:** `ENG-008`
- **Category:** Engineering
- **Owner:** Engineering Lead
- **Status:** Approved
- **Version:** `1.3.12`
- **Last reviewed:** `2026-09-15`

## Summary

This architecture specification defines the approved testing strategy practice for Engineering Lead. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record ENG-008 is synthetic demo data created for OneFind and is not a real company policy.



## Control facts

| Field | Value |
|---|---|
| Record owner | Engineering Lead |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 73 p95 latency |
| Related records | `ENG-009`, `ENG-010` |

## Procedure

1. Confirm the request against record ID `ENG-008` and the current version `1.3.12`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 73 p95 latency.
4. Link `ENG-009` and `ENG-010` when the decision crosses team boundaries.

## Structured configuration

```sql
SELECT c.doc_id, -bm25(fts) AS score
FROM fts
WHERE fts MATCH ?
ORDER BY score DESC, c.doc_id ASC
LIMIT 12;
```

## Decision notes

- Prefer a reproducible local command and committed evidence over an undocumented manual step.
- If evidence is missing, mark the answer as unknown and link the responsible owner.
- Reuse this record's terminology consistently in slides, documentation, and demonstrations.

## Presentation prompt

Ask: “What does `ENG-008` require for testing strategy?” The answer should identify the owner, version, threshold, evidence rule, and related records.
