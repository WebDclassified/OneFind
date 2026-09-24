"""Storage consistency, vector lifecycle, and fake-embedder edge cases."""

import sqlite3

import numpy as np
import pytest

from onefind.errors import DataError, UsageError
from onefind.search import semantic_search
from onefind.store import Document, Index, SCHEMA_VERSION


class FakeEmbedder:
    name = "fake-mini"
    revision = "test-revision-1"
    dimension = 3

    def __init__(self):
        self.document_calls = 0
        self.query_calls = 0

    def _vector(self, text: str) -> np.ndarray:
        lowered = text.lower()
        if "orbit" in lowered:
            vector = np.array([1.0, 0.05, 0.0], dtype=np.float32)
        elif "water" in lowered or "compost" in lowered:
            vector = np.array([0.0, 1.0, 0.05], dtype=np.float32)
        elif "money" in lowered or "finance" in lowered:
            vector = np.array([0.05, 0.0, 1.0], dtype=np.float32)
        else:
            digest = np.frombuffer(text.encode("utf-8").ljust(3, b"\0")[:3], dtype=np.uint8)
            vector = digest.astype(np.float32) + 0.1
        return vector / np.linalg.norm(vector)

    def encode_documents(self, texts):
        self.document_calls += 1
        return np.stack([self._vector(text) for text in texts])

    def encode_query(self, text):
        self.query_calls += 1
        return self._vector(text)


class BadShapeEmbedder(FakeEmbedder):
    def encode_documents(self, texts):
        self.document_calls += 1
        return np.ones((max(0, len(texts) - 1), self.dimension), dtype=np.float32)


def test_unrelated_sqlite_file_is_rejected_without_schema_mutation(tmp_path):
    path = tmp_path / "unrelated.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE private_data(value TEXT)")
        conn.execute("INSERT INTO private_data VALUES ('keep')")
        conn.commit()

    with pytest.raises(DataError, match="not a OneFind database"):
        Index.open(path)

    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT value FROM private_data").fetchone()[0] == "keep"
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='schema_meta'"
        ).fetchone()[0] == 0


def test_schema_version_mismatch_is_rejected(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO schema_meta VALUES ('schema_version', '1')"
        )
        conn.commit()
    with pytest.raises(DataError, match="unsupported schema version 1"):
        Index.open(path)


def test_new_database_records_current_schema(tmp_path):
    with Index.open(tmp_path / "new.db") as index:
        assert index.get_meta("schema_version") == str(SCHEMA_VERSION)
        assert index.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_attaching_embedder_backfills_lexical_documents(tmp_path):
    embedder = FakeEmbedder()
    with Index.open(tmp_path / "backfill.db") as index:
        index.add_documents(
            [
                Document("orbit.md", "Orbit", "orbit orbit"),
                Document("water.md", "Water", "water water"),
            ]
        )
        index.attach_embedder(embedder)
        assert index.count("vectors") == 2
        assert index.health()["ok"] is True
        assert index.health()["missing_vectors"] == 0


def test_lexical_update_cannot_leave_stale_vectors(tmp_path):
    path = tmp_path / "stale.db"
    with Index.open(path) as index:
        index.attach_embedder(FakeEmbedder())
        index.add_documents([Document("d", "Title", "original orbit")])

    with Index.open(path) as index:
        with pytest.raises(UsageError, match="stay synchronized"):
            index.add_documents([Document("d", "Title", "changed water")])


def test_model_revision_mismatch_is_rejected(tmp_path):
    class OtherRevision(FakeEmbedder):
        revision = "test-revision-2"

    with Index.open(tmp_path / "revision.db") as index:
        index.attach_embedder(FakeEmbedder())
        index.attach_embedder(FakeEmbedder())
        with pytest.raises(DataError, match="model revision"):
            index.attach_embedder(OtherRevision())


def test_invalid_encoder_output_rolls_back_whole_batch(tmp_path):
    with Index.open(tmp_path / "rollback.db") as index:
        index.attach_embedder(BadShapeEmbedder())
        with pytest.raises(DataError, match="invalid shape"):
            index.add_documents([Document("d", "Title", "orbit")])
        assert index.count("documents") == 0
        assert index.count("chunks") == 0
        assert index.count("fts") == 0
        assert index.count("vectors") == 0
        assert index.get_meta("int8_scale") is None


def test_clear_removes_content_but_keeps_schema(tmp_path):
    with Index.open(tmp_path / "clear.db") as index:
        index.attach_embedder(FakeEmbedder())
        index.add_documents([Document("d", "Title", "orbit")])
        index.clear()
        assert index.stats()["documents"] == 0
        assert index.stats().get("vectors", 0) == 0
        assert index.get_meta("schema_version") == str(SCHEMA_VERSION)
        assert index.health()["ok"] is True


def test_float_semantic_search_uses_native_knn_not_full_scan(tmp_path, monkeypatch):
    embedder = FakeEmbedder()
    with Index.open(tmp_path / "native.db") as index:
        index.attach_embedder(embedder)
        index.add_documents(
            [
                Document("orbit.md", "Orbit", "orbit"),
                Document("water.md", "Water", "water"),
                Document("money.md", "Money", "money"),
            ]
        )
        monkeypatch.setattr(
            index,
            "get_all_vectors",
            lambda: (_ for _ in ()).throw(AssertionError("full scan used")),
        )
        hits = semantic_search(index, "orbit", k=3, precision="float")
    assert hits[0].doc_id == "orbit.md"


def test_external_vector_update_invalidates_reader_cache(tmp_path):
    path = tmp_path / "external.db"
    first = Index.open(path)
    second = Index.open(path)
    try:
        embedder = FakeEmbedder()
        first.attach_embedder(embedder)
        second.attach_embedder(embedder)
        first.add_documents([Document("d", "Title", "original orbit")])
        before = second.get_all_vectors()[0].copy()

        first.add_documents([Document("d", "Title", "changed money")])
        after = second.get_all_vectors()[0]
        assert not np.array_equal(before, after)
    finally:
        first.close()
        second.close()
