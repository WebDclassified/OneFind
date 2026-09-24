# MiniLM Embeddings — Research Record RES-002

- **Record ID:** `RES-002`
- **Category:** Research
- **Owner:** Research Lead
- **Status:** Approved
- **Version:** `1.1.14`
- **Last reviewed:** `2026-09-03`

## Summary

This research note defines the approved minilm embeddings practice for Research Lead. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record RES-002 is synthetic demo data created for OneFind and is not a real company policy.



## Control facts

| Field | Value |
|---|---|
| Record owner | Research Lead |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 77 nDCG@10 |
| Related records | `RES-003`, `RES-004` |

## Procedure

1. Confirm the request against record ID `RES-002` and the current version `1.1.14`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 77 nDCG@10.
4. Link `RES-003` and `RES-004` when the decision crosses team boundaries.

## Structured configuration

```json
{
  "record_id": "RES-002",
  "topic": "MiniLM Embeddings",
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

Ask: “What does `RES-002` require for minilm embeddings?” The answer should identify the owner, version, threshold, evidence rule, and related records.
