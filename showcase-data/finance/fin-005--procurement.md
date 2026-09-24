# Procurement — Finance Record FIN-005

- **Record ID:** `FIN-005`
- **Category:** Finance
- **Owner:** Finance Partner
- **Status:** Approved
- **Version:** `1.0.13`
- **Last reviewed:** `2026-09-09`

## Summary

This financial control defines the approved procurement practice for Finance Partner. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record FIN-005 is synthetic demo data created for OneFind and is not a real company policy.



## Control facts

| Field | Value |
|---|---|
| Record owner | Finance Partner |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 88 monthly credits |
| Related records | `FIN-006`, `FIN-007` |

## Procedure

1. Confirm the request against record ID `FIN-005` and the current version `1.0.13`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 88 monthly credits.
4. Link `FIN-006` and `FIN-007` when the decision crosses team boundaries.

## Structured configuration

```json
{
  "record_id": "FIN-005",
  "topic": "Procurement",
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

Ask: “What does `FIN-005` require for procurement?” The answer should identify the owner, version, threshold, evidence rule, and related records.
