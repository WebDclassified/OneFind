# Quality Gates — Data Record DATA-007

- **Record ID:** `DATA-007`
- **Category:** Data
- **Owner:** Data Steward
- **Status:** Approved
- **Version:** `1.2.19`
- **Last reviewed:** `2026-09-13`

## Summary

This data control defines the approved quality gates practice for Data Steward. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record DATA-007 is synthetic demo data created for OneFind and is not a real company policy.



## Control facts

| Field | Value |
|---|---|
| Record owner | Data Steward |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 78 % complete |
| Related records | `DATA-008`, `DATA-009` |

## Procedure

1. Confirm the request against record ID `DATA-007` and the current version `1.2.19`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 78 % complete.
4. Link `DATA-008` and `DATA-009` when the decision crosses team boundaries.

## Structured configuration

```json
{
  "record_id": "DATA-007",
  "topic": "Quality Gates",
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

Ask: “What does `DATA-007` require for quality gates?” The answer should identify the owner, version, threshold, evidence rule, and related records.
