# Secret Handling — Security Record SEC-009

- **Record ID:** `SEC-009`
- **Category:** Security
- **Owner:** Security Reviewer
- **Status:** Approved
- **Version:** `1.0.5`
- **Last reviewed:** `2026-09-17`

## Summary

This security control defines the approved secret handling practice for Security Reviewer. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record SEC-009 is synthetic demo data created for OneFind and is not a real company policy.



## Control facts

| Field | Value |
|---|---|
| Record owner | Security Reviewer |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 94 % controls verified |
| Related records | `SEC-010`, `SEC-001` |

## Procedure

1. Confirm the request against record ID `SEC-009` and the current version `1.0.5`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 94 % controls verified.
4. Link `SEC-010` and `SEC-001` when the decision crosses team boundaries.

## Structured configuration

```json
{
  "record_id": "SEC-009",
  "topic": "Secret Handling",
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

Ask: “What does `SEC-009` require for secret handling?” The answer should identify the owner, version, threshold, evidence rule, and related records.
