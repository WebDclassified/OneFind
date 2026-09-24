# Open Source License — Governance Record GOV-004

- **Record ID:** `GOV-004`
- **Category:** Governance
- **Owner:** Project Governor
- **Status:** Approved
- **Version:** `1.3.20`
- **Last reviewed:** `2026-09-07`

## Summary

This governance control defines the approved open source license practice for Project Governor. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record GOV-004 is synthetic demo data created for OneFind and is not a real company policy.



## Control facts

| Field | Value |
|---|---|
| Record owner | Project Governor |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 93 % approvals |
| Related records | `GOV-005`, `GOV-006` |

## Procedure

1. Confirm the request against record ID `GOV-004` and the current version `1.3.20`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 93 % approvals.
4. Link `GOV-005` and `GOV-006` when the decision crosses team boundaries.

## Structured configuration

```json
{
  "record_id": "GOV-004",
  "topic": "Open Source License",
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

Ask: “What does `GOV-004` require for open source license?” The answer should identify the owner, version, threshold, evidence rule, and related records.
