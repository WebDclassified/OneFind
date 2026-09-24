"""Integrity and complexity gates for the large presentation corpus."""

import hashlib
import json
from pathlib import Path

from onefind.ingest import load_documents


ROOT = Path("showcase-data")


def _jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_showcase_has_large_diverse_corpus():
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    documents = list(load_documents(ROOT))
    ids = [document.doc_id for document in documents]

    assert manifest["synthetic"] is True
    assert manifest["document_count"] == 120
    assert len(documents) == 120
    assert len(ids) == len(set(ids)) == 120
    assert len(manifest["category_counts"]) == 12
    assert set(manifest["category_counts"].values()) == {10}
    assert sum(path.stat().st_size for path in ROOT.rglob("*.md")) > 100_000


def test_manifest_hashes_match_every_document():
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["files"]) == 120
    for item in manifest["files"]:
        path = ROOT / item["path"]
        assert path.is_file()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == item["sha256"]


def test_gold_queries_cover_all_retrieval_classes():
    queries = _jsonl(ROOT / "queries.jsonl")
    ids = {document.doc_id for document in load_documents(ROOT)}
    categories = {query["category"] for query in queries}

    assert len(queries) == 202
    assert {
        "lexical-identity",
        "semantic-paraphrase",
        "hybrid-complex",
        "multi-relevant",
        "no-answer",
    } <= categories
    assert sum(query["mode"] == "lexical" for query in queries) >= 90
    assert sum(query["mode"] == "semantic" for query in queries) >= 60
    assert sum(query["mode"] == "hybrid" for query in queries) >= 30
    assert sum(not query["relevant"] for query in queries) == 10
    assert sum(len(query["relevant"]) > 1 for query in queries) >= 12
    assert len({query["query_id"] for query in queries}) == 202

    for query in queries:
        assert query["query"].strip()
        assert query["mode"] in {"lexical", "semantic", "hybrid"}
        assert set(query["relevant"]) <= ids
        assert query["features"]


def test_combined_jsonl_mirrors_markdown_corpus():
    rows = _jsonl(ROOT / "documents.jsonl")
    assert len(rows) == 120
    assert len({row["_id"] for row in rows}) == 120
    assert {row["source"] for row in rows} == {
        item["path"] for item in json.loads(
            (ROOT / "manifest.json").read_text(encoding="utf-8")
        )["files"]
    }


def test_corpus_contains_complex_presentation_edge_cases():
    text_by_id = {
        document.doc_id: document.title + "\n" + document.body
        for document in load_documents(ROOT)
    }
    all_text = "\n".join(text_by_id.values())

    assert "café naïve façade" in all_text
    assert "日本語" in all_text and "Ελληνικά" in all_text
    assert "X" * 1000 in all_text
    assert "AND OR NOT NEAR" in all_text
    assert "2026-01-15 through 2026-12-31" in all_text
    assert "Minimum=10; maximum=10000" in all_text
    assert '<img src=x onerror="alert(1)">' in all_text
    assert "<script>alert(2)</script>" in all_text
    assert "| Record owner |" in all_text
    assert "```sql" in all_text and "```json" in all_text
    assert "The answer should identify" in all_text
