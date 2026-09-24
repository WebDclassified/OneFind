# Return On Investment — Finance Record FIN-006

- **Record ID:** `FIN-006`
- **Category:** Finance
- **Owner:** Finance Partner
- **Status:** Approved
- **Version:** `1.1.6`
- **Last reviewed:** `2026-09-11`

## Summary

This financial control defines the approved return on investment practice for Finance Partner. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record FIN-006 is synthetic demo data created for OneFind and is not a real company policy.



## Control facts

| Field | Value |
|---|---|
| Record owner | Finance Partner |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 83 monthly credits |
| Related records | `FIN-007`, `FIN-008` |

## Procedure

1. Confirm the request against record ID `FIN-006` and the current version `1.1.6`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 83 monthly credits.
4. Link `FIN-007` and `FIN-008` when the decision crosses team boundaries.

## Structured configuration

```json
{
  "record_id": "FIN-006",
  "topic": "Return On Investment",
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

Ask: “What does `FIN-006` require for return on investment?” The answer should identify the owner, version, threshold, evidence rule, and related records.
