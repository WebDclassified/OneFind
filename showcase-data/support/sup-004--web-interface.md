# Web Interface — Support Record SUP-004

- **Record ID:** `SUP-004`
- **Category:** Support
- **Owner:** Support Lead
- **Status:** Approved
- **Version:** `1.3.20`
- **Last reviewed:** `2026-09-07`

## Summary

This support playbook defines the approved web interface practice for Support Lead. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record SUP-004 is synthetic demo data created for OneFind and is not a real company policy.



## Control facts

| Field | Value |
|---|---|
| Record owner | Support Lead |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 93 minutes to resolution |
| Related records | `SUP-005`, `SUP-006` |

## Procedure

1. Confirm the request against record ID `SUP-004` and the current version `1.3.20`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 93 minutes to resolution.
4. Link `SUP-005` and `SUP-006` when the decision crosses team boundaries.

## Structured configuration

```http
POST /api/search HTTP/1.1
Content-Type: application/json

{"query":"Web Interface","mode":"hybrid","precision":"float","k":10}
```

## Decision notes

- Prefer a reproducible local command and committed evidence over an undocumented manual step.
- If evidence is missing, mark the answer as unknown and link the responsible owner.
- Reuse this record's terminology consistently in slides, documentation, and demonstrations.

## Presentation prompt

Ask: “What does `SUP-004` require for web interface?” The answer should identify the owner, version, threshold, evidence rule, and related records.
