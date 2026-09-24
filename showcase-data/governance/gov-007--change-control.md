# Change Control — Governance Record GOV-007

- **Record ID:** `GOV-007`
- **Category:** Governance
- **Owner:** Project Governor
- **Status:** Approved
- **Version:** `1.2.19`
- **Last reviewed:** `2026-09-13`

## Summary

This governance control defines the approved change control practice for Project Governor. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record GOV-007 is synthetic demo data created for OneFind and is not a real company policy.



## Control facts

| Field | Value |
|---|---|
| Record owner | Project Governor |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 78 % approvals |
| Related records | `GOV-008`, `GOV-009` |

## Procedure

1. Confirm the request against record ID `GOV-007` and the current version `1.2.19`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 78 % approvals.
4. Link `GOV-008` and `GOV-009` when the decision crosses team boundaries.

## Structured configuration

```json
{
  "record_id": "GOV-007",
  "topic": "Change Control",
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

Ask: “What does `GOV-007` require for change control?” The answer should identify the owner, version, threshold, evidence rule, and related records.
