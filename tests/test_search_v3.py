"""v1.2 retrieval: filters, explanations, confidence/abstention, timed stages.

Most tests use a deterministic 4-d fake embedder (no model download). The
section at the end re-checks calibration with the real MiniLM model and is
skipped when the [model] extra is absent.
"""

import datetime
import json
import math

import numpy as np
import pytest

import onefind.search as search_module
from onefind.errors import EmptyQueryError, UsageError
from onefind.search import (
    BINARY_STRONG_SIM,
    BINARY_WEAK_SIM,
    STRONG_SIM,
    WEAK_SIM,
    Hit,
    SearchFilters,
    SearchOutcome,
    binary_similarity,
    hybrid_search,
    lexical_confidence,
    lexical_search,
    query_terms,
    search,
    search_detailed,
    semantic_confidence,
    semantic_search,
)
from onefind.store import Document, Index


class AxisEmbedder:
    """Keyword -> axis vectors so cosine similarities are known exactly."""

    name = "axis-fake"
    revision = "axis-1"
    dimension = 4

    def __init__(self):
        self.query_calls = 0

    def _vector(self, text):
        lowered = text.lower()
        if "halfway" in lowered:  # cosine 0.4 with every orbit document
            vector = np.array([0.4, 0.0, 0.0, math.sqrt(1 - 0.16)], dtype=np.float32)
        elif "orbit" in lowered:
            vector = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        elif "water" in lowered:
            vector = np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)
        elif "money" in lowered:
            vector = np.array([0.0, 0.0, 1.0, 0.0], dtype=np.float32)
        else:
            vector = np.array([0.0, 0.0, 0.0, 1.0], dtype=np.float32)
        return vector / np.linalg.norm(vector)

    def encode_documents(self, texts):
        return np.stack([self._vector(text) for text in texts])

    def encode_query(self, text):
        self.query_calls += 1
        if text == "compost":  # keyword hits the water note, meaning points at orbits
            return self._vector("orbit")
        return self._vector(text)


T_JAN = datetime.datetime(2026, 1, 10, 12, 0).timestamp()
T_MAR = datetime.datetime(2026, 3, 5, 9, 30).timestamp()
T_JUN = datetime.datetime(2026, 6, 20, 18, 0).timestamp()

CORPUS = [
    Document("notes/orbit.md", "Orbit basics: an orbit is a curved path.", "Orbit basics",
             ext=".md", folder="notes", tags=["space"], mtime=T_JAN),
    Document("notes/deep/orbit2.txt", "Orbit insertion burns and orbit maths.", "Orbit burns",
             ext=".txt", folder="notes/deep", tags=["space", "work"], mtime=T_MAR),
    Document("notesextra/orbit3.md", "Orbit of the moon, another orbit note.", "Moon orbit",
             ext=".md", folder="notesextra", tags=["space"], mtime=T_JUN),
    Document("root-orbit.md", "Orbit at the root of the collection.", "Root orbit",
             ext=".md", folder="", mtime=T_JUN),
    Document("a_b/orbit4.md", "Orbit stored in an underscore folder.", "Underscore orbit",
             ext=".md", folder="a_b", mtime=T_MAR),
    Document("axb/orbit5.md", "Orbit stored in a lookalike folder.", "Lookalike orbit",
             ext=".md", folder="axb", mtime=T_MAR),
    Document("garden/water.md", "Water the compost weekly.", "Watering",
             ext=".md", folder="garden", tags=["home"], mtime=T_MAR),
    Document("finance/money.md", "Money saved in an emergency fund.", "Money",
             ext=".md", folder="finance", tags=["work"], mtime=T_JAN),
    Document("jsonl-orbit", "Orbit row imported from JSONL without file metadata.", "JSONL orbit"),
]


@pytest.fixture()
def index(tmp_path):
    embedder = AxisEmbedder()
    with Index.open(tmp_path / "v3.db") as idx:
        idx.attach_embedder(embedder)
        idx.add_documents(CORPUS)
        embedder.query_calls = 0
        yield idx


@pytest.fixture()
def lexical_index(tmp_path):
    with Index.open(tmp_path / "lex.db") as idx:
        idx.add_documents(CORPUS)
        yield idx


def _ids(hits):
    return [hit.doc_id for hit in hits]


# ---- SearchFilters -------------------------------------------------------------


