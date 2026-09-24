"""T-05 semantic search acceptance tests.

Model-dependent; skipped automatically when the [model] extra is absent
(e.g. light CI jobs). Both fixtures are module-scoped so MiniLM loads once.
"""

import pytest

st = pytest.importorskip("sentence_transformers")  # noqa: F401 - gates the module

from onefind.embed import DEFAULT_MODEL, SentenceEmbedder  # noqa: E402
from onefind.errors import UsageError  # noqa: E402
from onefind.ingest import ingest_path  # noqa: E402
from onefind.search import lexical_search, search, semantic_search  # noqa: E402
from onefind.store import Index  # noqa: E402


@pytest.fixture(scope="module")
def embedder():
    return SentenceEmbedder(DEFAULT_MODEL)


@pytest.fixture(scope="module")
def embedded_db(embedder, tmp_path_factory):
    db = tmp_path_factory.mktemp("t05") / "t05.db"
    with Index.open(db) as idx:
        idx.attach_embedder(embedder)
        ingest_path(idx, "sample-data")
    return db


def test_float_vectors_written_and_quant_calibration_present(embedded_db):
    # ADR-7: single native float table; int8/binary computed app-side
    with Index.open(embedded_db) as idx:
        assert idx.count("vectors") == 20
        assert idx.get_meta("int8_scale") is not None
        assert idx.get_meta("model_dim") == "384"


def _ids(hits):
    return [h.doc_id for h in hits]


def test_paraphrase_query_finds_doc_without_keyword_overlap(embedded_db, embedder):
    # shares no content word with the photosynthesis document on purpose
    with Index.open(embedded_db) as idx:
        idx.attach_embedder(embedder)
        hits = semantic_search(idx, "how plants turn sunlight into food", k=4)
    assert "photosynthesis.md" in _ids(hits)[:3]


@pytest.mark.parametrize("precision", ["float", "int8", "binary"])
def test_all_precisions_return_rankings(embedded_db, embedder, precision):
    with Index.open(embedded_db) as idx:
        idx.attach_embedder(embedder)
        hits = semantic_search(idx, "bread starter", k=4, precision=precision)
    assert len(hits) == 4
    assert "sourdough.md" in _ids(hits)[:2]


def test_precisions_overlap_but_may_differ(embedded_db, embedder):
    with Index.open(embedded_db) as idx:
        idx.attach_embedder(embedder)
        f = _ids(semantic_search(idx, "vitamin", k=4, precision="float"))
        b = _ids(semantic_search(idx, "vitamin", k=4, precision="binary"))
    assert set(f) & set(b), "quantized ranking should overlap full precision"


def test_semantic_scores_are_ordered_high_to_low(embedded_db, embedder):
    with Index.open(embedded_db) as idx:
        idx.attach_embedder(embedder)
        hits = semantic_search(idx, "vitamin", k=4, precision="float")
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)


def test_lexical_only_index_rejects_semantic(tmp_path):
    db = tmp_path / "lexonly.db"
    with Index.open(db) as idx:
        ingest_path(idx, "sample-data")
        with pytest.raises(UsageError):
            search(idx, "anything", mode="semantic")


def test_mode_dispatcher_routes_hybrid_since_phase_3(embedded_db, embedder):
    # hybrid was a guarded stub through Phase 2; it must work end-to-end now
    with Index.open(embedded_db) as idx:
        idx.attach_embedder(embedder)
        hits = search(idx, "vitamin", mode="hybrid", k=3)
    assert len(hits) == 3
    assert all(h.score > 0 for h in hits)


def test_determinism_across_runs(embedded_db, embedder):
    def one_round():
        with Index.open(embedded_db) as idx:
            idx.attach_embedder(embedder)
            return _ids(lexical_search(idx.conn, "vitamin")) + _ids(
                semantic_search(idx, "vitamin", k=4, precision="int8")
            )

    assert one_round() == one_round()
