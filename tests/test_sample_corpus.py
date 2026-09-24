"""The bundled corpus and gold queries must remain useful and self-contained."""

import json
from pathlib import Path

from onefind.ingest import load_documents


ROOT = Path("sample-data")


def _document_ids() -> set[str]:
    return {document.doc_id for document in load_documents(ROOT)}


def test_bundled_corpus_is_broad_and_collision_free():
    documents = list(load_documents(ROOT))
    ids = {document.doc_id for document in documents}
    assert len(documents) == 20
    assert len(ids) == 20
    assert {"archive/plan.md", "notes/plan.md"} <= ids
    assert {"craft/pottery-glaze.txt", "technology/vector-embeddings.md"} <= ids
    assert any("–" in document.body for document in documents)
    assert all(document.body.strip() for document in documents)


def test_gold_queries_reference_real_documents():
    ids = _document_ids()
    rows = [
        json.loads(line)
        for line in (ROOT / "queries.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) >= 24
    assert {"lexical", "semantic", "disambiguation", "no-answer"} <= {
        row["category"] for row in rows
    }
    for row in rows:
        assert row["query"].strip()
        assert row["mode"] in {"lexical", "semantic", "hybrid"}
        assert set(row["relevant"]) <= ids


def test_default_corpus_does_not_embed_active_html_payloads():
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in ROOT.rglob("*")
        if path.is_file() and path.suffix.lower() in {".md", ".txt"}
    ).casefold()
    assert "<script" not in text
    assert "onerror=" not in text