def test_empty_filters():
    filters = SearchFilters()
    assert filters.is_empty()
    assert filters.to_dict() == {}
    assert SearchFilters.from_dict(None) == filters
    assert SearchFilters.from_dict({}) == filters
    assert SearchFilters.from_dict({"folders": None, "modified_after": ""}).is_empty()


def test_filters_normalize_values():
    filters = SearchFilters.from_dict(
        {
            "folders": ["notes/", "./a\\b//c", "", "/", "notes"],
            "exts": ["MD", ".TXT", "md", " .Rst "],
            "tags": [" Work ", "work", "HOME"],
        }
    )
    assert filters.folders == ("notes", "a/b/c", "")
    assert filters.exts == (".md", ".txt", ".rst")
    assert filters.tags == ("work", "home")
    assert not filters.is_empty()


def test_filters_accept_single_strings_and_direct_construction():
    assert SearchFilters.from_dict({"exts": "md"}).exts == (".md",)
    direct = SearchFilters(exts=["MD"], tags=("Space",))
    assert direct.exts == (".md",) and direct.tags == ("space",)
    assert hash(direct) == hash(SearchFilters(exts=(".md",), tags=("space",)))


def test_filters_round_trip_and_identity():
    filters = SearchFilters.from_dict(
        {"folders": ["notes"], "exts": [".md"], "tags": ["work"],
         "modified_after": 100.0, "modified_before": 200}
    )
    data = filters.to_dict()
    assert data == {"folders": ["notes"], "exts": [".md"], "tags": ["work"],
                    "modified_after": 100.0, "modified_before": 200.0}
    assert json.loads(json.dumps(data)) == data
    assert SearchFilters.from_dict(data) == filters
    assert SearchFilters.from_dict(filters) is filters


def test_filters_parse_iso_dates_as_local_days():
    filters = SearchFilters.from_dict(
        {"modified_after": "2026-03-01", "modified_before": "2026-03-31"}
    )
    assert filters.modified_after == datetime.datetime(2026, 3, 1).timestamp()
    end_of_day = datetime.datetime(2026, 4, 1).timestamp()
    assert end_of_day - 0.01 < filters.modified_before < end_of_day
    single_day = SearchFilters.from_dict(
        {"modified_after": "2026-03-05", "modified_before": "2026-03-05"}
    )
    assert single_day.modified_after <= T_MAR <= single_day.modified_before


def test_filters_parse_iso_datetimes_and_numbers():
    filters = SearchFilters.from_dict({"modified_after": "2026-01-02T00:00:00Z"})
    utc = datetime.datetime(2026, 1, 2, tzinfo=datetime.timezone.utc).timestamp()
    assert filters.modified_after == utc
    assert SearchFilters.from_dict({"modified_before": 5}).modified_before == 5.0


@pytest.mark.parametrize(
    "payload",
    [
        ["not", "a", "dict"],
        "folders=notes",
        {"folder": ["notes"]},  # unknown key (typo)
        {"folders": 3},
        {"folders": [1]},
        {"folders": ["x" * 600]},
        {"exts": ["." * 1]},
        {"exts": [".m d"]},
        {"exts": [""]},
        {"exts": ["a/b"]},
        {"tags": ["  "]},
        {"tags": [f"t{n}" for n in range(51)]},
        {"modified_after": True},
        {"modified_after": float("nan")},
        {"modified_after": float("inf")},
        {"modified_after": "yesterday"},
        {"modified_after": "2026-02-30"},
        {"modified_after": [2026]},
        {"modified_after": "2026-03-02", "modified_before": "2026-03-01"},
        {"modified_after": 10, "modified_before": 9},
    ],
)
def test_filters_reject_invalid_input(payload):
    with pytest.raises(UsageError):
        SearchFilters.from_dict(payload)


def test_filter_value_limit_is_inclusive():
    assert len(SearchFilters.from_dict({"tags": [f"t{n}" for n in range(50)]}).tags) == 50


def test_store_records_search_filters(index):
    filters = SearchFilters.from_dict({"tags": ["Work"]})
    index.record_search("orbit", "hybrid", "float", filters, 2)
    assert index.recent_searches()[0]["filters"] == {"tags": ["work"]}


# ---- lexical filters ---------------------------------------------------------------


def _lex(index, filters, query="orbit", k=20):
    return set(_ids(lexical_search(index.conn, query, k=k, filters=filters)))


