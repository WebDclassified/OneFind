"""Search edge cases that do not require a downloaded language model."""

import numpy as np
import pytest

from onefind.errors import UsageError
from onefind.search import Hit, hybrid_search


class QueryEmbedder:
    def __init__(self):
        self.calls = 0

    def encode_query(self, _text):
        self.calls += 1
        return np.array([1.0, 0.0], dtype=np.float32)


class RerankIndex:
    def __init__(self):
        self.conn = None
        self.embedder = QueryEmbedder()
        self.pool = []

    def vectors_for_docs(self, doc_ids):
        self.pool = list(doc_ids)
        return {
            doc_id: np.array([1.0, 0.0] if doc_id.endswith("0") else [0.0, 1.0], dtype=np.float32)
            for doc_id in doc_ids
        }


def _hit(doc_id: str) -> Hit:
    return Hit(doc_id=doc_id, title=doc_id, score=1.0, snippet="")


def test_rerank_uses_complete_fused_pool_and_replaces_scores(monkeypatch):
    lexical = [_hit(f"l{i}") for i in range(4)]
    semantic = [_hit(f"s{i}") for i in range(4)]
    monkeypatch.setattr("onefind.search.lexical_search", lambda *_args, **_kwargs: lexical)
    monkeypatch.setattr("onefind.search.semantic_search", lambda *_args, **_kwargs: semantic)

    index = RerankIndex()
    hits = hybrid_search(
        index,
        "query",
        k=2,
        rerank=True,
        candidate_depth=4,
    )

    assert len(index.pool) == 8
    assert len(hits) == 2
    assert index.embedder.calls == 1
    assert {hit.score for hit in hits} == {1.0}


def test_rerank_candidate_depth_must_cover_result_count():
    index = RerankIndex()
    with pytest.raises(UsageError, match="candidate_depth"):
        hybrid_search(index, "query", k=10, rerank=True, candidate_depth=5)
