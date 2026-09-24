# Compliance Evidence — Governance Record GOV-008

- **Record ID:** `GOV-008`
- **Category:** Governance
- **Owner:** Project Governor
- **Status:** Approved
- **Version:** `1.3.12`
- **Last reviewed:** `2026-09-15`

## Summary

This governance control defines the approved compliance evidence practice for Project Governor. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record GOV-008 is synthetic demo data created for OneFind and is not a real company policy.



## Control facts

| Field | Value |
|---|---|
| Record owner | Project Governor |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 73 % approvals |
| Related records | `GOV-009`, `GOV-010` |

## Procedure

1. Confirm the request against record ID `GOV-008` and the current version `1.3.12`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 73 % approvals.
4. Link `GOV-009` and `GOV-010` when the decision crosses team boundaries.

## Structured configuration

```json
{
  "record_id": "GOV-008",
  "topic": "Compliance Evidence",
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

Ask: “What does `GOV-008` require for compliance evidence?” The answer should identify the owner, version, threshold, evidence rule, and related records.
