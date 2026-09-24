# Forecast — Finance Record FIN-009

- **Record ID:** `FIN-009`
- **Category:** Finance
- **Owner:** Finance Partner
- **Status:** Approved
- **Version:** `1.0.5`
- **Last reviewed:** `2026-09-17`

## Summary

This financial control defines the approved forecast practice for Finance Partner. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record FIN-009 is synthetic demo data created for OneFind and is not a real company policy.



## Control facts

| Field | Value |
|---|---|
| Record owner | Finance Partner |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 94 monthly credits |
| Related records | `FIN-010`, `FIN-001` |

## Procedure

1. Confirm the request against record ID `FIN-009` and the current version `1.0.5`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 94 monthly credits.
4. Link `FIN-010` and `FIN-001` when the decision crosses team boundaries.

## Structured configuration

```json
{
  "record_id": "FIN-009",
  "topic": "Forecast",
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

Ask: “What does `FIN-009` require for forecast?” The answer should identify the owner, version, threshold, evidence rule, and related records.
