# Privacy Review — Governance Record GOV-005

- **Record ID:** `GOV-005`
- **Category:** Governance
- **Owner:** Project Governor
- **Status:** Approved
- **Version:** `1.0.13`
- **Last reviewed:** `2026-09-09`

## Summary

This governance control defines the approved privacy review practice for Project Governor. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record GOV-005 is synthetic demo data created for OneFind and is not a real company policy.



## Control facts

| Field | Value |
|---|---|
| Record owner | Project Governor |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 88 % approvals |
| Related records | `GOV-006`, `GOV-007` |

## Procedure

1. Confirm the request against record ID `GOV-005` and the current version `1.0.13`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 88 % approvals.
4. Link `GOV-006` and `GOV-007` when the decision crosses team boundaries.

## Structured configuration

```json
{
  "record_id": "GOV-005",
  "topic": "Privacy Review",
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

Ask: “What does `GOV-005` require for privacy review?” The answer should identify the owner, version, threshold, evidence rule, and related records.
