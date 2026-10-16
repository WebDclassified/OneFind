"""Search quality: stopwords and relaxed keyword matching (lexical_match).

Uses a deterministic keyword-axis fake embedder; the section at the end
re-checks the behaviour with the real MiniLM model and is skipped when the
[model] extra is absent.
"""

import json

import numpy as np
import pytest

from onefind.errors import UsageError
from onefind.search import (
    LEXICAL_MATCHES,
    STOPWORDS,
    Hit,
    SearchOutcome,
    _below,
    any_terms_fts_query,
    content_terms,
    hybrid_search,
    is_stopword,
    keyword_terms,
    lexical_confidence,
    lexical_search,
    query_terms,
    sanitize_fts_query,
    search,
    search_detailed,
)
from onefind.store import Document, Index


class KeywordEmbedder:
    """First matching keyword picks an axis, so similarities are 1.0 or 0.0."""

    name = "keyword-fake"
    revision = "keyword-1"
    dimension = 4
    AXES = ("orbit", "compost", "bread")

    def _vector(self, text):
        lowered = text.lower()
        vector = np.zeros(4, dtype=np.float32)
        for axis, word in enumerate(self.AXES):
            if word in lowered:
                vector[axis] = 1.0
                return vector
        vector[3] = 1.0
        return vector

    def encode_documents(self, texts):
        return np.stack([self._vector(text) for text in texts])

    def encode_query(self, text):
        return self._vector(text)


CORPUS = [
    Document("garden/soil.md", "Healthy soil holds water; compost improves soil structure and drainage.",
             "Soil", folder="garden", ext=".md"),
    Document("garden/compost.md", "Compost bins need water and air. Turn the compost pile weekly.",
             "Compost care", folder="garden", ext=".md"),
    Document("garden/water.md", "Water the tomatoes every morning.", "Watering",
             folder="garden", ext=".md"),
    Document("kitchen/bread.md", "Bake bread with water and a sourdough starter.", "Bread",
             folder="kitchen", ext=".md"),
    Document("space/orbit.md", "An orbit is a curved path around a planet.", "Orbit",
             folder="space", ext=".md"),
    Document("notes/filler.md", "The end of the list.", "Filler", folder="notes", ext=".md"),
]


@pytest.fixture()
def index(tmp_path):
    with Index.open(tmp_path / "quality.db") as idx:
        idx.attach_embedder(KeywordEmbedder())
        idx.add_documents(CORPUS)
        yield idx


@pytest.fixture()
def lexical_index(tmp_path):
    with Index.open(tmp_path / "lex.db") as idx:
        idx.add_documents(CORPUS)
        yield idx


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


# ---- stopwords -------------------------------------------------------------------


def test_stopword_set_is_compact_and_lowercase():
    assert 80 <= len(STOPWORDS) <= 200
    assert all(word == word.casefold() and word.isalpha() for word in STOPWORDS)
    for word in ("the", "how", "what", "is", "my", "do", "a"):
        assert word in STOPWORDS
    # Words that are often real content in notes stay searchable.
    for word in ("may", "will", "us", "won", "d", "c", "orbit"):
        assert word not in STOPWORDS


def test_is_stopword_handles_case_and_contractions():
    assert is_stopword("The") and is_stopword("WHAT'S") and is_stopword("don’t")
    assert not is_stopword("orbit") and not is_stopword("vitamin")
    assert not is_stopword("") and not is_stopword("'")
    assert not is_stopword("o'brien")


def test_content_and_keyword_terms():
    assert content_terms(["how", "does", "compost", "work"]) == ["compost", "work"]
    assert content_terms(["what", "is", "this"]) == ["what", "is", "this"]
    assert content_terms([]) == []
    assert keyword_terms("How does the compost pile work?") == ["compost", "pile", "work"]
    assert keyword_terms("What is THIS") == ["what", "is", "this"]  # only stopwords: keep
    assert keyword_terms("") == []
    # The limit applies after stopwords are removed.
    long_query = " ".join(["the"] * 40 + [f"w{n}" for n in range(40)])
    assert keyword_terms(long_query) == [f"w{n}" for n in range(32)]
    # query_terms itself is unchanged.
    assert query_terms("how does compost work") == ["how", "does", "compost", "work"]