ALL_ORBIT = {
    "notes/orbit.md", "notes/deep/orbit2.txt", "notesextra/orbit3.md", "root-orbit.md",
    "a_b/orbit4.md", "axb/orbit5.md", "jsonl-orbit",
}


def test_lexical_without_filters_is_unchanged(lexical_index):
    assert _lex(lexical_index, None) == ALL_ORBIT
    assert _lex(lexical_index, SearchFilters()) == ALL_ORBIT
    assert _lex(lexical_index, {}) == ALL_ORBIT


def test_lexical_folder_prefix_matches_subfolders_only(lexical_index):
    assert _lex(lexical_index, {"folders": ["notes"]}) == {"notes/orbit.md", "notes/deep/orbit2.txt"}
    assert _lex(lexical_index, {"folders": ["notes/deep"]}) == {"notes/deep/orbit2.txt"}
    assert _lex(lexical_index, {"folders": ["NOTES"]}) == {"notes/orbit.md", "notes/deep/orbit2.txt"}


def test_lexical_root_folder_and_like_escaping(lexical_index):
    assert _lex(lexical_index, {"folders": [""]}) == {"root-orbit.md"}
    assert _lex(lexical_index, {"folders": ["a_b"]}) == {"a_b/orbit4.md"}
    assert _lex(lexical_index, {"folders": ["a%"]}) == set()


def test_lexical_ext_tag_and_date_filters(lexical_index):
    assert _lex(lexical_index, {"exts": ["txt"]}) == {"notes/deep/orbit2.txt"}
    assert _lex(lexical_index, {"tags": ["work", "nothing"]}) == {"notes/deep/orbit2.txt"}
    assert _lex(lexical_index, {"tags": ["SPACE"]}) == {
        "notes/orbit.md", "notes/deep/orbit2.txt", "notesextra/orbit3.md",
    }
    assert _lex(lexical_index, {"modified_after": "2026-06-01"}) == {
        "notesextra/orbit3.md", "root-orbit.md",
    }
    assert _lex(lexical_index, {"modified_before": "2026-01-10"}) == {"notes/orbit.md"}
    assert _lex(
        lexical_index, {"modified_after": T_MAR, "modified_before": T_MAR}
    ) == {"notes/deep/orbit2.txt", "a_b/orbit4.md", "axb/orbit5.md"}


def test_lexical_filters_combine_with_and(lexical_index):
    assert _lex(lexical_index, {"tags": ["space"], "exts": [".md"], "folders": ["notes"]}) == {
        "notes/orbit.md"
    }


def test_rows_without_metadata_only_match_unfiltered(lexical_index):
    # JSONL rows (and rows migrated from v2) have NULL folder/ext/tags/mtime.
    for filters in ({"folders": [""]}, {"exts": [".md"]}, {"tags": ["space"]},
                    {"modified_after": 0}):
        assert "jsonl-orbit" not in _lex(lexical_index, filters)


def test_corrupt_tags_json_is_ignored(lexical_index):
    lexical_index.conn.execute(
        "UPDATE documents SET tags_json = '{not json' WHERE doc_id = 'notes/orbit.md'"
    )
    lexical_index.conn.commit()
    assert _lex(lexical_index, {"tags": ["space"]}) == {
        "notes/deep/orbit2.txt", "notesextra/orbit3.md",
    }


def test_lexical_filters_keep_k_and_order(lexical_index):
    unfiltered = lexical_search(lexical_index.conn, "orbit", k=20)
    filtered = lexical_search(lexical_index.conn, "orbit", k=2, filters={"exts": [".md"]})
    allowed = [hit.doc_id for hit in unfiltered if hit.doc_id.endswith(".md")]
    assert _ids(filtered) == allowed[:2]


def test_lexical_rejects_invalid_filters(lexical_index):
    with pytest.raises(UsageError):
        lexical_search(lexical_index.conn, "orbit", filters=["notes"])
    with pytest.raises(UsageError):
        lexical_search(lexical_index.conn, "orbit", filters={"folder": "notes"})


# ---- semantic filters ------------------------------------------------------------


@pytest.mark.parametrize("precision", ["float", "int8", "binary"])
def test_filtered_semantic_never_loses_restricted_results(index, precision):
    # The only allowed document is far from the query; a KNN top-1 followed
    # by post-filtering would return nothing.
    hits = semantic_search(index, "orbit", k=1, precision=precision, filters={"tags": ["home"]})
    assert _ids(hits) == ["garden/water.md"]


