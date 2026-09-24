# Audit Logging — Security Record SEC-008

- **Record ID:** `SEC-008`
- **Category:** Security
- **Owner:** Security Reviewer
- **Status:** Approved
- **Version:** `1.3.12`
- **Last reviewed:** `2026-09-15`

## Summary

This security control defines the approved audit logging practice for Security Reviewer. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record SEC-008 is synthetic demo data created for OneFind and is not a real company policy.



## Control facts

| Field | Value |
|---|---|
| Record owner | Security Reviewer |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 73 % controls verified |
| Related records | `SEC-009`, `SEC-010` |

## Procedure

1. Confirm the request against record ID `SEC-008` and the current version `1.3.12`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 73 % controls verified.
4. Link `SEC-009` and `SEC-010` when the decision crosses team boundaries.

## Structured configuration

```json
{
  "record_id": "SEC-008",
  "topic": "Audit Logging",
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

Ask: “What does `SEC-008` require for audit logging?” The answer should identify the owner, version, threshold, evidence rule, and related records.
