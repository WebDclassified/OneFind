# Cost Model — Finance Record FIN-008

- **Record ID:** `FIN-008`
- **Category:** Finance
- **Owner:** Finance Partner
- **Status:** Approved
- **Version:** `1.3.12`
- **Last reviewed:** `2026-09-15`

## Summary

This financial control defines the approved cost model practice for Finance Partner. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record FIN-008 is synthetic demo data created for OneFind and is not a real company policy.



## Control facts

| Field | Value |
|---|---|
| Record owner | Finance Partner |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 73 monthly credits |
| Related records | `FIN-009`, `FIN-010` |

## Procedure

1. Confirm the request against record ID `FIN-008` and the current version `1.3.12`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 73 monthly credits.
4. Link `FIN-009` and `FIN-010` when the decision crosses team boundaries.

## Structured configuration

```json
{
  "record_id": "FIN-008",
  "topic": "Cost Model",
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

Ask: “What does `FIN-008` require for cost model?” The answer should identify the owner, version, threshold, evidence rule, and related records.