@pytest.mark.parametrize("precision", ["float", "int8", "binary"])
def test_filtered_semantic_respects_every_filter(index, precision):
    hits = semantic_search(index, "orbit", k=20, precision=precision,
                           filters={"folders": ["notes"], "exts": [".md", ".txt"]})
    assert set(_ids(hits)) == {"notes/orbit.md", "notes/deep/orbit2.txt"}
    assert semantic_search(index, "orbit", k=5, precision=precision,
                           filters={"tags": ["missing"]}) == []


def test_filtered_float_matches_native_ranking(index):
    native = semantic_search(index, "orbit", k=9)
    exact = semantic_search(index, "orbit", k=9, filters={"modified_after": -1e12})
    # jsonl-orbit has no mtime, so it is filtered out of the exact scan.
    expected = [hit for hit in native if hit.doc_id != "jsonl-orbit"]
    assert _ids(exact) == _ids(expected)[: len(exact)]
    for left, right in zip(exact, expected):
        assert left.score == pytest.approx(right.score, abs=1e-5)


def test_unfiltered_float_keeps_native_knn(index, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("exact scan must not run for unfiltered float search")

    monkeypatch.setattr(search_module, "_application_side_search", forbidden)
    assert semantic_search(index, "orbit", k=3)
    assert semantic_search(index, "orbit", k=3, filters=SearchFilters())  # empty = none


def test_filtered_float_never_uses_knn(index, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("filtered search must not post-filter a KNN top-k")

    monkeypatch.setattr(search_module, "_native_float_search", forbidden)
    assert semantic_search(index, "orbit", k=3, filters={"exts": [".md"]})


def test_filtered_blocks_are_bounded_and_complete(index):
    filters = SearchFilters.from_dict({"exts": [".md"]})
    blocks = list(search_module._vector_blocks(index, filters, block_size=2))
    assert blocks and all(len(row_ids) <= 2 for _matrix, row_ids in blocks)
    seen = [row_id for _matrix, row_ids in blocks for row_id in row_ids]
    expected = [
        int(row[0])
        for row in index.conn.execute(
            "SELECT c.row_id FROM chunks c JOIN documents d ON d.doc_id = c.doc_id "
            "WHERE d.ext = '.md' ORDER BY c.row_id"
        )
    ]
    assert seen == expected
    assert all(matrix.shape == (len(row_ids), 4) for matrix, row_ids in blocks)


def test_unfiltered_blocks_match_store_iterator(index):
    ours = list(search_module._vector_blocks(index, block_size=3))
    theirs = list(index.iter_vector_blocks(block_size=3))
    assert [ids for _m, ids in ours] == [ids for _m, ids in theirs]
    for (left, _), (right, _) in zip(ours, theirs):
        assert np.array_equal(left, right)
    for bad in (0, 10_001):
        with pytest.raises(UsageError):
            list(search_module._vector_blocks(index, block_size=bad))


def test_blocks_on_index_without_vectors_yield_nothing(lexical_index):
    assert list(search_module._vector_blocks(lexical_index)) == []


@pytest.mark.parametrize("precision", ["float", "int8", "binary"])
def test_block_size_does_not_change_ties_or_order(index, precision):
    vector = index.embedder.encode_query("orbit")
    filters = SearchFilters.from_dict({"modified_after": 0})
    small = search_module._application_side_search(
        index, vector, 3, precision,
        blocks=search_module._vector_blocks(index, filters, block_size=1),
    )
    large = search_module._application_side_search(
        index, vector, 3, precision,
        blocks=search_module._vector_blocks(index, filters, block_size=500),
    )
    assert [(hit.doc_id, hit.score) for hit in small] == [(hit.doc_id, hit.score) for hit in large]
    # Five orbit documents tie exactly; the doc_id tie-break must hold.
    assert _ids(small) == sorted(_ids(small))


def test_unfiltered_quantized_search_still_works(index):
    hits = semantic_search(index, "water", k=2, precision="int8")
    assert hits[0].doc_id == "garden/water.md"
    assert semantic_search(index, "water", k=2, precision="binary")[0].doc_id == "garden/water.md"


@pytest.mark.parametrize("precision", ["float", "int8", "binary"])
def test_hybrid_filters_apply_to_both_legs(index, precision):
    hits = hybrid_search(index, "orbit", k=10, precision=precision,
                         filters={"folders": ["notes"]})
    assert set(_ids(hits)) == {"notes/orbit.md", "notes/deep/orbit2.txt"}


# ---- Hit compatibility ---------------------------------------------------------


def test_hit_explain_is_optional_and_ignored_by_equality():
    plain = Hit("d", "t", 1.0, "s", "src")
    assert plain.explain is None
    explained = Hit("d", "t", 1.0, "s", "src", explain={"confidence": "strong"})
    assert plain == explained
    assert hash(plain) == hash(explained)
    assert "explain" not in repr(explained)
    assert Hit(doc_id="d", title="t", score=1.0, snippet="s") == Hit("d", "t", 1.0, "s")


def test_explain_off_leaves_hits_untouched(index):
    for mode in ("lexical", "semantic", "hybrid"):
        assert all(hit.explain is None for hit in search(index, "orbit", mode=mode, k=3))


# ---- explanations ---------------------------------------------------------------


def test_lexical_explain(index):
    hits = search(index, "orbit path", mode="lexical", k=5, explain=True)
    assert _ids(hits) == ["notes/orbit.md"]
    explain = hits[0].explain
    assert explain["mode"] == "lexical" and explain["precision"] is None
    assert explain["matched_terms"] == ["orbit", "path"]
    assert explain["missing_terms"] == []
    assert explain["lexical_rank"] == 1
    assert explain["lexical_score"] == pytest.approx(hits[0].score, abs=1e-5)
    assert explain["semantic_rank"] is None and explain["semantic_score"] is None
    assert explain["fused_score"] is None
    assert explain["confidence"] == "strong"
    assert explain["reasons"] == ["Matched all 2 keywords"]
    json.dumps(explain)  # JSON-safe


def test_matched_terms_use_porter_stemming(index):
    hits = search(index, "orbits", mode="lexical", k=1, explain=True)
    assert hits[0].explain["matched_terms"] == ["orbits"]
    assert hits[0].explain["reasons"] == ["Matched the keyword “orbits”"]


def test_hybrid_explain_both_legs_and_semantic_only(index):
    hits = search(index, "orbit zebra", mode="hybrid", k=10, explain=True)
    # Lexical AND finds nothing (no "zebra"), so every hit is semantic-only.
    assert hits
    for hit in hits:
        explain = hit.explain
        assert explain["lexical_rank"] is None
        assert explain["semantic_rank"] is not None
        assert explain["fusion"] == "rrf" and explain["reranked"] is False
        assert explain["fused_score"] == pytest.approx(hit.score, abs=1e-6)
        assert "Found by meaning search only" in explain["reasons"]
    top = hits[0].explain
    assert top["matched_terms"] == ["orbit"] and top["missing_terms"] == ["zebra"]
    assert "Matched 1 of 2 keywords" in top["reasons"]
    assert top["confidence"] == "strong"
    assert top["semantic_score"] == pytest.approx(1.0, abs=1e-5)

    both = search(index, "orbit", mode="hybrid", k=9, explain=True)
    in_both = [hit.explain for hit in both
               if hit.explain["lexical_rank"] and hit.explain["semantic_rank"]]
    assert len(in_both) == len(ALL_ORBIT)
    for explain in in_both:
        assert explain["confidence"] == "strong"
        assert "Found by both keyword and meaning search" in explain["reasons"]
        assert explain["reasons"][0] == "Matched the keyword “orbit”"


def test_hybrid_lexical_only_hit_takes_lexical_confidence(index):
    # The lexical leg finds only the water note; the semantic top-3 are orbits.
    hits = search(index, "compost", mode="hybrid", k=3, explain=True)
    by_id = {hit.doc_id: hit.explain for hit in hits}
    water = by_id["garden/water.md"]
    assert water["lexical_rank"] == 1 and water["semantic_rank"] is None
    assert water["semantic_score"] is None
    assert water["confidence"] == "strong"  # every keyword matched
    assert "Found by keyword search only" in water["reasons"]
    outcome = search_detailed(index, "compost", mode="hybrid", k=3)
    assert outcome.confidences == [by_id[hit.doc_id]["confidence"] for hit in outcome.hits]


def test_semantic_explain_and_quantized_reasons(index):
    hits = search(index, "halfway", mode="semantic", k=2, precision="int8", explain=True)
    explain = hits[0].explain
    assert explain["mode"] == "semantic" and explain["precision"] == "int8"
    assert explain["semantic_rank"] == 1
    assert explain["semantic_score"] == pytest.approx(0.4, abs=0.01)
    assert explain["confidence"] == "moderate"
    assert explain["lexical_rank"] is None and explain["fused_score"] is None
    assert "Compared using int8 vectors" in explain["reasons"]
    assert any(reason.startswith("Related in meaning (0.40)") for reason in explain["reasons"])


def test_binary_explain_reports_converted_similarity(index):
    hits = search(index, "orbit", mode="semantic", k=3, precision="binary", explain=True)
    for hit in hits:
        explain = hit.explain
        hamming = explain["hamming_distance"]
        assert hit.score == -hamming  # the Hit score keeps its legacy meaning
        assert explain["semantic_score"] == pytest.approx(binary_similarity(hamming, 4))
    assert hits[0].explain["semantic_score"] == pytest.approx(1.0)


def test_rerank_explain(index):
    hits = search(index, "orbit", mode="hybrid", k=2, rerank=True, candidate_depth=5,
                  explain=True)
    for hit in hits:
        assert hit.explain["reranked"] is True
        assert hit.explain["rerank_score"] == pytest.approx(hit.score, abs=1e-6)
        assert "Re-ranked by full-precision meaning similarity" in hit.explain["reasons"]


def test_term_matching_runs_in_one_query(index):
    statements = []
    index.conn.set_trace_callback(statements.append)
    try:
        search(index, "orbit insertion maths zebra", mode="hybrid", k=5, explain=True)
    finally:
        index.conn.set_trace_callback(None)
    fts_queries = [sql for sql in statements if "MATCH" in sql and "fts" in sql]
    # One lexical leg query plus one compound per-term query.
    assert len(fts_queries) == 2
    assert any("UNION ALL" in sql for sql in fts_queries)


def test_query_terms_are_unicode_words():
    assert query_terms('Rollback "owner" AND (owner) café_x 42') == [
        "rollback", "owner", "and", "café", "x", "42",
    ]
    assert query_terms("") == []
    assert len(query_terms(" ".join(f"w{n}" for n in range(100)))) == 32


# ---- confidence & abstention -------------------------------------------------------


def test_confidence_helpers():
    assert semantic_confidence(STRONG_SIM) == "strong"
    assert semantic_confidence(STRONG_SIM - 1e-9) == "moderate"
    assert semantic_confidence(WEAK_SIM) == "moderate"
    assert semantic_confidence(WEAK_SIM - 1e-9) == "weak"
    assert semantic_confidence(BINARY_STRONG_SIM, "binary") == "strong"
    assert semantic_confidence(BINARY_WEAK_SIM, "binary") == "moderate"
    assert semantic_confidence(BINARY_WEAK_SIM - 1e-9, "binary") == "weak"
    assert semantic_confidence(WEAK_SIM, "int8") == "moderate"
    assert 0 < WEAK_SIM < STRONG_SIM < 1 and 0 < BINARY_WEAK_SIM < BINARY_STRONG_SIM < 1
    assert lexical_confidence(2, 2) == "strong"
    assert lexical_confidence(1, 2) == "moderate"
    assert lexical_confidence(0, 2) == "weak"
    assert lexical_confidence(0, 0) == "moderate"
    assert binary_similarity(0, 384) == 1.0
    assert binary_similarity(384, 384) == -1.0
    assert binary_similarity(96, 384) == 0.5
    assert binary_similarity(3, 0) == 0.0


@pytest.mark.parametrize("mode", ["semantic", "hybrid"])
def test_off_topic_query_abstains_but_keeps_results(index, mode):
    outcome = search_detailed(index, "unrelated question", mode=mode, k=3)
    assert isinstance(outcome, SearchOutcome)
    assert len(outcome.hits) == 3
    assert outcome.abstained is True
    assert outcome.weak_count == 3
    assert outcome.confidences == ["weak"] * 3
    # Ranking is unchanged by confidence.
    assert _ids(outcome.hits) == _ids(search(index, "unrelated question", mode=mode, k=3))


def test_partial_confidence_does_not_abstain(index):
    outcome = search_detailed(index, "orbit", mode="semantic", k=9)
    assert outcome.confidences[0] == "strong"
    assert "weak" in outcome.confidences
    assert outcome.abstained is False
    assert outcome.weak_count == outcome.confidences.count("weak")


def test_lexical_results_are_confident_and_no_hits_never_abstain(index):
    outcome = search_detailed(index, "orbit", mode="lexical", k=3)
    assert outcome.confidences == ["strong"] * 3 and not outcome.abstained
    empty = search_detailed(index, "zebra", mode="lexical", k=3)
    assert empty.hits == [] and empty.abstained is False and empty.weak_count == 0


def test_detailed_matches_plain_search(index):
    for mode in ("lexical", "semantic", "hybrid"):
        detailed = search_detailed(index, "orbit water", mode=mode, k=4, explain=True)
        plain = search(index, "orbit water", mode=mode, k=4)
        assert detailed.hits == plain
        assert [hit.score for hit in detailed.hits] == [hit.score for hit in plain]
        assert [hit.explain["confidence"] for hit in detailed.hits] == detailed.confidences


def test_results_are_deterministic(index):
    first = search_detailed(index, "orbit", mode="hybrid", k=6, explain=True)
    second = search_detailed(index, "orbit", mode="hybrid", k=6, explain=True)
    assert [(h.doc_id, h.score, h.explain) for h in first.hits] == [
        (h.doc_id, h.score, h.explain) for h in second.hits
    ]


# ---- stages -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"mode": "lexical"}, ["parse", "lexical"]),
        ({"mode": "semantic"}, ["parse", "embed", "semantic"]),
        ({"mode": "hybrid"}, ["parse", "lexical", "embed", "semantic", "fuse"]),
        ({"mode": "hybrid", "rerank": True, "candidate_depth": 5},
         ["parse", "lexical", "embed", "semantic", "fuse", "rerank"]),
        ({"mode": "hybrid", "fusion": "linear"}, ["parse", "lexical", "embed", "semantic", "fuse"]),
    ],
)
def test_stage_events_in_order(index, kwargs, expected):
    events = []
    outcome = search_detailed(index, "orbit", k=3, on_stage=lambda *event: events.append(event),
                              **kwargs)
    assert [stage["name"] for stage in outcome.stages] == expected
    assert [(name, status) for name, status, _ms in events] == [
        pair for name in expected for pair in ((name, "start"), (name, "done"))
    ]
    for (name, status, ms) in events:
        assert ms >= 0 and (status == "done" or ms == 0.0)
    done = {name: ms for name, status, ms in events if status == "done"}
    for stage in outcome.stages:
        assert stage["ms"] >= 0
        assert stage["ms"] == pytest.approx(done[stage["name"]], abs=0.001)


