# Query Help — Support Record SUP-006

- **Record ID:** `SUP-006`
- **Category:** Support
- **Owner:** Support Lead
- **Status:** Approved
- **Version:** `1.1.6`
- **Last reviewed:** `2026-09-11`

## Summary

This support playbook defines the approved query help practice for Support Lead. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record SUP-006 is synthetic demo data created for OneFind and is not a real company policy.



## Control facts

| Field | Value |
|---|---|
| Record owner | Support Lead |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 83 minutes to resolution |
| Related records | `SUP-007`, `SUP-008` |

## Procedure

1. Confirm the request against record ID `SUP-006` and the current version `1.1.6`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 83 minutes to resolution.
4. Link `SUP-007` and `SUP-008` when the decision crosses team boundaries.

## Structured configuration

```http
POST /api/search HTTP/1.1
Content-Type: application/json

{"query":"Query Help","mode":"hybrid","precision":"float","k":10}
```

## Decision notes

- Prefer a reproducible local command and committed evidence over an undocumented manual step.
- If evidence is missing, mark the answer as unknown and link the responsible owner.
- Reuse this record's terminology consistently in slides, documentation, and demonstrations.

## Presentation prompt

Ask: “What does `SUP-006` require for query help?” The answer should identify the owner, version, threshold, evidence rule, and related records.
