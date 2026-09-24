# BM25 Evidence — Research Record RES-004

- **Record ID:** `RES-004`
- **Category:** Research
- **Owner:** Research Lead
- **Status:** Approved
- **Version:** `1.3.20`
- **Last reviewed:** `2026-09-07`

## Summary

This research note defines the approved bm25 evidence practice for Research Lead. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record RES-004 is synthetic demo data created for OneFind and is not a real company policy.



## Control facts

| Field | Value |
|---|---|
| Record owner | Research Lead |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 93 nDCG@10 |
| Related records | `RES-005`, `RES-006` |

## Procedure

1. Confirm the request against record ID `RES-004` and the current version `1.3.20`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 93 nDCG@10.
4. Link `RES-005` and `RES-006` when the decision crosses team boundaries.

## Structured configuration

```json
{
  "record_id": "RES-004",
  "topic": "BM25 Evidence",
  "review": "2026-09-24",
  "controls": {
    "owner_assigned": true,
    "evidence_required": true
  }
}
```

## Decision notes

- Prefer a reproducible local command and committed evidence over an undocumented manual step.
- If evidence is missing, mark the answer as unknown and link the responsible owner.
- Reuse this record's terminology consistently in slides, documentation, and demonstrations.

## Presentation prompt

Ask: “What does `RES-004` require for bm25 evidence?” The answer should identify the owner, version, threshold, evidence rule, and related records.
