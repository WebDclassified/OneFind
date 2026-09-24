"""Generate OneFind's deterministic, original presentation showcase corpus.

The corpus is synthetic and project-specific. It is intentionally structured to
exercise long documents, nested paths, tables, code, dates, numeric constraints,
Unicode, FTS operators, disambiguation, and multi-relevant retrieval.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CATEGORIES = {
    "product": {
        "title": "Product",
        "prefix": "PROD",
        "owner": "Product Manager",
        "topics": [
            "Feature Flags", "Roadmap Governance", "User Personas", "Pricing Packaging",
            "Product Analytics", "Onboarding Funnel", "Release Notes", "Customer Feedback",
            "Localization", "Success Metrics",
        ],
        "artifact": "decision record",
        "metric": "% adoption",
        "language": "json",
    },
    "engineering": {
        "title": "Engineering",
        "prefix": "ENG",
        "owner": "Engineering Lead",
        "topics": [
            "System Architecture", "SQLite Schema", "BM25 Ranking", "Vector KNN",
            "Hybrid Fusion", "Search Relevance", "Performance Budget", "Testing Strategy",
            "Release Engineering", "Technical Debt",
        ],
        "artifact": "architecture specification",
        "metric": "p95 latency",
        "language": "sql",
    },
    "operations": {
        "title": "Operations",
        "prefix": "OPS",
        "owner": "Operations Manager",
        "topics": [
            "Deployment Procedure", "Rollback Procedure", "Monitoring", "Backup Restore",
            "Incident Response", "Service Levels", "Capacity Planning", "Secret Rotation",
            "Maintenance Window", "Disaster Recovery",
        ],
        "artifact": "runbook",
        "metric": "recovery minutes",
        "language": "bash",
    },
    "data": {
        "title": "Data",
        "prefix": "DATA",
        "owner": "Data Steward",
        "topics": [
            "Corpus Policy", "Privacy Classification", "Retention Schedule", "Dataset Manifest",
            "Evaluation Protocol", "Relevance Labels", "Quality Gates", "Data Lineage",
            "Deletion Requests", "Sampling Strategy",
        ],
        "artifact": "data control",
        "metric": "% complete",
        "language": "json",
    },
    "security": {
        "title": "Security",
        "prefix": "SEC",
        "owner": "Security Reviewer",
        "topics": [
            "Threat Model", "Content Security Policy", "Local Authentication", "Reset Safety",
            "Remote Binding", "Dependency Review", "Cross Site Scripting", "Audit Logging",
            "Secret Handling", "Vulnerability Triage",
        ],
        "artifact": "security control",
        "metric": "% controls verified",
        "language": "json",
    },
    "support": {
        "title": "Support",
        "prefix": "SUP",
        "owner": "Support Lead",
        "topics": [
            "Model Troubleshooting", "Missing Embeddings", "FTS5 Diagnostics", "Web Interface",
            "HTTP API", "Query Help", "Citation Help", "User Feedback", "Incident Response", "Status Updates",
        ],
        "artifact": "support playbook",
        "metric": "minutes to resolution",
        "language": "http",
    },
    "finance": {
        "title": "Finance",
        "prefix": "FIN",
        "owner": "Finance Partner",
        "topics": [
            "Project Budget", "Emergency Reserve", "Local Compute Cost", "Software Licenses",
            "Procurement", "Return On Investment", "Conference Grant", "Cost Model",
            "Forecast", "Financial Controls",
        ],
        "artifact": "financial control",
        "metric": "monthly credits",
        "language": "json",
    },
    "research": {
        "title": "Research",
        "prefix": "RES",
        "owner": "Research Lead",
        "topics": [
            "Paper Reproduction", "MiniLM Embeddings", "BEIR Datasets", "BM25 Evidence",
            "RRF Evidence", "Int8 Quantization", "Binary Quantization", "Evaluation Limits",
            "Statistical Significance", "Future Work",
        ],
        "artifact": "research note",
        "metric": "nDCG@10",
        "language": "json",
    },
    "demo": {
        "title": "Presentation",
        "prefix": "DEMO",
        "owner": "Presenter",
        "topics": [
            "Opening Slide", "Quickstart", "Curated Queries", "Troubleshooting", "Screenshot Tour",
            "Live Search", "Question And Answer", "Safe Reset", "Presentation Checklist", "Closing Summary",
        ],
        "artifact": "presentation cue",
        "metric": "demo steps",
        "language": "markdown",
    },
    "governance": {
        "title": "Governance",
        "prefix": "GOV",
        "owner": "Project Governor",
        "topics": [
            "Decision Ownership", "Architecture Records", "Review Cadence", "Open Source License",
            "Privacy Review", "Risk Register", "Change Control", "Compliance Evidence",
            "Approval Matrix", "Project Closure",
        ],
        "artifact": "governance control",
        "metric": "% approvals",
        "language": "json",
    },
    "people": {
        "title": "Team",
        "prefix": "TEAM",
        "owner": "Team Coordinator",
        "topics": [
            "Team Roles", "Onboarding Plan", "Communication Channels", "Meeting Rhythm",
            "Handoff Checklist", "Technical Training", "Research Ethics", "AI Assistance",
            "Remote Collaboration", "Recognition",
        ],
        "artifact": "team agreement",
        "metric": "hours per week",
        "language": "markdown",
    },
    "edge_cases": {
        "title": "Edge Cases",
        "prefix": "EDGE",
        "owner": "Quality Engineer",
        "topics": [
            "Unicode Cafe", "Multilingual Terms", "Long Unbroken Line", "JSON Structure",
            "FTS Operators", "Version Ranges", "Date Constraints", "Numeric Limits",
            "Duplicate Titles", "Inert HTML",
        ],
        "artifact": "edge-case fixture",
        "metric": "test assertions",
        "language": "text",
    },
}


def slug(text: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")
    return value


def pseudo(seed: int, minimum: int, maximum: int) -> int:
    return minimum + ((seed * 73 + 41) % (maximum - minimum + 1))


def code_block(language: str, record_id: str, topic: str, index: int) -> str:
    if language == "sql":
        return (
            "SELECT c.doc_id, -bm25(fts) AS score\n"
            "FROM fts\nWHERE fts MATCH ?\n"
            "ORDER BY score DESC, c.doc_id ASC\n"
            f"LIMIT {5 + index};"
        )
    if language == "bash":
        return (
            f"onefind check\n"
            f"onefind search \"{topic}\" --db data/{record_id.lower()}.db --mode hybrid\n"
            f"onefind smoke --db data/{record_id.lower()}.db --queries showcase-data/queries.jsonl"
        )
    if language == "http":
        return (
            f"POST /api/search HTTP/1.1\nContent-Type: application/json\n\n"
            f'{{"query":"{topic}","mode":"hybrid","precision":"float","k":10}}'
        )
    if language == "markdown":
        return f"- **{topic}**\n  - Evidence: `benchmarks/reports/`\n  - Owner: presentation lead"
    if language == "text":
        if "FTS Operators" in topic:
            return "FTS syntax words AND OR NOT NEAR appear literally and must remain safe user input."
        if "Version Ranges" in topic:
            return "Accepted: 1.0 through 2.9. Rejected: 3.0-alpha and 0.8-rc1."
        if "Date Constraints" in topic:
            return "Effective window: 2026-01-15 through 2026-12-31 (ISO 8601)."
        if "Numeric Limits" in topic:
            return "Minimum=10; maximum=10000; step=5; tolerance=0.25."
        if "Duplicate Titles" in topic:
            return "Human titles may collide; stable record IDs must disambiguate them."
        if "Inert HTML" in topic:
            return "Literal <img src=x onerror=\"alert(1)\"> and <script>alert(2)</script> text must remain inert."
        return "Unicode: café naïve façade — 日本語 — Ελληνικά — 🚀"
    return json.dumps(
        {
            "record_id": record_id,
            "topic": topic,
            "review": "2026-09-24",
            "controls": {"owner_assigned": True, "evidence_required": True},
        },
        indent=2,
    )


def build_document(category_key: str, index: int) -> tuple[str, str, dict]:
    category = CATEGORIES[category_key]
    topic = category["topics"][index]
    record_id = f"{category['prefix']}-{index + 1:03d}"
    filename = f"{record_id.lower()}--{slug(topic)}.md"
    relative_id = f"{category_key}/{filename}"
    owner = category["owner"]
    review_day = 1 + (index * 2)
    version = f"1.{index % 4}.{pseudo(index + 3, 1, 20)}"
    threshold = 70 + pseudo(index + 11, 0, 25)
    related_a = f"{category['prefix']}-{(index + 1) % 10 + 1:03d}"
    related_b = f"{category['prefix']}-{(index + 2) % 10 + 1:03d}"
    special = ""
    if category_key == "edge_cases":
        special = {
            0: "This record contains café, naïve, façade, and multilingual punctuation for Unicode normalization tests.",
            1: "Terms appear in English, español, français, Deutsch, 日本語, and Ελληνικά.",
            2: "Unbroken value: " + ("X" * 1400),
            3: "Literal JSON braces, arrays, booleans, null values, and nested metadata are part of the retrieval challenge.",
            4: "FTS syntax words AND OR NOT NEAR appear literally and must remain safe user input.",
            5: "Accepted versions include 1.0 through 2.9; rejected examples include 3.0-alpha and 0.8-rc1.",
            6: "Effective window: 2026-01-15 through 2026-12-31; review dates use ISO 8601.",
            7: "Numeric constraints: minimum 10, maximum 10,000, step 5, and tolerance 0.25.",
            8: "Two unrelated records may share a human title; stable record IDs must disambiguate them.",
            9: "Literal text <img src=x onerror=\"alert(1)\"> and <script>alert(2)</script> must render inert.",
        }[index]
    summary = (
        f"This {category['artifact']} defines the approved {topic.casefold()} practice for {owner}. "
        f"It combines policy, evidence, ownership, and a repeatable procedure so a presenter, reviewer, "
        f"or operator can answer the same question consistently. Record {record_id} is synthetic demo "
        "data created for OneFind and is not a real company policy."
    )
    facts = [
        f"Owner: {owner}",
        f"Status: Approved for presentation",
        f"Version: {version}",
        f"Last review: 2026-09-{review_day:02d}",
        f"Escalation threshold: {threshold} {category['metric']}",
    ]
    related = f"{related_a}, {related_b}"
    content = f"""# {topic} — {category['title']} Record {record_id}

