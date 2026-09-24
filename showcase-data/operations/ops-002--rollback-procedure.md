# Rollback Procedure — Operations Record OPS-002

- **Record ID:** `OPS-002`
- **Category:** Operations
- **Owner:** Operations Manager
- **Status:** Approved
- **Version:** `1.1.14`
- **Last reviewed:** `2026-09-03`

## Summary

This runbook defines the approved rollback procedure practice for Operations Manager. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record OPS-002 is synthetic demo data created for OneFind and is not a real company policy.



## Control facts

| Field | Value |
|---|---|
| Record owner | Operations Manager |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 77 recovery minutes |
| Related records | `OPS-003`, `OPS-004` |

## Procedure

1. Confirm the request against record ID `OPS-002` and the current version `1.1.14`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 77 recovery minutes.
4. Link `OPS-003` and `OPS-004` when the decision crosses team boundaries.

## Structured configuration

```bash
onefind check
onefind search "Rollback Procedure" --db data/ops-002.db --mode hybrid
onefind smoke --db data/ops-002.db --queries showcase-data/queries.jsonl
```

## Decision notes

- Prefer a reproducible local command and committed evidence over an undocumented manual step.
- If evidence is missing, mark the answer as unknown and link the responsible owner.
- Reuse this record's terminology consistently in slides, documentation, and demonstrations.

## Presentation prompt

Ask: “What does `OPS-002` require for rollback procedure?” The answer should identify the owner, version, threshold, evidence rule, and related records.