def test_plain_search_reports_stages_too(index):
    names = []
    search(index, "orbit", mode="hybrid", k=2,
           on_stage=lambda name, status, _ms: names.append(name) if status == "done" else None)
    assert names == ["parse", "lexical", "embed", "semantic", "fuse"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mode": "semantic"},
        {"mode": "hybrid"},
        {"mode": "hybrid", "rerank": True, "candidate_depth": 5},
        {"mode": "hybrid", "explain": True, "filters": {"exts": [".md"]}},
    ],
)
def test_query_is_embedded_once(index, kwargs):
    index.embedder.query_calls = 0
    search_detailed(index, "orbit", k=3, **kwargs)
    assert index.embedder.query_calls == 1


def test_stage_callback_errors_propagate(index):
    def explode(name, status, _ms):
        if name == "lexical":
            raise RuntimeError("client went away")

    with pytest.raises(RuntimeError, match="client went away"):
        search(index, "orbit", mode="hybrid", on_stage=explode)


def test_failed_parse_emits_no_done_event(index):
    events = []
    with pytest.raises(EmptyQueryError):
        search_detailed(index, "   ", mode="lexical", on_stage=lambda *e: events.append(e))
    assert events == [("parse", "start", 0.0)]


# ---- validation / compatibility -----------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mode": "fuzzy"},
        {"k": 0},
        {"k": True},
        {"mode": "semantic", "precision": "fp16"},
        {"mode": "hybrid", "fusion": "max"},
        {"mode": "hybrid", "alpha": 1.5},
        {"mode": "hybrid", "rrf_k": 0},
        {"mode": "hybrid", "rrf_k": True},
        {"mode": "hybrid", "rerank": True, "candidate_depth": 2, "k": 5},
        {"filters": ["notes"]},
        {"filters": {"exts": [5]}},
    ],
)
def test_invalid_arguments_raise_usage_error(index, kwargs):
    with pytest.raises(UsageError):
        search_detailed(index, "orbit", **kwargs)