def test_any_terms_fts_query():
    assert any_terms_fts_query("How does compost improve soil?") == (
        '"compost" OR "improve" OR "soil?"'
    )
    assert any_terms_fts_query("the the THE") == '"the"'
    assert any_terms_fts_query("Orbit orbit ORBIT") == '"Orbit"'
    assert any_terms_fts_query('what\'s "the" orbit') == '"orbit"'
    assert any_terms_fts_query("* ^ - ???") == ""
    assert any_terms_fts_query("") == ""
    # FTS operators stay literal: tokens are quoted, stopword operators dropped.
    assert any_terms_fts_query("NEAR(orbit AND water)") == '"NEAR(orbit" OR "water)"'
    assert any_terms_fts_query("AND OR NOT") == '"AND" OR "OR" OR "NOT"'
    assert sanitize_fts_query("orbit path") == '"orbit" "path"'  # AND path unchanged


# ---- lexical_match ---------------------------------------------------------------


def test_lexical_match_values_and_validation(index):
    assert LEXICAL_MATCHES == ("all", "any", "auto")
    for bad in ("some", "", None, "ALL", 1):
        with pytest.raises(UsageError, match="lexical_match"):
            lexical_search(index.conn, "orbit", lexical_match=bad)
        with pytest.raises(UsageError, match="lexical_match"):
            search_detailed(index, "orbit", mode="lexical", lexical_match=bad)
        with pytest.raises(UsageError, match="lexical_match"):
            search(index, "orbit", mode="semantic", lexical_match=bad)
        with pytest.raises(UsageError, match="lexical_match"):
            hybrid_search(index, "orbit", lexical_match=bad)


def test_default_is_all_and_unchanged(index):
    for query in ("compost water", "how does compost improve soil drainage", "orbit"):
        default = lexical_search(index.conn, query, k=10)
        explicit = lexical_search(index.conn, query, k=10, lexical_match="all")
        assert default == explicit
        assert [hit.score for hit in default] == [hit.score for hit in explicit]
        for mode in ("lexical", "hybrid"):
            plain = search(index, query, mode=mode, k=5, explain=True)
            strict = search(index, query, mode=mode, k=5, explain=True, lexical_match="all")
            assert plain == strict and [h.explain for h in plain] == [h.explain for h in strict]
    # A natural-language question finds nothing with AND semantics.
    assert lexical_search(index.conn, "how does compost improve soil drainage") == []


def test_any_matches_some_words_and_ranks_by_coverage(index):
    hits = lexical_search(index.conn, "compost water drainage", k=10, lexical_match="any")
    ids = _ids(hits)
    assert ids[0] == "garden/soil.md"  # all three words
    assert ids.index("garden/compost.md") < ids.index("garden/water.md")  # two words beat one
    assert set(ids) == {"garden/soil.md", "garden/compost.md", "garden/water.md",
                        "kitchen/bread.md"}
    assert [hit.score for hit in hits] == sorted((hit.score for hit in hits), reverse=True)
    question = lexical_search(index.conn, "how does compost improve soil drainage", k=10,
                              lexical_match="any")
    assert _ids(question)[0] == "garden/soil.md"


def test_any_ignores_stopwords_unless_query_is_only_stopwords(index):
    # "the" appears in several notes but must not pull them in on its own.
    hits = lexical_search(index.conn, "the orbit", k=10, lexical_match="any")
    assert _ids(hits) == ["space/orbit.md"]
    only_stop = lexical_search(index.conn, "the", k=10, lexical_match="any")
    assert set(_ids(only_stop)) == set(_ids(lexical_search(index.conn, "the", k=10)))
    assert only_stop
    assert lexical_search(index.conn, "???", k=10, lexical_match="any") == []


def test_auto_puts_all_word_hits_first_then_partial(index):
    strict = lexical_search(index.conn, "compost water drainage", k=10)
    assert _ids(strict) == ["garden/soil.md"]
    auto = lexical_search(index.conn, "compost water drainage", k=10, lexical_match="auto")
    assert auto[0] == strict[0] and auto[0].score == strict[0].score
    assert len(auto) == len(set(_ids(auto))) == 4
    assert set(_ids(auto[1:])) == {"garden/compost.md", "garden/water.md", "kitchen/bread.md"}
    # Partial-match scores are rescaled below the all-words hits but keep their order.
    any_order = [doc for doc in _ids(lexical_search(index.conn, "compost water drainage",
                                                     k=10, lexical_match="any"))
                 if doc != "garden/soil.md"]
    assert _ids(auto[1:]) == any_order
    scores = [hit.score for hit in auto]
    assert scores == sorted(scores, reverse=True) and scores[1] < scores[0]
    assert all(score > 0 for score in scores)


