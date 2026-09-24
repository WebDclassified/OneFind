# HTTP API — Support Record SUP-005

- **Record ID:** `SUP-005`
- **Category:** Support
- **Owner:** Support Lead
- **Status:** Approved
- **Version:** `1.0.13`
- **Last reviewed:** `2026-09-09`

## Summary

This support playbook defines the approved http api practice for Support Lead. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record SUP-005 is synthetic demo data created for OneFind and is not a real company policy.



## Control facts

| Field | Value |
|---|---|
| Record owner | Support Lead |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 88 minutes to resolution |
| Related records | `SUP-006`, `SUP-007` |

## Procedure

1. Confirm the request against record ID `SUP-005` and the current version `1.0.13`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 88 minutes to resolution.
4. Link `SUP-006` and `SUP-007` when the decision crosses team boundaries.

## Structured configuration

```http
POST /api/search HTTP/1.1
Content-Type: application/json

{"query":"HTTP API","mode":"hybrid","precision":"float","k":10}
```

## Decision notes

- Prefer a reproducible local command and committed evidence over an undocumented manual step.
- If evidence is missing, mark the answer as unknown and link the responsible owner.
- Reuse this record's terminology consistently in slides, documentation, and demonstrations.

## Presentation prompt

Ask: “What does `SUP-005` require for http api?” The answer should identify the owner, version, threshold, evidence rule, and related records.
