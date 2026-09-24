# Secret Rotation — Operations Record OPS-008

- **Record ID:** `OPS-008`
- **Category:** Operations
- **Owner:** Operations Manager
- **Status:** Approved
- **Version:** `1.3.12`
- **Last reviewed:** `2026-09-15`

## Summary

This runbook defines the approved secret rotation practice for Operations Manager. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record OPS-008 is synthetic demo data created for OneFind and is not a real company policy.



## Control facts

| Field | Value |
|---|---|
| Record owner | Operations Manager |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 73 recovery minutes |
| Related records | `OPS-009`, `OPS-010` |

## Procedure

1. Confirm the request against record ID `OPS-008` and the current version `1.3.12`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 73 recovery minutes.
4. Link `OPS-009` and `OPS-010` when the decision crosses team boundaries.

## Structured configuration

```bash
onefind check
onefind search "Secret Rotation" --db data/ops-008.db --mode hybrid
onefind smoke --db data/ops-008.db --queries showcase-data/queries.jsonl
```

## Decision notes

- Prefer a reproducible local command and committed evidence over an undocumented manual step.
- If evidence is missing, mark the answer as unknown and link the responsible owner.
- Reuse this record's terminology consistently in slides, documentation, and demonstrations.

## Presentation prompt

Ask: “What does `OPS-008` require for secret rotation?” The answer should identify the owner, version, threshold, evidence rule, and related records.