def test_auto_respects_k(index):
    auto = lexical_search(index.conn, "compost water drainage", k=2, lexical_match="auto")
    assert _ids(auto) == ["garden/soil.md", "garden/compost.md"]
    assert len(lexical_search(index.conn, "the orbit water", k=1, lexical_match="auto")) == 1


def test_auto_equals_all_when_enough_full_matches(index):
    strict = lexical_search(index.conn, "water", k=2)
    assert len(strict) == 2
    assert lexical_search(index.conn, "water", k=2, lexical_match="auto") == strict
    statements = _fts_statements(
        index, lambda: lexical_search(index.conn, "compost water", k=1, lexical_match="auto")
    )
    assert len(statements) == 1  # k satisfied by the AND query: no OR query


def test_auto_skips_the_or_query_when_it_cannot_add_rows(index):
    for query in ("orbit", "zebra", "the"):
        statements = _fts_statements(
            index, lambda q=query: lexical_search(index.conn, q, k=10, lexical_match="auto")
        )
        assert len(statements) == 1, query
    # "the" is a stopword, so "the orbit" has one keyword: the all-keywords
    # AND and the any-keywords OR are the same query, run once.
    statements = _fts_statements(
        index, lambda: lexical_search(index.conn, "the orbit", k=10, lexical_match="auto")
    )
    assert len(statements) == 1
    statements = _fts_statements(
        index, lambda: lexical_search(index.conn, "the orbit water", k=10, lexical_match="auto")
    )
    assert len(statements) == 2


def test_relaxed_modes_respect_filters(index):
    query = "compost water drainage"
    for match in ("any", "auto"):
        unfiltered = lexical_search(index.conn, query, k=10, lexical_match=match)
        assert "kitchen/bread.md" in _ids(unfiltered)
        filtered = lexical_search(index.conn, query, k=10, lexical_match=match,
                                  filters={"folders": ["garden"]})
        assert set(_ids(filtered)) == {"garden/soil.md", "garden/compost.md", "garden/water.md"}


def test_below_rescales_only_when_needed():
    hits = [Hit("a", "", 9.0, ""), Hit("b", "", 3.0, "")]
    assert _below(hits, 10.0) == hits and _below([], 1.0) == []
    scaled = _below(hits, 2.0)
    assert _ids(scaled) == ["a", "b"]
    assert scaled[0].score < 2.0 and scaled[1].score < scaled[0].score
    assert scaled[1].score / scaled[0].score == pytest.approx(3.0 / 9.0)
    shifted = _below([Hit("a", "", 0.5, ""), Hit("b", "", -1.0, "")], -0.25)
    assert shifted[0].score < -0.25 and shifted[1].score < shifted[0].score


# ---- outcome flags ------------------------------------------------------------------


def test_relaxed_flag_and_strict_count(index):
    question = "how does compost improve soil drainage"
    auto = search_detailed(index, question, mode="lexical", k=5, lexical_match="auto")
    assert isinstance(auto, SearchOutcome)
    # soil.md has every keyword ("how"/"does" are stopwords): strict hit,
    # topped up with partial matches.
    assert auto.relaxed is True and auto.strict_count == 1
    assert _ids(auto.hits)[0] == "garden/soil.md"
    none_strict = search_detailed(index, "compost orbit", mode="lexical", k=5,
                                  lexical_match="auto")
    assert none_strict.relaxed is True and none_strict.strict_count == 0

    strict = search_detailed(index, question, mode="lexical", k=5)
    assert strict.hits == [] and strict.relaxed is False and strict.strict_count == 0

    partial = search_detailed(index, "compost water drainage", mode="lexical", k=5,
                              lexical_match="auto")
    assert partial.relaxed is True and partial.strict_count == 1

    enough = search_detailed(index, "water", mode="lexical", k=2, lexical_match="auto")
    assert enough.relaxed is False and enough.strict_count == 2

    nothing_more = search_detailed(index, "orbit", mode="lexical", k=5, lexical_match="auto")
    assert nothing_more.relaxed is False and nothing_more.strict_count == 1

    explicit_any = search_detailed(index, question, mode="lexical", k=5, lexical_match="any")
    assert explicit_any.relaxed is False and explicit_any.strict_count is None

    semantic = search_detailed(index, question, mode="semantic", k=5, lexical_match="auto")
    assert semantic.relaxed is False and semantic.strict_count is None

    hybrid = search_detailed(index, question, mode="hybrid", k=5, lexical_match="auto")
    assert hybrid.relaxed is True and hybrid.strict_count == 1
    # The flags are reported without explain/confidence work too.
    plain = search(index, question, mode="lexical", k=5, lexical_match="auto")
    assert plain == auto.hits


