# Incident Response — Support Record SUP-009

- **Record ID:** `SUP-009`
- **Category:** Support
- **Owner:** Support Lead
- **Status:** Approved
- **Version:** `1.0.5`
- **Last reviewed:** `2026-09-17`

## Summary

This support playbook defines the approved incident response practice for Support Lead. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record SUP-009 is synthetic demo data created for OneFind and is not a real company policy.



## Control facts

| Field | Value |
|---|---|
| Record owner | Support Lead |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 94 minutes to resolution |
| Related records | `SUP-010`, `SUP-001` |

## Procedure

1. Confirm the request against record ID `SUP-009` and the current version `1.0.5`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 94 minutes to resolution.
4. Link `SUP-010` and `SUP-001` when the decision crosses team boundaries.

## Structured configuration

```http
POST /api/search HTTP/1.1
Content-Type: application/json

{"query":"Incident Response","mode":"hybrid","precision":"float","k":10}
```

## Decision notes

- Prefer a reproducible local command and committed evidence over an undocumented manual step.
- If evidence is missing, mark the answer as unknown and link the responsible owner.
- Reuse this record's terminology consistently in slides, documentation, and demonstrations.

## Presentation prompt

Ask: “What does `SUP-009` require for incident response?” The answer should identify the owner, version, threshold, evidence rule, and related records.
