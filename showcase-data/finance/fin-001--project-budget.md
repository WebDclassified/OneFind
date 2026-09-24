# Project Budget — Finance Record FIN-001

- **Record ID:** `FIN-001`
- **Category:** Finance
- **Owner:** Finance Partner
- **Status:** Approved
- **Version:** `1.0.1`
- **Last reviewed:** `2026-09-01`

## Summary

This financial control defines the approved project budget practice for Finance Partner. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record FIN-001 is synthetic demo data created for OneFind and is not a real company policy.



## Control facts

| Field | Value |
|---|---|
| Record owner | Finance Partner |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 82 monthly credits |
| Related records | `FIN-002`, `FIN-003` |

## Procedure

1. Confirm the request against record ID `FIN-001` and the current version `1.0.1`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 82 monthly credits.
4. Link `FIN-002` and `FIN-003` when the decision crosses team boundaries.

## Structured configuration

```json
{
  "record_id": "FIN-001",
  "topic": "Project Budget",
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

Ask: “What does `FIN-001` require for project budget?” The answer should identify the owner, version, threshold, evidence rule, and related records.
