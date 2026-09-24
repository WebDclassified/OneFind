# Risk Register — Governance Record GOV-006

- **Record ID:** `GOV-006`
- **Category:** Governance
- **Owner:** Project Governor
- **Status:** Approved
- **Version:** `1.1.6`
- **Last reviewed:** `2026-09-11`

## Summary

This governance control defines the approved risk register practice for Project Governor. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record GOV-006 is synthetic demo data created for OneFind and is not a real company policy.



## Control facts

| Field | Value |
|---|---|
| Record owner | Project Governor |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 83 % approvals |
| Related records | `GOV-007`, `GOV-008` |

## Procedure

1. Confirm the request against record ID `GOV-006` and the current version `1.1.6`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 83 % approvals.
4. Link `GOV-007` and `GOV-008` when the decision crosses team boundaries.

## Structured configuration

```json
{
  "record_id": "GOV-006",
  "topic": "Risk Register",
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

Ask: “What does `GOV-006` require for risk register?” The answer should identify the owner, version, threshold, evidence rule, and related records.
