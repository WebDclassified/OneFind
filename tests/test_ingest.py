"""T-02 acceptance tests: ingest counts, idempotency, failure paths."""

import sqlite3

import pytest

from onefind.ingest import ingest_path
from onefind.errors import CorpusNotFoundError
from onefind.store import Index

SAMPLE = "sample-data"


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "t02.db"


def test_counts_match_input_files(db_path):
    with Index.open(db_path) as idx:
        stats = ingest_path(idx, SAMPLE)
    assert stats["files_indexed"] == 4
    assert stats["total_documents"] == 4
    with Index.open(db_path) as idx:
        assert idx.count("documents") == 4
        assert idx.count("fts") == 4
        assert idx.count("chunks") == 4


def test_reindex_is_idempotent(db_path):
    with Index.open(db_path) as idx:
        ingest_path(idx, SAMPLE)
        first_stats = idx.stats()
        ingest_path(idx, SAMPLE)
        second_stats = idx.stats()
    assert first_stats == second_stats
    assert first_stats["documents"] == 4


def test_missing_folder_raises_data_error(db_path):
    with Index.open(db_path) as idx, pytest.raises(CorpusNotFoundError):
        ingest_path(idx, db_path.with_name("does-not-exist"))


def test_empty_folder_raises_clear_error(db_path, tmp_path):
    empty = tmp_path / "empty-corpus"
    empty.mkdir()
    with Index.open(db_path) as idx, pytest.raises(CorpusNotFoundError) as excinfo:
        ingest_path(idx, empty)
    assert ".txt/.md" in str(excinfo.value)


def test_partial_batch_failure_leaves_db_openable(db_path, tmp_path):
    """A generator blowing up mid-stream must leave committed batches valid."""
    from onefind.store import Document

    def exploding_docs(n=300):
        for i in range(n):
            if i == 260:  # past the first 256-doc batch boundary
                raise RuntimeError("simulated crash mid-index")
            yield Document(doc_id=f"d{i}", body=f"body {i}")

    idx = Index.open(db_path)
    with pytest.raises(RuntimeError):
        idx.add_documents(exploding_docs())
    idx.close()

    with Index.open(db_path) as reopened:  # must open cleanly
        assert reopened.count("documents") == 256  # exactly the first batch
