# Monitoring — Operations Record OPS-003

- **Record ID:** `OPS-003`
- **Category:** Operations
- **Owner:** Operations Manager
- **Status:** Approved
- **Version:** `1.2.7`
- **Last reviewed:** `2026-09-05`

## Summary

This runbook defines the approved monitoring practice for Operations Manager. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record OPS-003 is synthetic demo data created for OneFind and is not a real company policy.



## Control facts

| Field | Value |
|---|---|
| Record owner | Operations Manager |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 72 recovery minutes |
| Related records | `OPS-004`, `OPS-005` |

## Procedure

1. Confirm the request against record ID `OPS-003` and the current version `1.2.7`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 72 recovery minutes.
4. Link `OPS-004` and `OPS-005` when the decision crosses team boundaries.

## Structured configuration

```bash
onefind check
onefind search "Monitoring" --db data/ops-003.db --mode hybrid
onefind smoke --db data/ops-003.db --queries showcase-data/queries.jsonl
```

## Decision notes

- Prefer a reproducible local command and committed evidence over an undocumented manual step.
- If evidence is missing, mark the answer as unknown and link the responsible owner.
- Reuse this record's terminology consistently in slides, documentation, and demonstrations.

## Presentation prompt

Ask: “What does `OPS-003` require for monitoring?” The answer should identify the owner, version, threshold, evidence rule, and related records.
