# Future Work — Research Record RES-010

- **Record ID:** `RES-010`
- **Category:** Research
- **Owner:** Research Lead
- **Status:** Approved
- **Version:** `1.1.18`
- **Last reviewed:** `2026-09-19`

## Summary

This research note defines the approved future work practice for Research Lead. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record RES-010 is synthetic demo data created for OneFind and is not a real company policy.



## Control facts

| Field | Value |
|---|---|
| Record owner | Research Lead |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 89 nDCG@10 |
| Related records | `RES-001`, `RES-002` |

## Procedure

1. Confirm the request against record ID `RES-010` and the current version `1.1.18`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 89 nDCG@10.
4. Link `RES-001` and `RES-002` when the decision crosses team boundaries.

## Structured configuration

```json
{
  "record_id": "RES-010",
  "topic": "Future Work",
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

Ask: “What does `RES-010` require for future work?” The answer should identify the owner, version, threshold, evidence rule, and related records.