def test_lexical_mode_ignores_precision_like_before(lexical_index):
    assert search(lexical_index, "orbit", mode="lexical", precision="whatever", k=1)


def test_semantic_modes_require_embedder(lexical_index):
    for mode in ("semantic", "hybrid"):
        with pytest.raises(UsageError):
            search_detailed(lexical_index, "orbit", mode=mode)


def test_index_search_wrapper_passes_new_options(index):
    hits = index.search("orbit", mode="lexical", k=5, filters={"exts": [".txt"]}, explain=True)
    assert _ids(hits) == ["notes/deep/orbit2.txt"]
    assert hits[0].explain["confidence"] == "strong"


# ---- real model calibration (skipped without the [model] extra) ---------------------


OFF_TOPIC = [
    "how do I renew my passport",
    "quantum entanglement explained",
    "who won the world cup in 1998",
    "recipe for chicken curry",
]


@pytest.fixture(scope="module")
def sample_model_db(tmp_path_factory):
    pytest.importorskip("sentence_transformers")
    from onefind.embed import DEFAULT_MODEL, SentenceEmbedder
    from onefind.ingest import ingest_path

    embedder = SentenceEmbedder(DEFAULT_MODEL)
    db = tmp_path_factory.mktemp("v3model") / "sample.db"
    with Index.open(db) as idx:
        idx.attach_embedder(embedder)
        ingest_path(idx, "sample-data")
    return db, embedder