# ---- explanations & confidence ------------------------------------------------------


def test_explain_ignores_stopwords(index):
    hits = search(index, "orbit is a path", mode="lexical", k=5, explain=True)
    assert _ids(hits) == ["space/orbit.md"]
    explain = hits[0].explain
    assert explain["matched_terms"] == ["orbit", "path"] and explain["missing_terms"] == []
    assert explain["reasons"] == ["Matched all 2 keywords"]
    assert explain["keyword_coverage"] == "all" and explain["lexical_match"] == "all"
    assert explain["confidence"] == "strong"

    only_stop = search(index, "the end", mode="lexical", k=5, explain=True)
    assert only_stop[0].explain["matched_terms"] == ["end"]
    stop_query = search(index, "of the", mode="lexical", k=5, explain=True)
    assert stop_query[0].explain["matched_terms"] == ["of", "the"]
    assert stop_query[0].explain["reasons"] == ["Matched all 2 keywords"]


def test_partial_matches_are_marked(index):
    hits = search(index, "compost water drainage", mode="lexical", k=10, explain=True,
                  lexical_match="auto")
    by_id = {hit.doc_id: hit.explain for hit in hits}
    soil = by_id["garden/soil.md"]
    assert soil["keyword_coverage"] == "all" and soil["confidence"] == "strong"
    assert soil["reasons"] == ["Matched all 3 keywords"]
    compost = by_id["garden/compost.md"]
    assert compost["matched_terms"] == ["compost", "water"]
    assert compost["missing_terms"] == ["drainage"]
    assert compost["keyword_coverage"] == "some" and compost["lexical_match"] == "auto"
    assert compost["reasons"] == ["Matched 2 of 3 keywords"]
    assert compost["confidence"] == "moderate"
    water = by_id["garden/water.md"]
    assert water["reasons"] == ["Matched 1 of 3 keywords"]
    assert water["confidence"] == "weak"  # one word of three: not enough on its own
    outcome = search_detailed(index, "compost water drainage", mode="lexical", k=10,
                              lexical_match="auto")
    assert outcome.abstained is False
    assert outcome.confidences == [by_id[doc]["confidence"] for doc in _ids(outcome.hits)]


def test_stopwords_do_not_push_a_full_keyword_match_into_the_relaxed_tier(index):
    # "the" is missing from the orbit note; it is a stopword, so the auto
    # strict pass (every keyword) still finds the note, with nothing to add.
    outcome = search_detailed(index, "the orbit path", mode="lexical", k=5, explain=True,
                              lexical_match="auto")
    assert outcome.relaxed is False and outcome.strict_count == 1
    # The historical all-words default still requires "the".
    assert search(index, "the orbit path", mode="lexical", k=5) == []
    explain = outcome.hits[0].explain
    assert outcome.hits[0].doc_id == "space/orbit.md"
    assert explain["matched_terms"] == ["orbit", "path"] and explain["confidence"] == "strong"
    assert explain["reasons"] == ["Matched all 2 keywords"]


def test_partial_lexical_confidence_helper():
    assert lexical_confidence(2, 3, partial=True) == "moderate"
    assert lexical_confidence(1, 2, partial=True) == "weak"
    assert lexical_confidence(1, 3, partial=True) == "weak"
    assert lexical_confidence(3, 3, partial=True) == "strong"
    assert lexical_confidence(0, 0, partial=True) == "moderate"
    assert lexical_confidence(1, 3) == "moderate"  # legacy behaviour unchanged


