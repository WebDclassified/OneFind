# Product Analytics — Product Record PROD-005

- **Record ID:** `PROD-005`
- **Category:** Product
- **Owner:** Product Manager
- **Status:** Approved
- **Version:** `1.0.13`
- **Last reviewed:** `2026-09-09`

## Summary

This decision record defines the approved product analytics practice for Product Manager. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record PROD-005 is synthetic demo data created for OneFind and is not a real company policy.



## Control facts

| Field | Value |
|---|---|
| Record owner | Product Manager |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 88 % adoption |
| Related records | `PROD-006`, `PROD-007` |

## Procedure

1. Confirm the request against record ID `PROD-005` and the current version `1.0.13`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 88 % adoption.
4. Link `PROD-006` and `PROD-007` when the decision crosses team boundaries.

## Structured configuration

```json
{
  "record_id": "PROD-005",
  "topic": "Product Analytics",
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

Ask: “What does `PROD-005` require for product analytics?” The answer should identify the owner, version, threshold, evidence rule, and related records.
