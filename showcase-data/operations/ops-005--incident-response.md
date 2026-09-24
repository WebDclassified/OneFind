# Incident Response — Operations Record OPS-005

- **Record ID:** `OPS-005`
- **Category:** Operations
- **Owner:** Operations Manager
- **Status:** Approved
- **Version:** `1.0.13`
- **Last reviewed:** `2026-09-09`

## Summary

This runbook defines the approved incident response practice for Operations Manager. It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, or operator can answer the same question consistently. Record OPS-005 is synthetic demo data created for OneFind and is not a real company policy.



## Control facts

| Field | Value |
|---|---|
| Record owner | Operations Manager |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | 88 recovery minutes |
| Related records | `OPS-006`, `OPS-007` |

## Procedure

1. Confirm the request against record ID `OPS-005` and the current version `1.0.13`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of 88 recovery minutes.
4. Link `OPS-006` and `OPS-007` when the decision crosses team boundaries.

## Structured configuration

```bash
onefind check
onefind search "Incident Response" --db data/ops-005.db --mode hybrid
onefind smoke --db data/ops-005.db --queries showcase-data/queries.jsonl
```

## Decision notes

- Prefer a reproducible local command and committed evidence over an undocumented manual step.
- If evidence is missing, mark the answer as unknown and link the responsible owner.
- Reuse this record's terminology consistently in slides, documentation, and demonstrations.

## Presentation prompt

Ask: “What does `OPS-005` require for incident response?” The answer should identify the owner, version, threshold, evidence rule, and related records.
