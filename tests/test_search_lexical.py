"""T-03 acceptance tests: BM25 ranking, determinism, sanitization, CLI."""

import pytest

from scrydb.cli import EXIT_OK, EXIT_USAGE, main
from scrydb.errors import EmptyQueryError
from scrydb.ingest import ingest_path
from scrydb.search import lexical_search, sanitize_fts_query
from scrydb.store import Index


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
    assert hits[0].doc_id == "photosynthesis"


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
    assert "[" in hits[0].snippet or "]" in hits[0].snippet


# ---- CLI surface -------------------------------------------------------------


def test_cli_search_lexical_roundtrip(indexed_db, capsys):
    code = main(["search", "chlorophyll", "--db", str(indexed_db), "--mode", "lexical"])
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "photosynthesis" in out


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
