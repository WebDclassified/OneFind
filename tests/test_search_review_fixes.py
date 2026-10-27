"""Regression tests for search review fixes (v1.2 product polish).

1. Vector block scans read one WAL snapshot, so a commit by another
   connection mid-scan no longer fails filtered / int8 / binary searches.
2. Hybrid RRF ties are broken by semantic rank before doc_id.
3. lexical_match="auto" uses an all-*keywords* strict pass (stopwords
   dropped), so strict_count / relaxed describe keyword coverage.
6. Binary-precision confidence labels use the float cosine of the bounded
   result set, so off-topic queries abstain like float/int8.
"""

import math

import numpy as np
import pytest

import onefind.search as search_module
from onefind.search import (
    Hit,
    _can_relax,
    all_keywords_fts_query,
    any_terms_fts_query,
    lexical_search,
    rrf_fuse,
    sanitize_fts_query,
    search,
    search_detailed,
    semantic_confidence,
    semantic_search,
)
from onefind.store import Document, Index


def _ids(hits):
    return [hit.doc_id for hit in hits]


def _fts_statements(index, action):
    statements = []
    index.conn.set_trace_callback(statements.append)
    try:
        action()
    finally:
        index.conn.set_trace_callback(None)
    return [sql for sql in statements if "MATCH" in sql and "bm25" in sql]


# ---- 1. one snapshot per vector scan ------------------------------------------------


class AxisEmbedder:
    name = "axis-fake"
    revision = "axis-1"
    dimension = 4

    def _vector(self, text):
        lowered = text.lower()
        if "orbit" in lowered:
            vector = [1.0, 0.1, 0.0, 0.0]
        elif "water" in lowered:
            vector = [0.0, 1.0, 0.1, 0.0]
        else:
            vector = [0.0, 0.0, 0.1, 1.0]
        vector = np.asarray(vector, dtype=np.float32)
        return vector / np.linalg.norm(vector)

    def encode_documents(self, texts):
        return np.stack([self._vector(text) for text in texts])

    def encode_query(self, text):
        return self._vector(text)


SCAN_CORPUS = [
    Document(f"notes/orbit{n}.md", f"Orbit note number {n}.", f"Orbit {n}", ext=".md", folder="notes")
    for n in range(6)
] + [
    Document("garden/water.md", "Water the garden.", "Water", ext=".md", folder="garden"),
    Document("misc/other.txt", "Something else entirely.", "Other", ext=".txt", folder="misc"),
]


@pytest.fixture()
def scan_db(tmp_path):
    db = tmp_path / "scan.db"
    with Index.open(db) as idx:
        idx.attach_embedder(AxisEmbedder())
        idx.add_documents(SCAN_CORPUS)
    return db


@pytest.fixture()
def reader_and_writer(scan_db):
    reader = Index.open(scan_db)
    reader.attach_embedder(AxisEmbedder())
    writer = Index.open(scan_db)
    try:
        yield reader, writer
    finally:
        writer.close()
        reader.close()


def _commit_during_metadata(monkeypatch, writer):
    """Make every metadata lookup between blocks race a commit on another connection."""
    original = search_module._metadata_for_rows
    commits = []

    def racing(index, row_ids):
        writer.record_search(f"concurrent {len(commits)}", "lexical", None, None, 0)
        commits.append(row_ids)
        return original(index, row_ids)

    monkeypatch.setattr(search_module, "_metadata_for_rows", racing)
    return commits


@pytest.mark.parametrize("precision", ["float", "int8", "binary"])
def test_filtered_block_scan_survives_concurrent_commit(reader_and_writer, monkeypatch, precision):
    reader, writer = reader_and_writer
    commits = _commit_during_metadata(monkeypatch, writer)
    vector = reader.embedder.encode_query("orbit")
    filters = search_module.SearchFilters.from_dict({"exts": [".md"]})
    hits = search_module._application_side_search(
        reader, vector, 3, precision,
        blocks=search_module._vector_blocks(reader, filters, block_size=1),
    )
    assert len(commits) >= 2  # commits really happened between blocks
    assert len(hits) == 3 and all(hit.doc_id.startswith("notes/orbit") for hit in hits)
    assert reader.conn.in_transaction is False


def test_filtered_semantic_search_survives_concurrent_commit(reader_and_writer, monkeypatch):
    reader, writer = reader_and_writer
    commits = _commit_during_metadata(monkeypatch, writer)
    hits = semantic_search(reader, "orbit", k=3, filters={"exts": [".md"]})
    assert commits and _ids(hits)[0].startswith("notes/orbit")
    assert reader.conn.in_transaction is False
    # The writer's rows landed and are visible once the scan's snapshot ended.
    history = reader.conn.execute("SELECT COUNT(*) FROM search_history").fetchone()[0]
    assert history == len(commits)