- **Record ID:** `{record_id}`
- **Category:** {category['title']}
- **Owner:** {owner}
- **Status:** Approved
- **Version:** `{version}`
- **Last reviewed:** `2026-09-{review_day:02d}`

## Summary

{summary}

{special}

## Control facts

| Field | Value |
|---|---|
| Record owner | {owner} |
| Evidence required | Yes |
| Review cadence | Monthly |
| Escalation threshold | {threshold} {category['metric']} |
| Related records | `{related_a}`, `{related_b}` |

## Procedure

1. Confirm the request against record ID `{record_id}` and the current version `{version}`.
2. Collect the minimum evidence needed to answer without copying unrelated personal or operational data.
3. Apply the documented rule, record the outcome, and compare it with the escalation threshold of {threshold} {category['metric']}.
4. Link `{related_a}` and `{related_b}` when the decision crosses team boundaries.

## Structured configuration

```{category['language']}
{code_block(category['language'], record_id, topic, index)}
```

## Decision notes

- Prefer a reproducible local command and committed evidence over an undocumented manual step.
- If evidence is missing, mark the answer as unknown and link the responsible owner.
- Reuse this record's terminology consistently in slides, documentation, and demonstrations.

## Presentation prompt

Ask: “What does `{record_id}` require for {topic.casefold()}?” The answer should identify the owner, version, threshold, evidence rule, and related records.
"""
    return relative_id, content, {
        "id": record_id,
        "title": topic,
        "category": category["title"],
        "owner": owner,
        "version": version,
        "keywords": [topic.casefold(), slug(topic).replace("-", " "), owner.casefold()],
    }


def build_queries(records: list[dict], paths: list[str]) -> list[dict]:
    queries: list[dict] = []
    for position, record in enumerate(records):
        record_id = record["id"]
        topic = record["title"]
        keywords = record["keywords"]
        lexical = f"{record_id.lower().replace('-', ' ')} {keywords[0]}"
        mode = "lexical" if position % 3 else "hybrid"
        category = "lexical-identity" if mode == "lexical" else "hybrid-complex"
        queries.append(
            {
                "query": lexical,
                "relevant": [paths[position]],
                "mode": mode,
                "category": category,
                "features": ["exact-id", "structured-record"],
            }
        )
        if position % 2 == 0:
            queries.append(
                {
                    "query": f"How should our team handle {topic.casefold()} with clear ownership and evidence?",
                    "relevant": [paths[position]],
                    "mode": "semantic",
                    "category": "semantic-paraphrase",
                    "features": ["paraphrase", "ownership", "evidence"],
                }
            )
    by_category: dict[str, list[str]] = defaultdict(list)
    for record, path in zip(records, paths):
        by_category[record["category"]].append(path)
    for category, category_paths in by_category.items():
        queries.append(
            {
                "query": f"approved {category.casefold()} ownership evidence escalation",
                "relevant": category_paths[:5],
                "mode": "hybrid",
                "category": "multi-relevant",
                "features": ["multi-document", category.casefold()],
            }
        )
    for index in range(10):
        queries.append(
            {
                "query": f"unmatched jury token zq{index}xx nonexistent record",
                "relevant": [],
                "mode": "lexical",
                "category": "no-answer",
                "features": ["abstention", "hostile-operators"],
            }
        )
    for query in queries:
        query["query_id"] = f"Q{queries.index(query) + 1:04d}"
    return queries


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "showcase-data")
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    if args.clean and output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)

    records: list[dict] = []
    paths: list[str] = []
    for category_key, category in CATEGORIES.items():
        category_dir = output / category_key
        category_dir.mkdir(parents=True, exist_ok=True)
        for index, _topic in enumerate(category["topics"]):
            relative_id, content, metadata = build_document(category_key, index)
            path = output / relative_id
            path.write_text(content, encoding="utf-8", newline="\n")
            paths.append(relative_id)
            records.append(metadata)

    queries = build_queries(records, paths)
    query_path = output / "queries.jsonl"
    query_path.write_text(
        "".join(json.dumps(query, ensure_ascii=False) + "\n" for query in queries),
        encoding="utf-8",
        newline="\n",
    )

    combined_path = output / "documents.jsonl"
    combined_path.write_text(
        "".join(
            json.dumps(
                {
                    "_id": record["id"],
                    "title": record["title"],
                    "category": record["category"],
                    "owner": record["owner"],
                    "version": record["version"],
                    "text": (output / path).read_text(encoding="utf-8"),
                    "source": path,
                },
                ensure_ascii=False,
            )
            + "\n"
            for record, path in zip(records, paths)
        ),
        encoding="utf-8",
        newline="\n",
    )

    manifest = {
        "name": "OneFind Presentation Knowledge Base",
        "version": "1.1",
        "generated": "2026-09-24",
        "synthetic": True,
        "document_count": len(records),
        "query_count": len(queries),
        "category_counts": {
            key: len(value["topics"]) for key, value in CATEGORIES.items()
        },
        "complex_features": [
            "nested directories", "structured markdown", "tables", "code blocks",
            "JSONL", "stable IDs", "versions", "ISO dates", "numeric constraints",
            "cross references", "Unicode", "long unbroken text", "FTS operators",
            "inert HTML", "multi-relevant queries", "no-answer queries",
        ],
        "files": [
            {
                "path": path,
                "sha256": hashlib.sha256((output / path).read_bytes()).hexdigest(),
            }
            for path in paths
        ],
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    readme = f"""# OneFind Presentation Knowledge Base

