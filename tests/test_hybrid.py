"""T-06 acceptance: RRF fusion math, hybrid behavior, rerank stability."""

import pytest

st = pytest.importorskip("sentence_transformers")  # noqa: F401 - gates model tests

from scrydb.embed import DEFAULT_MODEL, SentenceEmbedder  # noqa: E402
from scrydb.errors import UsageError  # noqa: E402
from scrydb.ingest import ingest_path  # noqa: E402
from scrydb.search import (  # noqa: E402
    hybrid_search,
    lexical_search,
    rrf_fuse,
    search,
    semantic_search,
)
from scrydb.store import Index  # noqa: E402


# ---- pure RRF math (no model) -------------------------------------------------


def test_rrf_known_scores_and_tie_break():
    fused = rrf_fuse([["a", "b", "c"], ["b", "a", "d"]], k=60)
    ids = [doc_id for doc_id, _score in fused]
    # a and b share the top combined score -> deterministic doc_id tie-break
    assert ids == ["a", "b", "c", "d"]
    by_id = dict(fused)
    assert by_id["a"] == by_id["b"]
    assert by_id["a"] == pytest.approx(1 / 61 + 1 / 62)
    # c and d each appear in exactly one leg at rank 3
    assert by_id["c"] == pytest.approx(1 / 63)
    assert by_id["d"] == by_id["c"]


def test_rrf_docs_in_both_rankings_float_up():
    fused = rrf_fuse([["x", "m"], ["n", "m"]], k=60)
    assert fused[0][0] == "m"


def test_rrf_handles_empty_legs():
    assert rrf_fuse([], k=60) == []
    assert [d for d, _ in rrf_fuse([[], ["only"]])] == ["only"]


# ---- hybrid with real embeddings ---------------------------------------------

CURATED = [  # (query, expected top doc)
    ("chlorophyll", "photosynthesis"),
    ("how plants turn sunlight into food", "photosynthesis"),
    ("vitamin C in oranges", "citrus-vitamin-c"),
    ("nutrients that help absorb iron", "citrus-vitamin-c"),
    ("foam midsole cushioning", "running-shoes"),
    ("shoes that return energy to runners", "running-shoes"),
    ("wild yeast starter culture", "sourdough"),
    ("bread that rises naturally tangy flavor", "sourdough"),
    ("glucose oxygen chloroplasts", "photosynthesis"),
    ("carbon fiber plates racing", "running-shoes"),
]


@pytest.fixture(scope="module")
def embedder():
    return SentenceEmbedder(DEFAULT_MODEL)


@pytest.fixture(scope="module")
def embedded_db(embedder, tmp_path_factory):
    db = tmp_path_factory.mktemp("t06") / "t06.db"
    with Index.open(db) as idx:
        idx.attach_embedder(embedder)
        ingest_path(idx, "sample-data")
    return db


def _hybrid(db, embedder, query, **kw):
    with Index.open(db) as idx:
        idx.attach_embedder(embedder)
        return hybrid_search(idx, query, k=4, **kw)


@pytest.mark.parametrize("precision", ["float", "int8", "binary"])
def test_hybrid_returns_k_results_all_precisions(embedded_db, embedder, precision):
    hits = _hybrid(embedded_db, embedder, "fermentation", precision=precision)
    assert len(hits) == 4
    assert all(h.score > 0 for h in hits)  # RRF scores are strictly positive


def test_hybrid_deterministic_across_runs(embedded_db, embedder):
    first = [(h.doc_id, round(h.score, 9)) for h in _hybrid(embedded_db, embedder, "vitamin")]
    second = [(h.doc_id, round(h.score, 9)) for h in _hybrid(embedded_db, embedder, "vitamin")]
    assert first == second


def test_rerank_never_lowers_result_count(embedded_db, embedder):
    base = _hybrid(embedded_db, embedder, "running shoes")
    reranked = _hybrid(embedded_db, embedder, "running shoes", rerank=True)
    assert len(reranked) == len(base) == 4


def test_rerank_orders_by_true_cosine(embedded_db, embedder):
    """Top reranked hit must be the candidate most similar to the query."""
    import numpy as np

    query = "energy return midsole"
    hits = _hybrid(embedded_db, embedder, query, rerank=True)
    with Index.open(embedded_db) as idx:
        idx.attach_embedder(embedder)
        vecs = idx.vectors_for_docs([h.doc_id for h in hits])
        qv = embedder.encode_query(query)
        sims = {d: float(np.dot(v, qv)) for d, v in vecs.items()}

    best = max(sims, key=sims.get)
    assert hits[0].doc_id == best


def test_dispatcher_routes_hybrid_with_options(embedded_db, embedder):
    with Index.open(embedded_db) as idx:
        idx.attach_embedder(embedder)
        hits = search(idx, "starter", mode="hybrid", k=3, precision="int8",
                      rrf_k=60, rerank=True)
    assert len(hits) == 3


def test_hybrid_requires_embedded_index(tmp_path):
    db = tmp_path / "lex.db"
    with Index.open(db) as idx:
        ingest_path(idx, "sample-data")
        with pytest.raises(UsageError):
            hybrid_search(idx, "q")


def test_ablation_hybrid_beats_both_singles_on_5_of_10(embedded_db, embedder):
    """T-06 acceptance: hybrid >= each single mode on >=5/10 curated queries."""
    wins = 0

    def target_rr(hits, target):
        for rank, hit in enumerate(hits, start=1):
            if hit.doc_id == target:
                return 1.0 / rank
        return 0.0

    for query, target in CURATED:
        with Index.open(embedded_db) as idx:
            idx.attach_embedder(embedder)
            lex = lexical_search(idx.conn, query, k=4)
            sem = semantic_search(idx, query, k=4, precision="float")
            hyb = hybrid_search(idx, query, k=4)

        rr_lex, rr_sem, rr_hyb = (
            target_rr(lex, target),
            target_rr(sem, target),
            target_rr(hyb, target),
        )
        if rr_hyb >= rr_lex and rr_hyb >= rr_sem:
            wins += 1

    assert wins >= 5, f"hybrid won only {wins}/10 curated queries"