def _open_model(sample_model_db):
    db, embedder = sample_model_db
    idx = Index.open(db)
    idx.attach_embedder(embedder)
    return idx


def test_model_answerable_queries_never_abstain(sample_model_db):
    queries = [
        json.loads(line)
        for line in open("sample-data/queries.jsonl", encoding="utf-8")
        if line.strip()
    ]
    with _open_model(sample_model_db) as idx:
        for query in queries:
            if not query["relevant"]:
                continue
            for mode in ("semantic", "hybrid"):
                outcome = search_detailed(idx, query["query"], mode=mode, k=5, explain=True)
                assert not outcome.abstained, (mode, query["query"])
                by_id = {hit.doc_id: hit.explain["confidence"] for hit in outcome.hits}
                for relevant in query["relevant"]:
                    if relevant in by_id:
                        assert by_id[relevant] != "weak", (mode, query["query"], relevant)


@pytest.mark.parametrize("precision", ["float", "int8", "binary"])
def test_model_off_topic_queries_abstain(sample_model_db, precision):
    with _open_model(sample_model_db) as idx:
        for query in OFF_TOPIC:
            for mode in ("semantic", "hybrid"):
                outcome = search_detailed(idx, query, mode=mode, k=5, precision=precision)
                assert outcome.abstained, (mode, precision, query)
                assert outcome.weak_count == len(outcome.hits) == 5