This original synthetic corpus contains {len(records)} structured records across {len(CATEGORIES)} domains. It is designed for a live jury presentation and retrieval stress testing without paid APIs or private data.

## Coverage

- {len(records)} Markdown documents in nested category folders
- {len(CATEGORIES)} product, engineering, operations, data, security, support, finance, research, presentation, governance, team, and edge-case domains
- {len(queries)} structured gold queries: lexical identity, semantic paraphrase, hybrid, multi-relevant, and no-answer cases
- `documents.jsonl` combined corpus
- `manifest.json` SHA-256 inventory and feature inventory

## Presentation use

```bash
onefind index ./showcase-data --db data/showcase.db --embed
onefind smoke --db data/showcase.db --queries showcase-data/queries.jsonl --k 5
onefind serve --db data/showcase.db --port 8080
```

Try these exact questions:

- `What does PROD-001 require for feature flags?`
- `How do we roll back a failed deployment?`
- `What is the CSP security control?`
- `How are int8 vectors evaluated?`
- `What is the fastest way to diagnose missing embeddings?`

All values are fictional demonstration data and must not be presented as real organizational policy.
"""
    (output / "README.md").write_text(readme, encoding="utf-8", newline="\n")
    # Keep operator notes out of the ingestible showcase corpus.
    (output / "README.md").unlink(missing_ok=True)
    print(f"generated {len(records)} documents and {len(queries)} queries in {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
