# Architecture Records — Governance Record GOV-002

- **Record ID:** `GOV-002`
- **Category:** Governance
- **Owner:** Project Governor
- **Status:** Approved
- **Version:** `1.1.14`
- **Last reviewed:** `2026-09-03`

## Summary

This governance control defines the approved architecture records practice for Project Governor. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record GOV-002 is synthetic demo data created for OneFind and is not a real company policy.



## Control facts

| Field | Value |
|---|---|
| Record owner | Project Governor |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 77 % approvals |
| Related records | `GOV-003`, `GOV-004` |

## Procedure

1. Confirm the request against record ID `GOV-002` and the current version `1.1.14`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 77 % approvals.
4. Link `GOV-003` and `GOV-004` when the decision crosses team boundaries.

## Structured configuration

```json
{
  "record_id": "GOV-002",
  "topic": "Architecture Records",
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

Ask: “What does `GOV-002` require for architecture records?” The answer should identify the owner, version, threshold, evidence rule, and related records.
