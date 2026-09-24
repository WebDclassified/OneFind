# Backup Restore — Operations Record OPS-004

- **Record ID:** `OPS-004`
- **Category:** Operations
- **Owner:** Operations Manager
- **Status:** Approved
- **Version:** `1.3.20`
- **Last reviewed:** `2026-09-07`

## Summary

This runbook defines the approved backup restore practice for Operations Manager. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record OPS-004 is synthetic demo data created for OneFind and is not a real company policy.



## Control facts

| Field | Value |
|---|---|
| Record owner | Operations Manager |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 93 recovery minutes |
| Related records | `OPS-005`, `OPS-006` |

## Procedure

1. Confirm the request against record ID `OPS-004` and the current version `1.3.20`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 93 recovery minutes.
4. Link `OPS-005` and `OPS-006` when the decision crosses team boundaries.

## Structured configuration

```bash
onefind check
onefind search "Backup Restore" --db data/ops-004.db --mode hybrid
onefind smoke --db data/ops-004.db --queries showcase-data/queries.jsonl
```

## Decision notes

- Prefer a reproducible local command and committed evidence over an undocumented manual step.
- If evidence is missing, mark the answer as unknown and link the responsible owner.
- Reuse this record's terminology consistently in slides, documentation, and demonstrations.

## Presentation prompt

Ask: “What does `OPS-004` require for backup restore?” The answer should identify the owner, version, threshold, evidence rule, and related records.
