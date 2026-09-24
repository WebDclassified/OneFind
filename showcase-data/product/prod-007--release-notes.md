# Release Notes — Product Record PROD-007

- **Record ID:** `PROD-007`
- **Category:** Product
- **Owner:** Product Manager
- **Status:** Approved
- **Version:** `1.2.19`
- **Last reviewed:** `2026-09-13`

## Summary

This decision record defines the approved release notes practice for Product Manager. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record PROD-007 is synthetic demo data created for OneFind and is not a real company policy.



## Control facts

| Field | Value |
|---|---|
| Record owner | Product Manager |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 78 % adoption |
| Related records | `PROD-008`, `PROD-009` |

## Procedure

1. Confirm the request against record ID `PROD-007` and the current version `1.2.19`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 78 % adoption.
4. Link `PROD-008` and `PROD-009` when the decision crosses team boundaries.

## Structured configuration

```json
{
  "record_id": "PROD-007",
  "topic": "Release Notes",
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

Ask: “What does `PROD-007` require for release notes?” The answer should identify the owner, version, threshold, evidence rule, and related records.