@pytest.mark.parametrize("precision", ["int8", "binary"])
def test_unfiltered_quantized_search_survives_concurrent_commit(reader_and_writer, monkeypatch,
                                                                precision):
    reader, writer = reader_and_writer
    commits = _commit_during_metadata(monkeypatch, writer)
    outcome = search_detailed(reader, "orbit", mode="semantic", k=3, precision=precision)
    assert commits and len(outcome.hits) == 3
    assert reader.conn.in_transaction is False


def test_block_scan_transaction_ends_on_early_close(reader_and_writer):
    reader, _writer = reader_and_writer
    blocks = search_module._vector_blocks(reader, block_size=1)
    next(blocks)
    assert reader.conn.in_transaction is True
    blocks.close()
    assert reader.conn.in_transaction is False


def test_block_scan_transaction_ends_when_ranking_fails(reader_and_writer, monkeypatch):
    reader, _writer = reader_and_writer

    def broken(_index, _row_ids):
        raise RuntimeError("metadata lookup failed")

    monkeypatch.setattr(search_module, "_metadata_for_rows", broken)
    with pytest.raises(RuntimeError):
        search_module._application_side_search(
            reader, reader.embedder.encode_query("orbit"), 3, "binary",
            blocks=search_module._vector_blocks(reader, block_size=1),
        )
    assert reader.conn.in_transaction is False


def test_block_scan_leaves_a_caller_transaction_open(reader_and_writer):
    reader, _writer = reader_and_writer
    reader.conn.execute("BEGIN")
    try:
        assert list(search_module._vector_blocks(reader, block_size=2))
        assert reader.conn.in_transaction is True
    finally:
        reader.conn.rollback()


# ---- 2. hybrid RRF ties go to the better semantic rank --------------------------------


def test_rrf_prefer_breaks_exact_ties_only():
    assert [doc for doc, _ in rrf_fuse([["a", "b"], ["b", "a"]])] == ["a", "b"]
    preferred = rrf_fuse([["a", "b"], ["b", "a"]], prefer=["b", "a"])
    assert [doc for doc, _ in preferred] == ["b", "a"]
    assert preferred[0][1] == preferred[1][1]
    # prefer never overrides a better fused score, and docs missing from it
    # fall back to doc_id order after the preferred ones.
    fused = rrf_fuse([["x", "m", "c"], ["m", "d"]], prefer=["m", "d"])
    assert [doc for doc, _ in fused] == ["m", "x", "d", "c"]
    assert rrf_fuse([["a"], ["b"]], prefer=[]) == rrf_fuse([["a"], ["b"]])
    with pytest.raises(search_module.UsageError):
        rrf_fuse([["a"]], k=0, prefer=["a"])


class SunEmbedder:
    """Query and photosynthesis point one way; compost is only loosely related."""

    name = "sun-fake"
    revision = "sun-1"
    dimension = 3

    def _vector(self, text):
        lowered = text.lower()
        if "compost" in lowered:
            vector = [0.3, 1.0, 0.0]
        elif "photosynthesis" in lowered or "sunlight" in lowered:
            vector = [1.0, 0.0, 0.0]
        else:
            vector = [0.0, 0.0, 1.0]
        vector = np.asarray(vector, dtype=np.float32)
        return vector / np.linalg.norm(vector)

    def encode_documents(self, texts):
        return np.stack([self._vector(text) for text in texts])

    def encode_query(self, text):
        return self._vector(text)


@pytest.fixture()
def sun_index(tmp_path):
    with Index.open(tmp_path / "sun.db") as idx:
        idx.attach_embedder(SunEmbedder())
        idx.add_documents([
            # Lexical #1 (both keywords), semantic #2.
            Document("environment/compost.md",
                     "Compost heaps release energy; sunlight warms the compost heap energy.",
                     "Compost"),
            # Lexical #2 (one keyword), semantic #1.
            Document("photosynthesis.md", "Photosynthesis: plants capture sunlight.",
                     "Photosynthesis"),
            Document("other.md", "Unrelated note about trains.", "Trains"),
        ])
        yield idx


def test_hybrid_rank_swap_tie_prefers_semantic_rank(sun_index):
    outcome = search_detailed(sun_index, "sunlight energy", mode="hybrid", k=2,
                              lexical_match="auto", explain=True)
    by_id = {hit.doc_id: hit.explain for hit in outcome.hits}
    compost, photo = by_id["environment/compost.md"], by_id["photosynthesis.md"]
    # The exact two-leg rank swap: identical fused scores.
    assert (compost["lexical_rank"], compost["semantic_rank"]) == (1, 2)
    assert (photo["lexical_rank"], photo["semantic_rank"]) == (2, 1)
    assert compost["fused_score"] == photo["fused_score"]
    # Path order would put compost first; the semantic rank decides instead.
    assert _ids(outcome.hits) == ["photosynthesis.md", "environment/compost.md"]


