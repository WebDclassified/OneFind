# Project Closure — Governance Record GOV-010

- **Record ID:** `GOV-010`
- **Category:** Governance
- **Owner:** Project Governor
- **Status:** Approved
- **Version:** `1.1.18`
- **Last reviewed:** `2026-09-19`

## Summary

This governance control defines the approved project closure practice for Project Governor. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record GOV-010 is synthetic demo data created for OneFind and is not a real company policy.



## Control facts

| Field | Value |
|---|---|
| Record owner | Project Governor |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 89 % approvals |
| Related records | `GOV-001`, `GOV-002` |

## Procedure

1. Confirm the request against record ID `GOV-010` and the current version `1.1.18`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 89 % approvals.
4. Link `GOV-001` and `GOV-002` when the decision crosses team boundaries.

## Structured configuration

```json
{
  "record_id": "GOV-010",
  "topic": "Project Closure",
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

Ask: “What does `GOV-010` require for project closure?” The answer should identify the owner, version, threshold, evidence rule, and related records.
