"""T-10 extension: linear fusion math + alpha-sweep end-to-end."""

import json
import pytest

pytest.importorskip("ranx")
st = pytest.importorskip("sentence_transformers")  # noqa: F841

from scrydb.embed import DEFAULT_MODEL, SentenceEmbedder  # noqa: E402
from scrydb.errors import UsageError  # noqa: E402
from scrydb.evaluate import run_alpha_sweep  # noqa: E402
from scrydb.search import (  # noqa: E402
    Hit,
    hybrid_search,
    linear_fuse,
    rrf_fuse,
    search,
)
from scrydb.store import Index  # noqa: E402


# ---- pure linear_fuse math (no model) ----------------------------------------


def _h(doc_id: str, score: float) -> Hit:
    return Hit(doc_id=doc_id, title="", score=score, snippet="")


def test_linear_fuse_extremes_match_underlying_rankings():
    lex = [_h("a", 0.9), _h("b", 0.5)]
    sem = [_h("c", 0.99), _h("a", 0.40)]
    # candidates are the union; ranking is what changes with alpha
    fused = linear_fuse(lex, sem, alpha=0.0)
    assert [d for d, _ in fused] == ["a", "b", "c"]
    fused = linear_fuse(lex, sem, alpha=1.0)
    assert [d for d, _ in fused] == ["c", "a", "b"]


def test_linear_fuse_min_max_normalization_inside_pool():
    lex = [_h("a", 1.0), _h("b", 10.0)]  # normalized to a:0, b:1
    sem = [_h("a", 5.0), _h("b", 5.0)]   # tie -> both 1.0
    fused = linear_fuse(lex, sem, alpha=0.5)
    by_id = dict(fused)
    # a: 0.5*1 + 0.5*0 = 0.5 ; b: 0.5*1 + 0.5*1 = 1.0
    assert by_id["b"] == pytest.approx(1.0)
    assert by_id["a"] == pytest.approx(0.5)


def test_linear_fuse_deterministic():
    fused = linear_fuse(
        [_h("a", 0.9), _h("b", 0.5)],
        [_h("a", 0.4), _h("c", 0.7)],
        alpha=0.6,
    )
    again = linear_fuse(
        [_h("a", 0.9), _h("b", 0.5)],
        [_h("a", 0.4), _h("c", 0.7)],
        alpha=0.6,
    )
    assert [d for d, _ in fused] == [d for d, _ in again]


# ---- hybrid mode now accepts fusion/alpha + end-to-end sweep ------------------


@pytest.fixture(scope="module")
def embedder():
    return SentenceEmbedder(DEFAULT_MODEL)


@pytest.fixture(scope="module")
def embedded_db(embedder, tmp_path_factory):
    db = tmp_path_factory.mktemp("t10") / "t10.db"
    with Index.open(db) as idx:
        idx.attach_embedder(embedder)
        from scrydb.ingest import Document

        idx.add_documents([
            Document("d0", "oranges are citrus fruits rich in vitamin C", "Citrus"),
            Document("d1", "plants convert sunlight into glucose", "Photosynthesis"),
            Document("d2", "wild yeast starter leavens bread dough", "Sourdough"),
            Document("d3", "foam midsoles cushion and return energy", "Running"),
        ])
    return db


def _hit_ids(hits):
    return [h.doc_id for h in hits]


def test_hybrid_dispatches_to_linear_when_fusion_linear(embedded_db, embedder):
    with Index.open(embedded_db) as idx:
        idx.attach_embedder(embedder)
        rrf_hits = search(idx, "starter", mode="hybrid", k=4, fusion="rrf")
        lin_hits = search(idx, "starter", mode="hybrid", k=4, fusion="linear", alpha=0.5)
    # both must be sensible k-length rankings; the target doc (d2/sourdough)
    # should appear in the top-2 of either fusion on this small fixture
    assert len(rrf_hits) == len(lin_hits) == 4
    assert "d2" in _hit_ids(rrf_hits)[:2]
    assert "d2" in _hit_ids(lin_hits)[:2]


def test_hybrid_rejects_out_of_range_alpha(embedded_db, embedder):
    with Index.open(embedded_db) as idx:
        idx.attach_embedder(embedder)
        with pytest.raises(UsageError):
            hybrid_search(idx, "q", fusion="linear", alpha=1.2)


def test_sweep_writes_report_and_picks_best_alpha(embedded_db, embedder, tmp_path):
    # synthesize a tiny BEIR-style folder for the sweep loader
    folder = tmp_path / "fixture"
    folder.mkdir()
    (folder / "corpus.jsonl").write_text(
        "\n".join(
            json.dumps(r) for r in [
                {"_id": "d0", "title": "Citrus", "text": "vitamin C oranges"},
                {"_id": "d1", "title": "Photosynthesis", "text": "sunlight glucose"},
                {"_id": "d2", "title": "Sourdough", "text": "yeast starter bread"},
                {"_id": "d3", "title": "Running", "text": "foam midsoles energy"},
            ]
        ),
        encoding="utf-8",
    )
    (folder / "queries.jsonl").write_text(
        "\n".join(
            json.dumps(r) for r in [
                {"_id": "q1", "text": "vitamin"},
                {"_id": "q2", "text": "yeast"},
                {"_id": "q3", "text": "energy"},
            ]
        ),
        encoding="utf-8",
    )
    (folder / "qrels.tsv").write_text(
        "query-id\tcorpus-id\tscore\nq1\td0\t1\nq2\td2\t1\nq3\td3\t1\n",
        encoding="utf-8",
    )

    report = run_alpha_sweep(
        str(folder),
        db=embedded_db,
        alphas=[0.0, 0.5, 1.0],
        k=3,
        out_dir=tmp_path / "out",
    )
    text = report.read_text(encoding="utf-8")
    assert "Alpha sweep" in text
    assert "linear(α=0.0)" in text and "linear(α=1.0)" in text
    assert "RRF baseline" in text
    assert "Best alpha" in text