# ---- 3. auto strict pass ANDs keywords, not stopwords ---------------------------------


def test_all_keywords_query_drops_stopwords_like_the_any_query():
    assert all_keywords_fts_query("what is wild yeast") == '"wild" "yeast"'
    assert any_terms_fts_query("what is wild yeast") == '"wild" OR "yeast"'
    assert all_keywords_fts_query("what is this") == '"what" "is" "this"'
    assert all_keywords_fts_query("Orbit orbit ???") == '"Orbit"'
    assert all_keywords_fts_query("") == ""
    # FTS operators stay quoted text; operator words that are stopwords drop out.
    assert all_keywords_fts_query('NEAR(orbit "AND" water*)') == '"NEAR(orbit" "water*)"'
    assert all_keywords_fts_query("orbit* -water") == '"orbit*" "-water"'
    # The historical all-words query is unchanged.
    assert sanitize_fts_query("what is wild yeast") == '"what" "is" "wild" "yeast"'
    assert _can_relax("what is wild yeast") and not _can_relax("the orbit")
    assert not _can_relax("orbit") and not _can_relax("???")


KEYWORD_CORPUS = [
    Document("baking/sourdough.md", "Sourdough relies on wild yeast and lactic bacteria.",
             "Sourdough"),
    Document("materials/rust.md",
             "Galvanic corrosion occurs when two different metals touch in water.",
             "Rust prevention"),
    Document("baking/yeast.md", "Commercial yeast is dried and sold in packets.", "Yeast"),
    Document("misc/wild.md", "Notes on wild flowers.", "Wild flowers"),
]


@pytest.fixture()
def keyword_index(tmp_path):
    with Index.open(tmp_path / "kw.db") as idx:
        idx.add_documents(KEYWORD_CORPUS)
        yield idx


def test_auto_strict_pass_counts_full_keyword_matches(keyword_index):
    outcome = search_detailed(keyword_index, "what is wild yeast", mode="lexical", k=1,
                              explain=True, lexical_match="auto")
    assert outcome.strict_count == 1 and outcome.relaxed is False
    assert _ids(outcome.hits) == ["baking/sourdough.md"]
    assert outcome.hits[0].explain["confidence"] == "strong"
    assert outcome.hits[0].explain["reasons"] == ["Matched all 2 keywords"]

    # With room for more, partial matches still top up below the strict hit.
    wide = search_detailed(keyword_index, "what is wild yeast", mode="lexical", k=10,
                           lexical_match="auto")
    assert wide.strict_count == 1 and wide.relaxed is True
    assert _ids(wide.hits)[0] == "baking/sourdough.md"
    assert set(_ids(wide.hits[1:])) == {"baking/yeast.md", "misc/wild.md"}

    corrosion = search_detailed(keyword_index, "how does galvanic corrosion happen",
                                mode="lexical", k=10, explain=True, lexical_match="auto")
    # "happen" is a keyword the note lacks, so the strict pass finds nothing,
    # but stopwords ("how", "does") no longer count against it.
    assert corrosion.strict_count == 0 and corrosion.relaxed is True
    assert _ids(corrosion.hits) == ["materials/rust.md"]
    galvanic = search_detailed(keyword_index, "how does galvanic corrosion occur",
                               mode="lexical", k=10, explain=True, lexical_match="auto")
    assert galvanic.strict_count == 1 and galvanic.relaxed is False
    assert galvanic.hits[0].explain["confidence"] == "strong"


def test_auto_strict_pass_in_hybrid_matches_lexical(tmp_path):
    with Index.open(tmp_path / "kwh.db") as idx:
        idx.attach_embedder(AxisEmbedder())
        idx.add_documents(KEYWORD_CORPUS)
        lexical = search_detailed(idx, "what is wild yeast", mode="lexical", k=4,
                                  lexical_match="auto")
        hybrid = search_detailed(idx, "what is wild yeast", mode="hybrid", k=4,
                                 lexical_match="auto", explain=True)
        assert hybrid.strict_count == lexical.strict_count == 1
        assert hybrid.relaxed == lexical.relaxed is True
        sourdough = {hit.doc_id: hit.explain for hit in hybrid.hits}["baking/sourdough.md"]
        assert sourdough["lexical_rank"] == 1


def test_all_mode_is_unchanged_by_keyword_strict_pass(keyword_index):
    assert lexical_search(keyword_index.conn, "what is wild yeast", k=10) == []
    outcome = search_detailed(keyword_index, "what is wild yeast", mode="lexical", k=10)
    assert outcome.hits == [] and outcome.strict_count == 0 and outcome.relaxed is False
    statements = _fts_statements(
        keyword_index, lambda: lexical_search(keyword_index.conn, "what is wild yeast", k=10)
    )
    assert len(statements) == 1