def test_semantic_explain_has_coverage_but_no_lexical_match(index):
    hits = search(index, "the compost", mode="semantic", k=2, explain=True,
                  lexical_match="auto")
    explain = hits[0].explain
    assert "lexical_match" not in explain
    assert explain["matched_terms"] == ["compost"] and explain["keyword_coverage"] == "all"


# ---- hybrid ---------------------------------------------------------------------------


def test_hybrid_auto_relaxes_the_lexical_leg_before_fusion(index):
    question = "how does compost improve soil drainage"
    strict = search(index, question, mode="hybrid", k=6, explain=True)
    assert all(hit.explain["lexical_rank"] is None for hit in strict)

    hits = search(index, question, mode="hybrid", k=6, explain=True, lexical_match="auto")
    by_id = {hit.doc_id: hit.explain for hit in hits}
    soil = by_id["garden/soil.md"]
    assert soil["lexical_rank"] == 1 and soil["semantic_rank"] is not None
    assert "Found by both keyword and meaning search" in soil["reasons"]
    assert soil["confidence"] == "strong"
    # Both garden notes tie in meaning and swap keyword/meaning ranks 1-2.
    assert set(_ids(hits[:2])) == {"garden/soil.md", "garden/compost.md"}
    assert by_id["garden/compost.md"]["lexical_rank"] == 2
    assert hybrid_search(index, question, k=6, lexical_match="auto") == hits


def test_hybrid_partial_hit_in_both_legs_takes_the_better_leg(index):
    hits = search(index, "compost water drainage", mode="hybrid", k=10, explain=True,
                  lexical_match="auto")
    by_id = {hit.doc_id: hit.explain for hit in hits}
    # water.md: 1 of 3 keywords (relaxed) and unrelated in meaning -> weak, even
    # though both legs returned it.
    water = by_id["garden/water.md"]
    assert water["lexical_rank"] is not None and water["semantic_rank"] is not None
    assert water["confidence"] == "weak"
    # compost.md: 2 of 3 keywords but very close in meaning -> strong.
    assert by_id["garden/compost.md"]["confidence"] == "strong"
    # soil.md contains every word: both legs -> strong, as before.
    assert by_id["garden/soil.md"]["confidence"] == "strong"

    # With "all", water.md is a semantic-only weak match.
    strict = {hit.doc_id: hit.explain for hit in search(
        index, "compost water drainage", mode="hybrid", k=10, explain=True)}
    assert strict["garden/water.md"]["lexical_rank"] is None
    assert strict["garden/water.md"]["confidence"] == "weak"


def test_hybrid_any_and_rerank_and_linear(index):
    question = "how does compost improve soil drainage"
    for kwargs in ({"fusion": "linear"}, {"rerank": True, "candidate_depth": 6}, {}):
        for match in ("any", "auto"):
            hits = search(index, question, mode="hybrid", k=3, lexical_match=match, **kwargs)
            assert hits and hits[0].doc_id in {"garden/soil.md", "garden/compost.md"}
            assert len(hits) == len(set(_ids(hits)))


def test_index_search_wrapper_passes_lexical_match(index):
    hits = index.search("how does compost improve soil drainage", mode="lexical", k=3,
                        lexical_match="auto")
    assert _ids(hits)[0] == "garden/soil.md"


def test_lexical_only_index_supports_relaxed_matching(lexical_index):
    hits = search(lexical_index, "how does compost improve soil drainage", mode="lexical",
                  k=3, lexical_match="auto", explain=True)
    assert _ids(hits)[0] == "garden/soil.md"
    assert hits[0].explain["confidence"] == "strong"


# ---- real model (skipped without the [model] extra) ---------------------------------------


OFF_TOPIC = [
    "how do I renew my passport",
    "quantum entanglement explained",
    "who won the world cup in 1998",
    "recipe for chicken curry",
    "how to train a puppy",
    "what is the capital of australia",
]


@pytest.fixture(scope="module")
def sample_model_db(tmp_path_factory):
    pytest.importorskip("sentence_transformers")
    from onefind.embed import DEFAULT_MODEL, SentenceEmbedder
    from onefind.ingest import ingest_path

    embedder = SentenceEmbedder(DEFAULT_MODEL)
    db = tmp_path_factory.mktemp("quality-model") / "sample.db"
    with Index.open(db) as idx:
        idx.attach_embedder(embedder)
        ingest_path(idx, "sample-data")
    return db, embedder


