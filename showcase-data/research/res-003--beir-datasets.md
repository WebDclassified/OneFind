# BEIR Datasets — Research Record RES-003

- **Record ID:** `RES-003`
- **Category:** Research
- **Owner:** Research Lead
- **Status:** Approved
- **Version:** `1.2.7`
- **Last reviewed:** `2026-09-05`

## Summary

This research note defines the approved beir datasets practice for Research Lead. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record RES-003 is synthetic demo data created for OneFind and is not a real company policy.



## Control facts

| Field | Value |
|---|---|
| Record owner | Research Lead |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 72 nDCG@10 |
| Related records | `RES-004`, `RES-005` |

## Procedure

1. Confirm the request against record ID `RES-003` and the current version `1.2.7`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 72 nDCG@10.
4. Link `RES-004` and `RES-005` when the decision crosses team boundaries.

## Structured configuration

```json
{
  "record_id": "RES-003",
  "topic": "BEIR Datasets",
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

Ask: “What does `RES-003` require for beir datasets?” The answer should identify the owner, version, threshold, evidence rule, and related records.