def test_auto_strict_pass_sql_uses_keywords_only(keyword_index):
    params = []
    keyword_index.conn.set_trace_callback(params.append)
    try:
        lexical_search(keyword_index.conn, "what is wild yeast", k=1, lexical_match="auto")
    finally:
        keyword_index.conn.set_trace_callback(None)
    matches = [sql for sql in params if "MATCH" in sql and "bm25" in sql]
    assert len(matches) == 1  # k satisfied by the keyword AND: no OR query
    assert "'\"wild\" \"yeast\"'" in matches[0]


# ---- 6. binary confidence uses the float cosine of the result set ----------------------


class SlantEmbedder:
    """Documents whose binary sign pattern agrees with the query far more than
    their float cosine does, so binary similarity alone overstates relevance."""

    name = "slant-fake"
    revision = "slant-1"
    dimension = 8

    QUERY = np.array([1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0], dtype=np.float32)

    def _vector(self, text):
        if "offtopic" in text.lower():
            # Same sign as the query in 7 of 8 dims (binary sim 0.75), but the
            # mass sits on the one disagreeing axis: float cosine is negative.
            vector = np.array([0.05] * 7 + [-1.0], dtype=np.float32)
        elif "ontopic" in text.lower():
            vector = np.array([1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 0.9, 0.8], dtype=np.float32)
        else:
            vector = -self.QUERY
        return vector / np.linalg.norm(vector)

    def encode_documents(self, texts):
        return np.stack([self._vector(text) for text in texts])

    def encode_query(self, text):
        if "query" in text:
            return self.QUERY / np.linalg.norm(self.QUERY)
        return self._vector(text)


def test_binary_confidence_uses_float_cosine(tmp_path):
    with Index.open(tmp_path / "slant.db") as idx:
        idx.attach_embedder(SlantEmbedder())
        idx.add_documents([Document("offtopic.md", "offtopic note", "Off")])
        query_vector = idx.embedder.encode_query("query")
        doc_vector = idx.embedder._vector("offtopic")
        float_cos = float(np.dot(query_vector, doc_vector))
        assert float_cos < search_module.WEAK_SIM
        assert search_module.binary_similarity(1, 8) == 0.75

        for mode in ("semantic", "hybrid"):
            outcome = search_detailed(idx, "query", mode=mode, k=3, precision="binary",
                                      explain=True, lexical_match="auto")
            assert _ids(outcome.hits) == ["offtopic.md"], mode
            assert outcome.abstained is True, mode
            explain = outcome.hits[0].explain
            # The ranking score and hamming distance are still reported.
            assert explain["semantic_score"] == pytest.approx(0.75)
            assert explain["hamming_distance"] == 1
            assert explain["float_similarity"] == pytest.approx(float_cos, abs=1e-5)
            assert explain["confidence"] == "weak"

        idx.add_documents([Document("ontopic.md", "ontopic note", "On")])
        outcome = search_detailed(idx, "query", mode="semantic", k=3, precision="binary",
                                  explain=True)
        assert _ids(outcome.hits)[0] == "ontopic.md"
        assert outcome.abstained is False and outcome.confidences[0] == "strong"
        on_cos = outcome.hits[0].explain["float_similarity"]
        assert on_cos >= search_module.STRONG_SIM
        assert semantic_confidence(on_cos, "float") == "strong"


def test_binary_confidence_falls_back_without_float_vectors(tmp_path, monkeypatch):
    with Index.open(tmp_path / "slant2.db") as idx:
        idx.attach_embedder(SlantEmbedder())
        idx.add_documents([Document("offtopic.md", "offtopic note", "Off")])
        monkeypatch.setattr(idx, "vectors_for_docs", lambda _ids: {})
        outcome = search_detailed(idx, "query", mode="semantic", k=3, precision="binary",
                                  explain=True)
        explain = outcome.hits[0].explain
        assert explain["float_similarity"] is None
        # Binary similarity 0.75 keeps its own (binary-calibrated) label.
        assert explain["confidence"] == semantic_confidence(0.75, "binary") == "strong"
        assert math.isclose(explain["semantic_score"], 0.75)


def test_binary_explain_hits_keep_their_order(tmp_path):
    with Index.open(tmp_path / "slant3.db") as idx:
        idx.attach_embedder(SlantEmbedder())
        idx.add_documents([Document("offtopic.md", "offtopic note", "Off"),
                           Document("ontopic.md", "ontopic note", "On"),
                           Document("away.md", "away note", "Away")])
        plain = search(idx, "query", mode="semantic", k=3, precision="binary")
        explained = search(idx, "query", mode="semantic", k=3, precision="binary", explain=True)
        assert plain == explained and _ids(plain)[0] == "ontopic.md"
        assert isinstance(plain[0], Hit)
