# Threat Model — Security Record SEC-001

- **Record ID:** `SEC-001`
- **Category:** Security
- **Owner:** Security Reviewer
- **Status:** Approved
- **Version:** `1.0.1`
- **Last reviewed:** `2026-09-01`

## Summary

This security control defines the approved threat model practice for Security Reviewer. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record SEC-001 is synthetic demo data created for OneFind and is not a real company policy.



## Control facts

| Field | Value |
|---|---|
| Record owner | Security Reviewer |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 82 % controls verified |
| Related records | `SEC-002`, `SEC-003` |

## Procedure

1. Confirm the request against record ID `SEC-001` and the current version `1.0.1`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 82 % controls verified.
4. Link `SEC-002` and `SEC-003` when the decision crosses team boundaries.

## Structured configuration

```json
{
  "record_id": "SEC-001",
  "topic": "Threat Model",
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

Ask: “What does `SEC-001` require for threat model?” The answer should identify the owner, version, threshold, evidence rule, and related records.
