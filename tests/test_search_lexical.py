"""T-03 acceptance tests: BM25 ranking, determinism, sanitization, CLI."""

import pytest

from onefind.cli import EXIT_OK, EXIT_USAGE, main
from onefind.errors import EmptyQueryError, UsageError
from onefind.ingest import ingest_path
from onefind.search import lexical_search, sanitize_fts_query
from onefind.store import Index


@pytest.fixture(scope="module")
def indexed_db(tmp_path_factory):
    db = tmp_path_factory.mktemp("t03") / "t03.db"
    with Index.open(db) as idx:
        ingest_path(idx, "sample-data")
    return db


def _hits(db, query, k=10):
    with Index.open(db) as idx:
        return lexical_search(idx.conn, query, k=k)


def test_relevant_document_ranks_first(indexed_db):
    hits = _hits(indexed_db, "chlorophyll photosynthesis")
    assert hits, "expected at least one result"
    assert hits[0].doc_id == "photosynthesis.md"


def test_scores_are_higher_is_better_and_ordered(indexed_db):
    hits = _hits(indexed_db, "vitamin")
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)


def test_deterministic_ordering_across_runs(indexed_db):
    q = "fermentation"
    assert [h.doc_id for h in _hits(indexed_db, q)] == [
        h.doc_id for h in _hits(indexed_db, q)
    ]


def test_no_match_returns_empty_not_error(indexed_db):
    assert _hits(indexed_db, "zzzqqx nonexistentterm") == []


def test_empty_query_raises_usage_error(indexed_db):
    with pytest.raises(EmptyQueryError):
        _hits(indexed_db, "   ")


def test_hostile_query_syntax_does_not_crash(indexed_db):
    hostile = 'quotes " inside AND (nested)) OR : colon'
    hits = _hits(indexed_db, hostile)  # must not raise sqlite3 errors
    assert isinstance(hits, list)


def test_snippet_contains_highlight_marker(indexed_db):
    hits = _hits(indexed_db, "starter culture")
    assert hits
    assert "\ue000" in hits[0].snippet or "\ue001" in hits[0].snippet


# ---- CLI surface -------------------------------------------------------------


def test_cli_search_lexical_roundtrip(indexed_db, capsys):
    code = main(["search", "chlorophyll", "--db", str(indexed_db), "--mode", "lexical"])
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "photosynthesis.md" in out


def test_cli_no_match_exit_zero(indexed_db, capsys):
    code = main(["search", "zzzqqx", "--db", str(indexed_db), "--mode", "lexical"])
    assert code == EXIT_OK
    assert "no results" in capsys.readouterr().out


def test_cli_unimplemented_mode_is_explicit(indexed_db):
    code = main(["search", "anything", "--db", str(indexed_db), "--mode", "semantic"])
    assert code == EXIT_USAGE


def test_cli_rejects_k_out_of_range(indexed_db):
    assert main(["search", "q", "--db", str(indexed_db), "--mode", "lexical", "--k", "99"]) == (
        EXIT_USAGE
    )


def test_sanitize_produces_quoted_tokens():
    assert sanitize_fts_query('he said "hi"') == '"he" "said" "hi"'
    assert sanitize_fts_query("AND") == '"AND"'


def test_multi_hit_bm25_is_strongest_first(tmp_path):
    from onefind.store import Document, Index

    with Index.open(tmp_path / "rank.db") as index:
        index.add_documents(
            [
                Document("weak", "Orbit", "orbit"),
                Document("strong", "Orbit", "orbit orbit orbit orbit orbit"),
                Document("medium", "Orbit", "orbit orbit orbit"),
            ]
        )
        hits = lexical_search(index.conn, "orbit", k=3)

    assert [hit.doc_id for hit in hits] == ["strong", "medium", "weak"]
    assert [hit.score for hit in hits] == sorted(
        (hit.score for hit in hits), reverse=True
    )


def test_quote_only_query_returns_no_hits(indexed_db):
    assert _hits(indexed_db, '"""') == []


@pytest.mark.parametrize("bad_k", [0, -1, True, 1.5, 10_001])
def test_direct_search_rejects_invalid_k(indexed_db, bad_k):
    with pytest.raises(UsageError) as excinfo:
        _hits(indexed_db, "vitamin", k=bad_k)
    assert "k must be" in str(excinfo.value)
