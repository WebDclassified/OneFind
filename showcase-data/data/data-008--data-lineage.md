# Data Lineage — Data Record DATA-008

- **Record ID:** `DATA-008`
- **Category:** Data
- **Owner:** Data Steward
- **Status:** Approved
- **Version:** `1.3.12`
- **Last reviewed:** `2026-09-15`

## Summary

This data control defines the approved data lineage practice for Data Steward. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record DATA-008 is synthetic demo data created for OneFind and is not a real company policy.



## Control facts

| Field | Value |
|---|---|
| Record owner | Data Steward |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 73 % complete |
| Related records | `DATA-009`, `DATA-010` |

## Procedure

1. Confirm the request against record ID `DATA-008` and the current version `1.3.12`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 73 % complete.
4. Link `DATA-009` and `DATA-010` when the decision crosses team boundaries.

## Structured configuration

```json
{
  "record_id": "DATA-008",
  "topic": "Data Lineage",
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

Ask: “What does `DATA-008` require for data lineage?” The answer should identify the owner, version, threshold, evidence rule, and related records.