def _open_model(sample_model_db):
    db, embedder = sample_model_db
    idx = Index.open(db)
    idx.attach_embedder(embedder)
    return idx


def _queries():
    return [
        json.loads(line)
        for line in open("sample-data/queries.jsonl", encoding="utf-8")
        if line.strip()
    ]


def test_model_natural_language_questions_get_keyword_hits(sample_model_db):
    natural = [query for query in _queries() if query["category"] == "semantic"]
    assert natural
    with _open_model(sample_model_db) as idx:
        for query in natural:
            assert search(idx, query["query"], mode="lexical", k=10) == []
            for match in ("any", "auto"):
                ids = _ids(search(idx, query["query"], mode="lexical", k=10, lexical_match=match))
                assert set(query["relevant"]) & set(ids[:3]), (match, query["query"])


def test_model_auto_does_not_hurt_hybrid(sample_model_db):
    def hit_at_3(match):
        found = 0
        for query in answerable:
            ids = _ids(search(idx, query["query"], mode="hybrid", k=10, lexical_match=match))
            found += bool(set(query["relevant"]) & set(ids[:3]))
        return found

    answerable = [query for query in _queries() if query["relevant"]]
    with _open_model(sample_model_db) as idx:
        assert hit_at_3("auto") >= hit_at_3("all")


def test_model_off_topic_queries_still_abstain_with_auto(sample_model_db):
    with _open_model(sample_model_db) as idx:
        for query in OFF_TOPIC:
            for mode in ("hybrid", "semantic"):
                outcome = search_detailed(idx, query, mode=mode, k=5, lexical_match="auto")
                assert outcome.abstained, (mode, query)


@pytest.mark.parametrize("precision", ["float", "binary"])
def test_model_hybrid_auto_rank_swap_tie_goes_to_meaning(sample_model_db, precision):
    # compost.md shares more of the words, photosynthesis.md is far closer in
    # meaning; their RRF scores tie exactly and the semantic rank decides.
    with _open_model(sample_model_db) as idx:
        for query in ("how plants turn sunlight into food",
                      "how do plants turn sunlight into energy"):
            outcome = search_detailed(idx, query, mode="hybrid", k=10, precision=precision,
                                      lexical_match="auto")
            assert outcome.hits[0].doc_id == "photosynthesis.md", (precision, query)
            assert outcome.confidences[0] == "strong"


def test_model_auto_strict_pass_ignores_stopwords(sample_model_db):
    with _open_model(sample_model_db) as idx:
        outcome = search_detailed(idx, "what is wild yeast", mode="lexical", k=10,
                                  explain=True, lexical_match="auto")
        assert outcome.strict_count == 1 and outcome.relaxed is False
        assert _ids(outcome.hits) == ["sourdough.md"]
        assert outcome.hits[0].explain["confidence"] == "strong"
        # "happen" is a real keyword the note lacks: still a partial match
        # lexically, but hybrid agreement makes it strong.
        hybrid = search_detailed(idx, "how does galvanic corrosion happen", mode="hybrid",
                                 k=10, lexical_match="auto")
        assert hybrid.hits[0].doc_id == "materials/rust-prevention.md"
        assert hybrid.confidences[0] == "strong"


BINARY_OFF_TOPIC = [
    "tips for a job interview",
    "how to clean a cast iron pan",
    "wedding speech ideas",
    "how to grow tomatoes",
    "best hiking trails in colorado",
]


def test_model_binary_off_topic_queries_abstain(sample_model_db):
    with _open_model(sample_model_db) as idx:
        for query in BINARY_OFF_TOPIC + OFF_TOPIC:
            for mode in ("semantic", "hybrid"):
                outcome = search_detailed(idx, query, mode=mode, k=5, precision="binary",
                                          lexical_match="auto")
                assert outcome.abstained, (mode, query)


def test_model_binary_answerable_queries_do_not_abstain(sample_model_db):
    answerable = [query for query in _queries() if query["relevant"]]
    with _open_model(sample_model_db) as idx:
        for query in answerable:
            for mode in ("semantic", "hybrid"):
                outcome = search_detailed(idx, query["query"], mode=mode, k=10,
                                          precision="binary", lexical_match="auto")
                assert not outcome.abstained, (mode, query["query"])
