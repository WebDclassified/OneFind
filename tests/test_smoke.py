"""Bundled gold-query quality gate using the free local MiniLM model."""

import pytest

pytest.importorskip("sentence_transformers")

from onefind.embed import DEFAULT_MODEL
from onefind.evaluate import run_smoke
from onefind.ingest import ingest_path
from onefind.store import Index


def test_bundled_smoke_corpus_meets_quality_gate(tmp_path):
    with Index.open(tmp_path / "sample.db") as index:
        ingest_path(index, "sample-data")

    result = run_smoke(
        tmp_path / "sample.db",
        "sample-data/queries.jsonl",
        k=5,
        model_name=DEFAULT_MODEL,
    )
    assert result["cases"] >= 24
    assert result["hit_at_3"] >= 0.80
    assert result["mrr_at_k"] >= 0.80
    assert result["no_answer_accuracy"] == 1.0
