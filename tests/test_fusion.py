"""Model-free fusion contracts required in every development install."""

import pytest

from onefind.errors import UsageError
from onefind.search import Hit, linear_fuse, rrf_fuse


def hit(doc_id: str, score: float) -> Hit:
    return Hit(doc_id=doc_id, title="", score=score, snippet="")


def test_rrf_exact_scores_ties_and_empty_legs():
    fused = rrf_fuse([["a", "b", "c"], ["b", "a", "d"]], k=60)
    assert [doc_id for doc_id, _score in fused] == ["a", "b", "c", "d"]
    assert fused[0][1] == pytest.approx(1 / 61 + 1 / 62)
    assert fused[1][1] == pytest.approx(1 / 61 + 1 / 62)
    assert rrf_fuse([[], ["only"]]) == [("only", pytest.approx(1 / 61))]


def test_rrf_rejects_invalid_constant():
    for value in (0, -1, True, 1.5):
        with pytest.raises(UsageError):
            rrf_fuse([["a"]], k=value)


def test_linear_fuse_endpoints_are_exact_selected_leg():
    lexical = [hit("a", 0.9), hit("b", 0.5)]
    semantic = [hit("c", 0.99), hit("a", 0.4)]
    assert linear_fuse(lexical, semantic, 0.0) == [("a", 0.9), ("b", 0.5)]
    assert linear_fuse(lexical, semantic, 1.0) == [("c", 0.99), ("a", 0.4)]


def test_linear_fuse_normalizes_and_blends_union():
    lexical = [hit("a", 1.0), hit("b", 10.0)]
    semantic = [hit("a", 5.0), hit("b", 5.0), hit("c", 7.0)]
    fused = linear_fuse(lexical, semantic, 0.5)
    assert [doc_id for doc_id, _score in fused] == ["b", "c", "a"]
    assert dict(fused)["b"] == pytest.approx(0.5)
    assert dict(fused)["a"] == pytest.approx(0.0)
    assert dict(fused)["c"] == pytest.approx(0.5)


def test_linear_fuse_ties_are_doc_id_deterministic():
    fused = linear_fuse(
        [hit("z", 1.0), hit("a", 1.0)],
        [hit("m", 1.0)],
        alpha=0.5,
    )
    assert [doc_id for doc_id, _score in fused] == ["a", "m", "z"]