def test_model_filtered_semantic_and_stages(sample_model_db):
    events = []
    with _open_model(sample_model_db) as idx:
        outcome = search_detailed(
            idx, "how plants turn sunlight into food", mode="hybrid", k=5,
            filters={"folders": ["health", "agriculture"]}, explain=True,
            on_stage=lambda name, status, _ms: events.append((name, status)),
        )
        folders = {
            doc["folder"] for doc in idx.get_documents([hit.doc_id for hit in outcome.hits]).values()
        }
    assert outcome.hits and folders <= {"health", "agriculture"}
    assert [name for name, status in events if status == "done"] == [
        "parse", "lexical", "embed", "semantic", "fuse",
    ]


def test_outcome_relaxation_fields_keep_strict_defaults(index):
    hybrid = search_detailed(index, "orbit", mode="hybrid", k=3)
    assert hybrid.relaxed is False and hybrid.strict_count == 3
    lexical = search_detailed(index, "orbit zebra", mode="lexical", k=3)
    assert lexical.hits == [] and lexical.relaxed is False and lexical.strict_count == 0
    semantic = search_detailed(index, "orbit", mode="semantic", k=3)
    assert semantic.relaxed is False and semantic.strict_count is None
    explain = search(index, "orbit", mode="hybrid", k=1, explain=True)[0].explain
    assert explain["lexical_match"] == "all" and explain["keyword_coverage"] == "all"
