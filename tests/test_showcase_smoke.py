"""Retrieval quality gate for the 120-document presentation corpus."""

import pytest

pytest.importorskip("sentence_transformers")

from onefind.embed import DEFAULT_MODEL
from onefind.evaluate import run_smoke
from onefind.ingest import ingest_path
from onefind.store import Index


def test_showcase_gold_queries_meet_presentation_quality_gate(tmp_path):
    database = tmp_path / "showcase.db"
    with Index.open(database) as index:
        ingest_path(index, "showcase-data")

    result = run_smoke(
        database,
        "showcase-data/queries.jsonl",
        k=5,
        model_name=DEFAULT_MODEL,
    )

    assert result["cases"] == 202
    assert result["answerable"] == 192
    assert result["no_answer"] == 10
    assert result["hit_at_3"] >= 0.90
    assert result["mrr_at_k"] >= 0.90
    assert result["no_answer_accuracy"] == 1.0
