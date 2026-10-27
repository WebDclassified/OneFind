"""Schema v3: migration from v2, metadata columns, user tables, maintenance."""

import json
import sqlite3

import numpy as np
import pytest

import onefind.store as store_module
from onefind.errors import DataError, UsageError
from onefind.search import lexical_search, semantic_search
from onefind.store import BATCH_SIZE, HISTORY_LIMIT, SCHEMA_VERSION, Document, Index


class FakeEmbedder:
    name = "fake-mini"
    revision = "test-revision-1"
    dimension = 3

    def __init__(self):
        self.document_calls = 0
        self.encoded = 0

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
        self.encoded += len(texts)
        return np.stack([self._vector(text) for text in texts])

    def encode_query(self, text):
        return self._vector(text)


# The exact v2 DDL shipped in OneFind 1.1 (src/onefind/store.py, SCHEMA_VERSION 2).
V2_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id     TEXT PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT '',
    body       TEXT NOT NULL,
    source     TEXT,
    meta_json  TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    seq    INTEGER NOT NULL,
    text   TEXT NOT NULL,
    UNIQUE (doc_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

INSERT INTO schema_meta(key, value) VALUES ('schema_version', '2')
    ON CONFLICT(key) DO NOTHING;

CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    doc_id UNINDEXED,
    title,
    body,
    tokenize = 'porter unicode61'
);
"""

V2_DOCS = [
    ("orbit.md", "Orbit notes", "satellites orbit the earth", "orbit.md"),
    ("water.md", "Water cycle", "water evaporates and condenses", "water.md"),
    ("notes/money.md", "Budget", "money and finance planning", "notes/money.md"),
]


def build_v2_database(path, *, with_vectors=False):
    """Create a genuine v2 OneFind database without using the v3 code."""
    conn = sqlite3.connect(path)
    conn.executescript(V2_SCHEMA)
    conn.execute("PRAGMA user_version = 2")
    created = "2025-01-02T03:04:05+00:00"
    for doc_id, title, body, source in V2_DOCS:
        conn.execute(
            "INSERT INTO documents (doc_id, title, body, source, meta_json, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, NULL, ?, ?)",
            (doc_id, title, body, source, created, created),
        )
        conn.execute("INSERT INTO chunks (doc_id, seq, text) VALUES (?, 0, ?)", (doc_id, body))
        conn.execute(
            "INSERT INTO fts (doc_id, title, body) VALUES (?, ?, ?)", (doc_id, title, body)
        )
    if with_vectors:
        import sqlite_vec

        from onefind.embed import compose_embed_text, serialize_float32

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        embedder = FakeEmbedder()
        conn.execute(
            "CREATE VIRTUAL TABLE vec_float USING vec0("
            "embedding float[3] distance_metric=cosine)"
        )
        for key, value in (
            ("model_name", embedder.name),
            ("model_revision", embedder.revision),
            ("model_dim", "3"),
            ("int8_scale", repr(1.0 / 127.0)),
        ):
            conn.execute("INSERT INTO schema_meta(key, value) VALUES (?, ?)", (key, value))
        rows = conn.execute(
            "SELECT c.row_id, c.text, d.title FROM chunks c JOIN documents d USING(doc_id)"
        ).fetchall()
        for row_id, text, title in rows:
            vector = embedder._vector(compose_embed_text(title, text))
            conn.execute(
                "INSERT INTO vec_float(rowid, embedding) VALUES (?, ?)",
                (row_id, serialize_float32(vector)),
            )
    conn.execute("PRAGMA journal_mode=WAL")
    conn.commit()
    conn.close()
    return path


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _indexes(conn):
    return {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }


V3_TABLES = {
    "collection_roots",
    "search_history",
    "saved_queries",
    "bookmarks",
    "doc_activity",
    "index_runs",
}
V3_COLUMNS = {"content_hash", "mtime", "size_bytes", "ext", "folder", "tags_json", "root_id"}


# -- migration -------------------------------------------------------------------


def test_v2_database_is_migrated_in_place(tmp_path):
    path = build_v2_database(tmp_path / "v2.db")
    with Index.open(path) as index:
        assert index.get_meta("schema_version") == str(SCHEMA_VERSION) == "3"
        assert index.conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert V3_COLUMNS <= _columns(index.conn, "documents")
        tables = {
            row[0] for row in index.conn.execute("SELECT name FROM sqlite_master")
        }
        assert V3_TABLES <= tables
        assert {
            "idx_documents_folder",
            "idx_documents_mtime",
            "idx_documents_hash",
        } <= _indexes(index.conn)
        # Data, FTS, and search survive untouched.
        assert index.count("documents") == 3
        assert index.count("fts") == 3
        assert index.health()["ok"] is True
        hits = lexical_search(index.conn, "evaporates")
        assert [hit.doc_id for hit in hits] == ["water.md"]
        doc = index.get_document("notes/money.md")
        assert doc["title"] == "Budget"
        assert doc["content_hash"] is None and doc["folder"] is None
        assert doc["tags"] == [] and doc["bookmarked"] is False
        # created_at falls back to the oldest document timestamp.
        assert index.get_meta("created_at") == "2025-01-02T03:04:05+00:00"


def test_migration_is_idempotent(tmp_path):
    path = build_v2_database(tmp_path / "v2.db")
    with Index.open(path) as index:
        before = sorted(_columns(index.conn, "documents"))
        store_module._migrate_to_v3(index.conn)  # re-running is a no-op
        assert sorted(_columns(index.conn, "documents")) == before
    with Index.open(path) as index:
        assert index.count("documents") == 3
        assert index.get_meta("schema_version") == "3"
        index.add_documents([Document("new.md", "fresh orbit text", "New", ext=".md")])
    with Index.open(path) as index:
        assert index.count("documents") == 4
        assert index.get_document("new.md")["ext"] == ".md"


def test_migration_failure_rolls_back_to_intact_v2(tmp_path, monkeypatch):
    path = build_v2_database(tmp_path / "v2.db")
    monkeypatch.setattr(
        store_module,
        "_V3_STATEMENTS",
        store_module._V3_STATEMENTS + ("CREATE TABLE broken (",),
    )
    with pytest.raises(DataError, match="could not upgrade"):
        Index.open(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == "2"
        assert not (V3_COLUMNS & _columns(conn, "documents"))
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 3
    monkeypatch.undo()
    with Index.open(path) as index:  # a later open still upgrades cleanly
        assert index.get_meta("schema_version") == "3"


def test_migrated_vectors_keep_working(tmp_path):
    path = build_v2_database(tmp_path / "v2vec.db", with_vectors=True)
    embedder = FakeEmbedder()
    with Index.open(path) as index:
        index.attach_embedder(embedder)
        assert embedder.encoded == 0  # nothing to backfill
        assert index.health()["ok"] is True
        hits = semantic_search(index, "orbit", k=3, precision="float")
        assert hits[0].doc_id == "orbit.md"


def test_v3_file_missing_user_tables_is_healed(tmp_path):
    path = tmp_path / "partial.db"
    with Index.open(path) as index:
        index.add_documents([Document("a", "alpha", "A")])
        index.conn.execute("DROP TABLE saved_queries")
        index.conn.commit()
    with Index.open(path) as index:
        assert index.saved_queries() == []
        assert index.count("documents") == 1


@pytest.mark.parametrize("version", ["1", "4", "99"])
def test_unknown_schema_versions_are_rejected(tmp_path, version):
    path = tmp_path / f"v{version}.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO schema_meta VALUES ('schema_version', ?)", (version,))
    with pytest.raises(DataError, match=f"unsupported schema version {version}"):
        Index.open(path)


def test_v2_missing_required_tables_is_rejected_without_migration(tmp_path):
    path = tmp_path / "broken-v2.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO schema_meta VALUES ('schema_version', '2')")
    with pytest.raises(DataError, match="missing required tables"):
        Index.open(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == "2"


def test_new_database_is_created_at_v3(tmp_path):
    with Index.open(tmp_path / "fresh.db") as index:
        assert V3_COLUMNS <= _columns(index.conn, "documents")
        assert index.get_meta("created_at")
        stats = index.stats()
        assert stats["roots"] == 0
        assert stats["last_indexed_at"] is None
        assert stats["collection_name"] is None
        index.set_meta("collection_name", "Notes")
        assert index.stats()["collection_name"] == "Notes"


# -- documents ----------------------------------------------------------------------


def _doc(doc_id, body, title="", **fields):
    return Document(doc_id=doc_id, body=body, title=title, source=doc_id, **fields)


def test_add_documents_persists_and_upserts_metadata(tmp_path):
    with Index.open(tmp_path / "meta.db") as index:
        root_id = index.ensure_root("/notes")
        index.add_documents(
            [
                _doc(
                    "a/x.md",
                    "alpha body",
                    "X",
                    content_hash="h1",
                    mtime=1700000000.5,
                    size_bytes=10,
                    ext=".md",
                    folder="a",
                    tags=["Work", " work ", "Ideas", ""],
                    root_id=root_id,
                )
            ]
        )
        doc = index.get_document("a/x.md")
        assert doc["content_hash"] == "h1"
        assert doc["mtime"] == 1700000000.5
        assert doc["size_bytes"] == 10
        assert doc["ext"] == ".md" and doc["folder"] == "a"
        assert doc["tags"] == ["work", "ideas"]
        assert doc["root_id"] == root_id
        assert doc["created_at"] and doc["updated_at"]

        index.add_documents([_doc("a/x.md", "beta body", "X2", content_hash="h2")])
        doc = index.get_document("a/x.md")
        assert doc["content_hash"] == "h2" and doc["title"] == "X2"
        assert doc["tags"] == [] and doc["folder"] is None and doc["root_id"] is None
        assert index.count("documents") == index.count("fts") == 1
        assert index.get_document("missing") is None


def test_add_documents_reports_progress_per_committed_batch(tmp_path):
    events = []
    docs = [_doc(f"d{i}", f"body {i}") for i in range(BATCH_SIZE + 10)]
    with Index.open(tmp_path / "progress.db") as index:
        assert index.add_documents(docs, progress=events.append) == len(docs)
        assert events == [
            {"phase": "write", "done": BATCH_SIZE, "total": len(docs)},
            {"phase": "write", "done": len(docs), "total": len(docs)},
        ]
        events.clear()
        index.add_documents((d for d in docs[:3]), progress=events.append)
        assert events == [{"phase": "write", "done": 3, "total": None}]


def test_add_documents_progress_reports_embed_phase(tmp_path):
    events = []
    with Index.open(tmp_path / "embed.db") as index:
        index.attach_embedder(FakeEmbedder())
        index.add_documents([_doc("o", "orbit")], progress=events.append)
    assert events == [{"phase": "embed", "done": 1, "total": 1}]


def test_progress_callback_error_keeps_committed_batch(tmp_path):
    def explode(_event):
        raise RuntimeError("callback failed")

    with Index.open(tmp_path / "cb.db") as index:
        with pytest.raises(RuntimeError):
            index.add_documents([_doc("a", "alpha")], progress=explode)
        assert index.count("documents") == 1


def test_remove_documents_cleans_every_derived_row(tmp_path):
    embedder = FakeEmbedder()
    with Index.open(tmp_path / "remove.db") as index:
        index.attach_embedder(embedder)
        index.add_documents(
            [_doc("orbit.md", "orbit"), _doc("water.md", "water"), _doc("money.md", "money")]
        )
        index.add_bookmark("orbit.md", "keep")
        index.record_open("orbit.md")
        assert index.remove_documents(["orbit.md", "orbit.md", "unknown"]) == 1
        assert index.count("documents") == index.count("chunks") == index.count("fts") == 2
        assert index.count("vectors") == 2
        assert index.health()["ok"] is True
        assert index.bookmarks() == []
        assert index.conn.execute("SELECT COUNT(*) FROM doc_activity").fetchone()[0] == 0
        assert lexical_search(index.conn, "orbit") == []
        assert all(hit.doc_id != "orbit.md" for hit in semantic_search(index, "orbit", k=5))
        assert index.remove_documents([]) == 0


def test_remove_documents_without_vectors_and_in_bulk(tmp_path):
    with Index.open(tmp_path / "bulk.db") as index:
        index.add_documents([_doc(f"d{i}", f"body {i}") for i in range(1200)])
        removed = index.remove_documents(f"d{i}" for i in range(1100))
        assert removed == 1100
        assert index.count("documents") == index.count("fts") == index.count("chunks") == 100
        assert index.health()["ok"] is True


def test_fingerprints_and_manifest(tmp_path):
    with Index.open(tmp_path / "fp.db") as index:
        first = index.ensure_root("/one")
        second = index.ensure_root("/two")
        index.add_documents(
            [
                _doc("a", "alpha", content_hash="ha", root_id=first, mtime=1.0, size_bytes=5),
                _doc("b", "beta", content_hash="hb", root_id=second),
                _doc("c", "gamma"),
            ]
        )
        assert index.fingerprints() == {"a": "ha", "b": "hb", "c": None}
        assert index.fingerprints(first) == {"a": "ha"}
        manifest = index.document_manifest(first)
        assert manifest == {
            "a": {
                "content_hash": "ha",
                "mtime": 1.0,
                "size_bytes": 5,
                "root_id": first,
                "title": "",
                "source": "a",
                "meta_json": None,
            }
        }
        assert index.update_metadata(
            [{"doc_id": "a", "mtime": 2.0, "size_bytes": 6, "root_id": first,
              "folder": "", "ext": ".md"}]
        ) == 1
        assert index.get_document("a")["mtime"] == 2.0


# -- roots ---------------------------------------------------------------------------


def test_roots_registration(tmp_path):
    with Index.open(tmp_path / "roots.db") as index:
        root_id = index.ensure_root("/notes", ["drafts"])
        assert index.ensure_root("/notes") == root_id  # None keeps excludes
        assert index.roots()[0]["exclude"] == ["drafts"]
        assert index.ensure_root("/notes", []) == root_id
        assert index.get_root("/notes")["exclude"] == []
        index.add_documents([_doc("a", "alpha", root_id=root_id)])
        [root] = index.roots()
        assert root["documents"] == 1 and root["last_indexed_at"] is None
        assert root["path"] == "/notes" and root["added_at"]
        index.mark_root_indexed(root_id)
        assert index.roots()[0]["last_indexed_at"]
        assert index.stats()["last_indexed_at"] == index.roots()[0]["last_indexed_at"]
        assert index.stats()["roots"] == 1
        assert index.get_root("/elsewhere") is None
        assert index.remove_root(root_id) is True
        assert index.remove_root(root_id) is False
        assert index.get_document("a")["root_id"] is None
        with pytest.raises(UsageError):
            index.ensure_root("   ")
        with pytest.raises(UsageError):
            index.ensure_root("/x", "drafts")  # type: ignore[arg-type]
        with pytest.raises(UsageError):
            index.ensure_root("/x", [1])  # type: ignore[list-item]


# -- duplicates & facets ----------------------------------------------------------------


def test_duplicates(tmp_path):
    with Index.open(tmp_path / "dups.db") as index:
        root = index.ensure_root("/r")
        index.add_documents(
            [
                _doc("a", "same", content_hash="h", root_id=root),
                _doc("b", "same", content_hash="h"),
                _doc("c", "same", content_hash="h"),
                _doc("d", "other", content_hash="h2"),
                _doc("e", "other", content_hash="h2"),
                _doc("f", "unique", content_hash="h3"),
                _doc("g", "nohash"),
                _doc("h", "nohash"),
            ]
        )
        assert index.duplicates_of("b") == ["a", "c"]
        assert index.duplicates_of("f") == []
        assert index.duplicates_of("g") == []
        assert index.duplicates_of("unknown") == []
        assert index.duplicate_groups() == [["a", "b", "c"], ["d", "e"]]
        assert index.duplicate_groups(limit=1) == [["a", "b", "c"]]
        assert index.duplicate_groups(root_id=root) == [["a", "b", "c"]]


def test_facets(tmp_path):
    with Index.open(tmp_path / "facets.db") as index:
        assert index.facets() == {
            "folders": [],
            "types": [],
            "tags": [],
            "modified": {"min": None, "max": None},
        }
        index.add_documents(
            [
                _doc("a.md", "x", folder="", ext=".md", tags=["Work"], mtime=100.0),
                _doc("n/b.md", "x", folder="n", ext=".md", tags=["work", "home"], mtime=300.0),
                _doc("n/c.txt", "x", folder="n", ext=".txt", mtime=200.0),
                _doc("jsonl-row", "x"),
            ]
        )
        index.conn.execute("UPDATE documents SET tags_json = 'not json' WHERE doc_id = 'n/c.txt'")
        index.conn.commit()
        facets = index.facets()
        assert facets["folders"] == [{"folder": "n", "count": 2}, {"folder": "", "count": 1}]
        assert facets["types"] == [{"ext": ".md", "count": 2}, {"ext": ".txt", "count": 1}]
        assert facets["tags"] == [{"tag": "work", "count": 2}, {"tag": "home", "count": 1}]
        assert facets["modified"] == {"min": 100.0, "max": 300.0}


# -- history & saved queries --------------------------------------------------------------


def test_search_history_dedup_and_cap(tmp_path):
    with Index.open(tmp_path / "history.db") as index:
        index.record_search("orbit", "hybrid", "float", {"exts": [".md"]}, 3)
        index.record_search("water", "lexical", None, None, 1)
        index.record_search("orbit", "hybrid", "float", None, 4)
        index.record_search("orbit", "lexical", None, {}, 2)
        index.record_search("   ", "hybrid", None, None, 0)  # ignored
        recent = index.recent_searches()
        assert [(item["query"], item["mode"]) for item in recent] == [
            ("orbit", "lexical"),
            ("orbit", "hybrid"),
            ("water", "lexical"),
        ]
        assert recent[1]["result_count"] == 4 and recent[1]["filters"] is None
        assert recent[0]["filters"] is None and recent[0]["searched_at"]
        assert len(index.recent_searches(limit=1)) == 1
        assert len(index.recent_searches(limit=0)) == 1  # clamped, never empty
        for number in range(HISTORY_LIMIT + 25):
            index.record_search(f"q{number}", "lexical", None, None, number)
        total = index.conn.execute("SELECT COUNT(*) FROM search_history").fetchone()[0]
        assert total == HISTORY_LIMIT
        assert index.recent_searches(1)[0]["query"] == f"q{HISTORY_LIMIT + 24}"
        assert index.clear_history() == HISTORY_LIMIT
        assert index.recent_searches() == []
        with pytest.raises(UsageError):
            index.record_search("x", "hybrid", None, ["not", "a", "dict"], 1)


def test_history_accepts_filter_objects_with_to_dict(tmp_path):
    class Filters:
        def to_dict(self):
            return {"tags": ["work"]}

    with Index.open(tmp_path / "filters.db") as index:
        index.record_search("x", "hybrid", "int8", Filters(), 1)
        assert index.recent_searches()[0]["filters"] == {"tags": ["work"]}


def test_saved_queries(tmp_path):
    with Index.open(tmp_path / "saved.db") as index:
        first = index.save_query(" Weekly ", "status report", "hybrid", "float", {"folders": ["work"]})
        assert first["name"] == "Weekly" and first["filters"] == {"folders": ["work"]}
        assert first["precision"] == "float" and first["created_at"]
        replaced = index.save_query("weekly", "status update", "lexical")
        assert replaced["id"] == first["id"] and replaced["query"] == "status update"
        second = index.save_query("Ideas", "brainstorm", "semantic")
        assert [item["name"] for item in index.saved_queries()] == ["Ideas", "weekly"]
        assert index.delete_saved_query(second["id"]) is True
        assert index.delete_saved_query(second["id"]) is False
        assert index.delete_saved_query("nope") is False  # type: ignore[arg-type]
        for bad in (
            ("", "q", "hybrid"),
            ("n", "  ", "hybrid"),
            ("n", "q", "fuzzy"),
            ("x" * 201, "q", "hybrid"),
        ):
            with pytest.raises(UsageError):
                index.save_query(*bad)
        with pytest.raises(UsageError):
            index.save_query("n", "q", "hybrid", precision="int4")


# -- bookmarks & activity ---------------------------------------------------------------


def test_bookmarks(tmp_path):
    with Index.open(tmp_path / "bm.db") as index:
        index.add_documents([_doc("a", "alpha", "A", folder=""), _doc("b", "beta", "B")])
        entry = index.add_bookmark("a", "  read later ")
        assert entry["note"] == "read later" and entry["title"] == "A"
        assert index.add_bookmark("a", "new note")["note"] == "new note"
        index.add_bookmark("b")
        assert {item["doc_id"] for item in index.bookmarks()} == {"a", "b"}
        assert index.is_bookmarked("a") and index.get_document("a")["bookmarked"]
        assert index.remove_bookmark("a") is True
        assert index.remove_bookmark("a") is False
        assert not index.is_bookmarked("a")
        with pytest.raises(DataError, match="not found"):
            index.add_bookmark("missing")


def test_record_open_and_recent_documents(tmp_path):
    with Index.open(tmp_path / "recent.db") as index:
        index.add_documents(
            [
                _doc("old", "x", mtime=100.0),
                _doc("new", "x", mtime=300.0),
                _doc("mid", "x", mtime=200.0),
            ]
        )
        assert [item["doc_id"] for item in index.recent_documents()] == ["new", "mid", "old"]
        index.record_open("old")
        index.record_open("old")
        recent = index.recent_documents(limit=2)
        assert [item["doc_id"] for item in recent] == ["old", "new"]
        assert recent[0]["open_count"] == 2 and recent[0]["last_opened_at"]
        assert recent[1]["last_opened_at"] is None
        with pytest.raises(DataError):
            index.record_open("missing")


# -- runs -----------------------------------------------------------------------------


def test_runs(tmp_path):
    with Index.open(tmp_path / "runs.db") as index:
        assert index.last_run() is None
        run_id = index.start_run("/notes")
        assert index.last_run()["status"] == "running"
        index.finish_run(
            run_id,
            "succeeded",
            {"added": 2, "updated": 1, "unchanged": 4, "removed": 0,
             "skipped": [{"path": "x", "reason": "empty"}]},
        )
        run = index.last_run()
        assert run["status"] == "succeeded" and run["finished_at"]
        assert (run["added"], run["updated"], run["unchanged"], run["skipped"]) == (2, 1, 4, 1)
        assert index.stats()["last_indexed_at"] == run["finished_at"]
        failed = index.start_run(None)
        index.finish_run(failed, "failed", None, error="boom")
        assert index.last_run()["error"] == "boom"
        with pytest.raises(UsageError):
            index.finish_run(failed, "running", {})
        for _ in range(store_module.RUNS_LIMIT + 5):
            index.finish_run(index.start_run("/x"), "cancelled", {})
        count = index.conn.execute("SELECT COUNT(*) FROM index_runs").fetchone()[0]
        assert count <= store_module.RUNS_LIMIT + 1


# -- maintenance --------------------------------------------------------------------------


def test_backup_is_consistent_and_guarded(tmp_path):
    source = tmp_path / "live.db"
    dest = tmp_path / "backups" / "copy.db"
    embedder = FakeEmbedder()
    with Index.open(source) as index:
        index.attach_embedder(embedder)
        index.add_documents([_doc("orbit.md", "orbit"), _doc("water.md", "water")])
        index.add_bookmark("orbit.md")
        assert (tmp_path / "live.db-wal").exists()  # data still lives in the WAL
        result = index.backup_to(dest)
        assert result == {"path": str(dest), "bytes": dest.stat().st_size}
        assert not (tmp_path / "backups" / "copy.db-wal").exists()
        with pytest.raises(UsageError, match="already exists"):
            index.backup_to(dest)
        index.add_documents([_doc("money.md", "money")])
        index.backup_to(dest, overwrite=True)
        with pytest.raises(UsageError, match="collection file itself"):
            index.backup_to(source)
        with pytest.raises(UsageError, match="collection file itself"):
            index.backup_to(tmp_path / "." / "live.db", overwrite=True)
        with pytest.raises(UsageError, match="folder"):
            index.backup_to(tmp_path, overwrite=True)
        with pytest.raises(UsageError):
            index.backup_to("  ")
    assert [p.name for p in (tmp_path / "backups").iterdir()] == ["copy.db"]
    with Index.open(dest) as copy:
        copy.attach_embedder(FakeEmbedder())
        assert copy.count("documents") == 3
        assert copy.health()["ok"] is True
        assert copy.is_bookmarked("orbit.md")
        assert semantic_search(copy, "money", k=1)[0].doc_id == "money.md"


def test_integrity_check_and_repair_fix_damage(tmp_path):
    embedder = FakeEmbedder()
    with Index.open(tmp_path / "repair.db") as index:
        index.attach_embedder(embedder)
        index.add_documents(
            [_doc("orbit.md", "orbit"), _doc("water.md", "water"), _doc("money.md", "money")]
        )
        clean = index.integrity_check()
        assert clean["ok"] is True and clean["problems"] == [] and clean["suggestions"] == []
        assert clean["sqlite"] == "ok" and clean["fts"] == "ok"
        assert clean["health"]["ok"] is True

        # Damage: FTS row missing, stray FTS row, stale chunk text, orphan vector.
        index.conn.execute("DELETE FROM fts WHERE doc_id = 'orbit.md'")
        index.conn.execute("INSERT INTO fts (doc_id, title, body) VALUES ('ghost', '', 'boo')")
        index.conn.execute("UPDATE chunks SET text = 'stale' WHERE doc_id = 'water.md'")
        index.conn.execute(
            "INSERT INTO vec_float(rowid, embedding) VALUES (999, ?)",
            (np.array([1, 0, 0], dtype="<f4").tobytes(),),
        )
        index.conn.commit()

        report = index.integrity_check()
        assert report["ok"] is False
        text = " ".join(report["problems"])
        assert "missing from the keyword index" in text
        assert "no longer in the collection" in text
        assert "out-of-date" in text
        assert "belong to documents that were removed" in text
        assert any("Repair" in s for s in report["suggestions"])
        assert len(report["suggestions"]) == len(set(report["suggestions"]))

        before = embedder.encoded
        fixed = index.repair()
        assert fixed["ok"] is True
        assert fixed["fts_rebuilt"] == 3
        assert fixed["orphan_vectors_removed"] == 1
        assert fixed["vectors_embedded"] == 1  # the stale water chunk
        assert fixed["vectors_missing"] == 0
        assert embedder.encoded == before + 1
        assert index.integrity_check()["ok"] is True
        assert [hit.doc_id for hit in lexical_search(index.conn, "orbit")] == ["orbit.md"]
        assert lexical_search(index.conn, "boo") == []


def test_repair_without_embedder_reports_missing_vectors(tmp_path):
    path = tmp_path / "noembed.db"
    with Index.open(path) as index:
        index.attach_embedder(FakeEmbedder())
        index.add_documents([_doc("orbit.md", "orbit"), _doc("water.md", "water")])
        index.conn.execute("DELETE FROM vec_float WHERE rowid = 1")
        index.conn.commit()
    with Index.open(path) as index:
        report = index.integrity_check()
        assert any("no meaning vector" in problem for problem in report["problems"])
        repaired = index.repair()
        assert repaired["vectors_missing"] == 1 and repaired["vectors_embedded"] == 0
        assert repaired["ok"] is False
        index.attach_embedder(FakeEmbedder())  # attaching backfills the gap
        assert index.integrity_check()["ok"] is True


def test_repair_restores_missing_chunks_and_orphan_user_rows(tmp_path):
    with Index.open(tmp_path / "chunks.db") as index:
        root = index.ensure_root("/r")
        index.add_documents([_doc("a", "alpha", root_id=root), _doc("b", "beta")])
        index.add_bookmark("b")
        index.conn.execute("PRAGMA foreign_keys=OFF")
        index.conn.execute("DELETE FROM chunks WHERE doc_id = 'a'")
        index.conn.execute("DELETE FROM documents WHERE doc_id = 'b'")
        index.conn.execute("DELETE FROM collection_roots")
        index.conn.commit()
        index.conn.execute("PRAGMA foreign_keys=ON")
        report = index.repair()
        assert report["chunks_fixed"] == 2  # one orphan chunk dropped, one restored
        assert report["orphan_rows_removed"] == 2  # bookmark + dangling root_id
        assert report["vectors_missing"] is None
        assert index.health()["ok"] is True
        assert index.get_document("a")["root_id"] is None


def test_compact_shrinks_and_keeps_search_working(tmp_path):
    with Index.open(tmp_path / "compact.db") as index:
        index.attach_embedder(FakeEmbedder())
        index.add_documents([_doc(f"d{i}", f"orbit body {i} " * 50) for i in range(300)])
        index.remove_documents([f"d{i}" for i in range(290)])
        result = index.compact()
        assert set(result) == {"before", "after"}
        assert result["after"] < result["before"]
        assert index.health()["ok"] is True
        assert len(lexical_search(index.conn, "orbit", k=20)) == 10
        assert semantic_search(index, "orbit", k=3)


def test_clear_is_a_full_reset_but_keeps_identity(tmp_path):
    with Index.open(tmp_path / "reset.db") as index:
        index.set_meta("collection_name", "Work")
        created = index.get_meta("created_at")
        root = index.ensure_root("/w")
        index.add_documents([_doc("a", "alpha", root_id=root)])
        index.record_search("alpha", "hybrid", None, None, 1)
        index.save_query("A", "alpha", "hybrid")
        index.add_bookmark("a")
        index.record_open("a")
        index.finish_run(index.start_run("/w"), "succeeded", {})
        index.set_meta("custom", "x")
        index.clear()
        for table in store_module.USER_TABLES:
            assert index.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert index.count("documents") == 0
        assert index.get_meta("collection_name") == "Work"
        assert index.get_meta("created_at") == created
        assert index.get_meta("custom") is None
        assert index.stats()["last_indexed_at"] is None


def test_get_documents_includes_display_metadata(tmp_path):
    with Index.open(tmp_path / "gd.db") as index:
        index.add_documents([_doc("a", "alpha", "A", folder="f", ext=".md", tags=["t"])])
        data = index.get_documents(["a", "missing"])
        assert set(data) == {"a"}
        assert data["a"]["title"] == "A" and data["a"]["body"] == "alpha"
        assert data["a"]["folder"] == "f" and data["a"]["tags"] == ["t"]
        assert json.loads(
            index.conn.execute("SELECT tags_json FROM documents").fetchone()[0]
        ) == ["t"]
