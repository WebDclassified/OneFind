"""Regression tests for backend review findings (store, ingest, jobs, library, embed)."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
import types
from pathlib import Path

import numpy as np
import pytest

import onefind.ingest as ingest_module
import onefind.store as store_module
from onefind.errors import DataError, OperationCancelled
from onefind.ingest import _decode, ingest_path, scan_changes
from onefind.jobs import EmbedderCache, JobManager
from onefind.library import Library
from onefind.store import BATCH_SIZE, INTERRUPTED_RUN_ERROR, Document, Index

WAIT = 30.0


class FakeEmbedder:
    revision = "test-revision-1"

    def __init__(self, name: str = "fake-mini", dimension: int = 3) -> None:
        self.name = name
        self.dimension = dimension
        self.calls = 0

    def encode_documents(self, texts):
        self.calls += 1
        return np.stack([self._vector(text) for text in texts])

    def encode_query(self, text):
        return self._vector(text)

    def _vector(self, text):
        digest = hashlib.sha256(text.encode("utf-8")).digest()[: self.dimension]
        vector = np.frombuffer(digest, dtype=np.uint8).astype(np.float32) + 1.0
        return vector / np.linalg.norm(vector)


class SlowEmbedder(FakeEmbedder):
    """Signals when a batch starts encoding and waits for ``release``."""

    def __init__(self, name: str = "fake-mini", dimension: int = 3) -> None:
        super().__init__(name, dimension)
        self.started = threading.Event()
        self.release = threading.Event()
        self.batches = 0

    def encode_documents(self, texts):
        self.batches += 1
        self.started.set()
        assert self.release.wait(WAIT), "test never released the encoder"
        return super().encode_documents(texts)


def write(path: Path, text: str = "", *, data: bytes | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if data is not None:
        path.write_bytes(data)
    else:
        path.write_text(text, encoding="utf-8")
    return path


def bodies(index: Index) -> dict[str, str]:
    return {
        str(row[0]): str(row[1])
        for row in index.conn.execute("SELECT doc_id, body FROM documents ORDER BY doc_id")
    }


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "fixes.db"


@pytest.fixture()
def notes(tmp_path):
    root = tmp_path / "notes"
    write(root / "a.md", "alpha body")
    write(root / "sub" / "b.md", "beta body")
    write(root / "sub" / "c.md", "gamma body")
    return root


def lexical_collection(db: Path, count: int) -> None:
    with Index.open(db) as index:
        index.add_documents(
            [Document(f"d{number:04d}", f"note number {number}", f"Note {number}") for number in range(count)]
        )


# -- finding 1/18/24: re-indexing a sub-folder or a file of a registered root ---------


def test_reindex_single_file_inside_root_updates_in_place(notes, db):
    with Index.open(db) as index:
        ingest_path(index, notes, prune=True)
        index.add_bookmark("sub/b.md", note="keep me")
        write(notes / "sub" / "b.md", "beta body edited")
        result = ingest_path(index, notes / "sub" / "b.md")
        assert result["updated"] == 1 and result["added"] == 0
        assert bodies(index) == {
            "a.md": "alpha body",
            "sub/b.md": "beta body edited",
            "sub/c.md": "gamma body",
        }
        assert [root["path"] for root in index.roots()] == [str(notes.resolve())]
        assert result["root"] == str(notes.resolve())
        assert index.bookmarks()[0]["doc_id"] == "sub/b.md"
        assert index.duplicate_groups() == []
        assert scan_changes(index, notes)["new"] == 0


def test_reindex_subfolder_with_prune_scopes_to_the_subfolder(notes, db):
    with Index.open(db) as index:
        ingest_path(index, notes, prune=True)
        write(notes / "sub" / "b.md", "beta body edited")
        (notes / "sub" / "c.md").unlink()
        (notes / "a.md").unlink()  # outside the sub-folder: must not be pruned
        result = ingest_path(index, notes / "sub", prune=True)
        assert (result["added"], result["updated"], result["removed"]) == (0, 1, 1)
        assert bodies(index) == {"a.md": "alpha body", "sub/b.md": "beta body edited"}
        assert len(index.roots()) == 1
        assert index.duplicate_groups() == []
        sub_scan = scan_changes(index, notes / "sub")
        assert (sub_scan["new"], sub_scan["changed"], sub_scan["deleted"]) == (0, 0, 0)


def test_indexing_a_parent_absorbs_registered_subfolders(notes, db):
    with Index.open(db) as index:
        ingest_path(index, notes / "sub")
        index.add_bookmark("b.md", note="from the sub-folder root")
        result = ingest_path(index, notes, prune=True)
        roots = index.roots()
        assert [root["path"] for root in roots] == [str(notes.resolve())]
        assert result["added"] == 1  # only a.md is new
        assert index.duplicate_groups() == []
        assert {doc["source"] for doc in (index.get_document(d) for d in bodies(index))} == {
            "a.md",
            "sub/b.md",
            "sub/c.md",
        }
        # doc_ids are kept, so the bookmark survives
        assert index.bookmarks()[0]["note"] == "from the sub-folder root"
        assert index.get_document("b.md")["folder"] == "sub"
        assert scan_changes(index, notes)["new"] == 0


# -- finding 2/26: vectors are built outside the write lock, backfill is cancellable ---


def test_backfill_cancel_keeps_finished_batches_and_binding(db):
    lexical_collection(db, BATCH_SIZE + 44)
    embedder = FakeEmbedder()
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] > 1  # cancel before the second batch

    with Index.open(db) as index:
        with pytest.raises(OperationCancelled):
            index.attach_embedder(embedder, cancel=cancel)
        assert index.embedder is None
    with Index.open(db) as index:
        assert index.get_meta("model_name") == "fake-mini"
        assert index.count("vectors") == BATCH_SIZE
        assert index.health()["missing_vectors"] == 44
        index.attach_embedder(embedder)  # the next attach finishes the job
        assert index.health()["ok"] is True


def test_backfill_interrupt_keeps_committed_batches(db):
    lexical_collection(db, 2 * BATCH_SIZE + 10)

    class Interrupting(FakeEmbedder):
        batches = 0

        def encode_documents(self, texts):
            self.batches += 1
            if self.batches == 2:
                raise KeyboardInterrupt
            return super().encode_documents(texts)

    with Index.open(db) as index:
        with pytest.raises(KeyboardInterrupt):
            index.attach_embedder(Interrupting())
        assert not index.conn.in_transaction
    with Index.open(db) as index:
        assert index.count("vectors") == BATCH_SIZE
        assert index.get_meta("model_name") == "fake-mini"


def test_other_connections_can_write_while_the_model_encodes(db):
    lexical_collection(db, 20)
    embedder = SlowEmbedder()
    errors: list[BaseException] = []

    def backfill():
        try:
            with Index.open(db) as index:
                index.attach_embedder(embedder)
                index.add_documents([Document("new", "fresh note body", "Fresh")])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    worker = threading.Thread(target=backfill)
    worker.start()
    try:
        assert embedder.started.wait(WAIT)
        other = sqlite3.connect(db, timeout=1.0)
        try:
            started = time.monotonic()
            other.execute("INSERT INTO saved_queries (name, query, mode, created_at) "
                          "VALUES ('n', 'q', 'lexical', 'now')")
            other.commit()
            assert time.monotonic() - started < 1.0
        finally:
            other.close()
        # second batch: add_documents also encodes before taking the lock
        embedder.started.clear()
        embedder.release.set()
    finally:
        embedder.release.set()
        worker.join(WAIT)
    assert not errors
    with Index.open(db) as index:
        assert index.health()["ok"] is True
        assert index.count("vectors") == 21


def test_job_cancel_stops_the_backfill_between_batches(db, tmp_path):
    lexical_collection(db, BATCH_SIZE + 30)
    folder = write(tmp_path / "more" / "x.md", "extra note").parent
    created: list[SlowEmbedder] = []

    def factory(name):
        created.append(SlowEmbedder(name))
        return created[-1]

    manager = JobManager(EmbedderCache(factory=factory))
    job = manager.start_index(db, folder, embed=True, model_name="fake-mini")
    deadline = time.monotonic() + WAIT
    while not created and time.monotonic() < deadline:
        time.sleep(0.01)
    assert created and created[0].started.wait(WAIT)
    # While the model encodes the first backfill batch, the file is writable.
    other = sqlite3.connect(db, timeout=1.0)
    try:
        other.execute("INSERT INTO saved_queries (name, query, mode, created_at) "
                      "VALUES ('n', 'q', 'lexical', 'now')")
        other.commit()
    finally:
        other.close()
    manager.cancel(job["id"])
    cancelled_at = time.monotonic()
    created[0].release.set()
    final = manager.wait(job["id"], WAIT)
    assert final["status"] == "cancelled"
    assert time.monotonic() - cancelled_at < 5.0
    assert created[0].batches == 1  # stopped before the second batch
    with Index.open(db) as index:
        assert index.count("vectors") == BATCH_SIZE
        assert index.health()["missing_vectors"] == 30


# -- finding 3/4: integrity_check on a long-lived connection ---------------------------


def test_integrity_check_ignores_a_stale_fts_cache(db):
    with Index.open(db) as first:
        first.add_documents([Document(f"a{n}", f"alpha {n}", "A") for n in range(5)])
        assert first.integrity_check()["ok"] is True
        with Index.open(db) as second:
            second.add_documents([Document(f"b{n}", f"beta {n}", "B") for n in range(300)])
            second.remove_documents(["b7"])
        report = first.integrity_check()
        assert report["ok"] is True, report


def test_integrity_check_reports_lock_contention_not_damage(db):
    with Index.open(db) as index:
        index.add_documents([Document("a", "alpha", "A")])
        holder = sqlite3.connect(db, timeout=0.1)
        try:
            holder.execute("BEGIN IMMEDIATE")
            holder.execute("INSERT INTO schema_meta(key, value) VALUES ('x', 'y')")
            index.conn.execute("PRAGMA busy_timeout = 100")
            with pytest.raises(sqlite3.OperationalError, match="locked|busy"):
                index.integrity_check()
        finally:
            holder.rollback()
            holder.close()
        assert index.integrity_check()["ok"] is True


# -- finding 8/29: skipped files never count as new ------------------------------------


def test_scan_changes_ignores_files_ingest_skips(tmp_path, db):
    root = tmp_path / "n"
    write(root / "a.md", "# A\nreal note")
    write(root / "todo.md", "---\ntitle: Todo\ntags: [x]\n---\n")
    write(root / "blank.txt", "   \r\n\t")
    write(root / "data.txt", data=b"abc\x00\x01\x02binary")
    with Index.open(db) as index:
        for _ in range(2):
            ingest_path(index, root, prune=True)
            report = scan_changes(index, root)
            assert (report["new"], report["changed"], report["deleted"]) == (0, 0, 0)
        write(root / "fresh.md", "a new note")
        assert scan_changes(index, root)["new"] == 1


# -- finding 9/25: forgetting a moved/deleted folder -----------------------------------


def test_forget_root_removes_its_notes_and_registration(tmp_path, db):
    keep = write(tmp_path / "keep" / "k.md", "keep note").parent
    gone = write(tmp_path / "gone" / "g.md", "gone note").parent
    with Index.open(db) as index:
        ingest_path(index, keep)
        ingest_path(index, gone)
        root_id = index.get_root(str(gone.resolve()))["root_id"]
        index.add_bookmark("g.md", note="x")
        report = index.forget_root(root_id)
        assert report == {"removed": 1, "forgotten": True, "root": str(gone.resolve())}
        assert set(bodies(index)) == {"k.md"}
        assert [root["path"] for root in index.roots()] == [str(keep.resolve())]
        assert index.bookmarks() == []
        assert index.health()["ok"] is True
        assert index.forget_root(root_id)["forgotten"] is False


def test_relocate_root_keeps_doc_ids(tmp_path, db):
    old = write(tmp_path / "old" / "one.md", "one note").parent
    with Index.open(db) as index:
        ingest_path(index, old)
        index.add_bookmark("one.md", note="n")
        new = tmp_path / "new"
        old.rename(new)
        root_id = index.roots()[0]["root_id"]
        index.relocate_root(root_id, str(new.resolve()))
        result = ingest_path(index, new, prune=True)
        assert (result["added"], result["removed"]) == (0, 0)
        assert set(bodies(index)) == {"one.md"} and len(index.bookmarks()) == 1


# -- finding 10: creating a database is atomic ----------------------------------------


def test_concurrent_open_of_a_new_database_never_deletes_it(tmp_path):
    failures: list[BaseException] = []
    for trial in range(40):
        target = tmp_path / f"race-{trial}.db"
        ready = threading.Event()

        def create():
            ready.set()
            try:
                Index.open(target).close()
            except BaseException as exc:  # noqa: BLE001
                failures.append(exc)

        def follow():
            ready.wait(WAIT)
            deadline = time.monotonic() + 5
            while not target.exists() and time.monotonic() < deadline:
                pass
            try:
                Index.open(target).close()
            except BaseException as exc:  # noqa: BLE001
                failures.append(exc)

        threads = [threading.Thread(target=create), threading.Thread(target=follow)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(WAIT)
        assert target.exists()
        with Index.open(target) as index:
            assert index.get_meta("schema_version") == "3"
    assert failures == []


def test_failed_open_does_not_delete_someone_elses_database(tmp_path, monkeypatch):
    target = tmp_path / "other.db"
    Index.open(target).close()
    size = target.stat().st_size
    assert size > 0
    # An existing file is never "new", so even a failing open keeps it.
    monkeypatch.setattr(store_module, "_ensure_wal", lambda conn: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
    with pytest.raises(sqlite3.OperationalError):
        Index.open(target)
    assert target.exists() and target.stat().st_size == size


# -- finding 11: a finished job has already closed its connection -----------------------


def test_db_can_be_deleted_as_soon_as_the_job_finished(tmp_path, monkeypatch):
    import onefind.jobs as jobs_module

    monkeypatch.setattr(jobs_module, "model_stack_available", lambda: False)
    folder = write(tmp_path / "n" / "a.md", "alpha").parent
    manager = JobManager(EmbedderCache())
    for trial in range(5):
        target = tmp_path / f"job-{trial}.db"
        job = manager.wait(manager.start_index(target, folder)["id"], WAIT)
        assert job["status"] == "succeeded"
        assert manager.active_for(target) is None
        for leftover in (target, Path(f"{target}-wal"), Path(f"{target}-shm")):
            leftover.unlink(missing_ok=True)  # PermissionError on Windows before the fix
        assert not target.exists()


# -- finding 12/32: clear() allows a model with another dimension ----------------------


def test_clear_then_attach_a_model_with_another_dimension(db):
    pytest.importorskip("sqlite_vec")
    with Index.open(db) as index:
        index.attach_embedder(FakeEmbedder("m1", 3))
        index.add_documents([Document("d", "orbit notes", "T")])
        index.clear()
        assert index.embedder is None
        assert index.health()["ok"] is True
        index.attach_embedder(FakeEmbedder("m2", 5))
        index.add_documents([Document("e", "water notes", "W")])
        assert index.count("vectors") == 1
        assert index.get_meta("model_dim") == "5"
        assert index.health()["ok"] is True


# -- finding 20 (interim): long notes are reported -------------------------------------


def test_health_reports_notes_longer_than_the_model_window(db):
    embedder = FakeEmbedder()
    embedder.max_seq_length = 8  # ~32 characters
    with Index.open(db) as index:
        index.attach_embedder(embedder)
        index.add_documents(
            [Document("short", "tiny", "S"), Document("long", "word " * 40, "L")]
        )
        assert index.health()["long_documents"] == 1
        assert index.health()["ok"] is True


# -- finding 21: cached models load without network ------------------------------------


def test_sentence_embedder_loads_from_cache_first(monkeypatch):
    calls: list[dict] = []

    class FakeSentenceTransformer:
        max_seq_length = 256

        def __init__(self, name, device=None, **kwargs):
            calls.append(kwargs)
            if kwargs.get("local_files_only"):
                raise OSError("not in the cache")

        def get_embedding_dimension(self):
            return 3

    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = FakeSentenceTransformer
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    from onefind.embed import SentenceEmbedder

    embedder = SentenceEmbedder("fake/model")
    assert calls == [{"local_files_only": True}, {}]
    assert embedder.dimension == 3 and embedder.max_seq_length == 256

    calls.clear()
    FakeSentenceTransformer.__init__ = lambda self, name, device=None, **kw: calls.append(kw)
    SentenceEmbedder("fake/model")
    assert calls == [{"local_files_only": True}]  # cached: no second (online) load


# -- finding 22: "building vectors" is announced before the model runs ----------------


def test_embed_phase_is_reported_before_the_batch_is_encoded(notes, db):
    events: list[dict] = []
    seen_at_encode: list[list[str]] = []

    class Recording(FakeEmbedder):
        def encode_documents(self, texts):
            seen_at_encode.append([event["phase"] for event in events])
            return super().encode_documents(texts)

    with Index.open(db) as index:
        index.attach_embedder(Recording())
        ingest_path(index, notes, progress=events.append)
    assert seen_at_encode and seen_at_encode[0][-1] == "embed"


# -- finding 23: a folder that cannot be listed is not treated as deleted -------------


def _walk_with_locked(real_walk, locked_name):
    def walk(top, onerror=None, **kwargs):
        for dirpath, dirnames, filenames in real_walk(top, onerror=onerror, **kwargs):
            if locked_name == "" and Path(dirpath) == Path(top):
                if onerror is not None:
                    onerror(PermissionError(13, "Access is denied", str(top)))
                return
            if locked_name in dirnames:
                dirnames.remove(locked_name)
                if onerror is not None:
                    onerror(PermissionError(13, "Access is denied", os.path.join(dirpath, locked_name)))
            yield dirpath, dirnames, filenames

    return walk


def test_unlistable_subfolder_is_not_pruned(tmp_path, db, monkeypatch):
    root = tmp_path / "notes"
    write(root / "a.md", "alpha")
    write(root / "locked" / "secret.md", "secret one")
    write(root / "locked" / "secret2.md", "secret two")
    with Index.open(db) as index:
        ingest_path(index, root, prune=True)
        index.add_bookmark("locked/secret.md", note="important")
        monkeypatch.setattr(ingest_module.os, "walk", _walk_with_locked(os.walk, "locked"))
        report = scan_changes(index, root)
        assert report["deleted"] == 0 and report["unreadable"] == ["locked"]
        result = ingest_path(index, root, prune=True)
        assert result["removed"] == 0
        assert {"path": "locked", "reason": "unreadable"} in result["skipped"]
        assert set(bodies(index)) == {"a.md", "locked/secret.md", "locked/secret2.md"}
        assert index.bookmarks()[0]["note"] == "important"


def test_unlistable_root_fails_before_pruning(tmp_path, db, monkeypatch):
    root = tmp_path / "notes"
    write(root / "a.md", "alpha")
    with Index.open(db) as index:
        ingest_path(index, root, prune=True)
        monkeypatch.setattr(ingest_module.os, "walk", _walk_with_locked(os.walk, ""))
        assert scan_changes(index, root)["deleted"] == 0
        with pytest.raises(DataError) as caught:
            ingest_path(index, root, prune=True)
        assert isinstance(caught.value.__cause__, PermissionError)
        assert set(bodies(index)) == {"a.md"}


# -- finding 27: read-only files ---------------------------------------------------------


def test_read_only_backup_can_be_opened_without_writing(tmp_path, db):
    with Index.open(db) as index:
        index.add_documents([Document("a", "alpha water", "A")])
        backup = Path(index.backup_to(tmp_path / "backup.db")["path"])
    before = backup.read_bytes()
    with Index.open(backup, readonly=True) as index:
        assert index.count("documents") == 1
        assert index.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
    assert backup.read_bytes() == before
    os.chmod(backup, 0o444)
    try:
        with Index.open(backup) as index:  # no "attempt to write a readonly database"
            assert index.count("documents") == 1
    finally:
        os.chmod(backup, 0o666)


# -- finding 28: single-file roots stay "files only" -----------------------------------


def test_single_file_root_is_not_permanently_stale(tmp_path, db):
    docs = tmp_path / "docs"
    for number in range(1, 6):
        write(docs / f"d{number}.md", f"document {number}")
    with Index.open(db) as index:
        ingest_path(index, docs / "d1.md")
        root = index.roots()[0]
        assert root["files_only"] is True
        report = scan_changes(index, docs)
        assert (report["new"], report["changed"], report["deleted"]) == (0, 0, 0)
        write(docs / "d1.md", "document one, edited")
        assert scan_changes(index, docs)["changed"] == 1
        refreshed = ingest_path(index, docs, prune=True, refresh_files=True)
        assert (refreshed["added"], refreshed["updated"]) == (0, 1)
        assert set(bodies(index)) == {"d1.md"}
        # An explicit folder index turns it into a full folder root.
        ingest_path(index, docs, prune=True)
        assert index.roots()[0]["files_only"] is False
        assert len(bodies(index)) == 5


# -- finding 30: two processes share one registry --------------------------------------


def test_concurrent_processes_never_lose_registry_entries(tmp_path):
    home = tmp_path / "home"
    script = textwrap.dedent(
        """
        import sys
        from onefind.library import Library
        home, tag, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
        library = Library(home)
        for number in range(count):
            library.create(f"{tag}-{number}")
        """
    )
    count = 12
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    processes = [
        subprocess.Popen([sys.executable, "-c", script, str(home), tag, str(count)], env=env)
        for tag in ("a", "b")
    ]
    for process in processes:
        assert process.wait(120) == 0
    names = {entry["name"] for entry in Library(home).list()}
    assert len(names) == 2 * count
    assert len(list((home / "collections").glob("*.db"))) == 2 * count


# -- finding 31: renaming a note keeps its bookmark -----------------------------------


def test_renamed_note_keeps_bookmark_after_prune(tmp_path, db):
    root = tmp_path / "n"
    write(root / "plan.md", "the plan for next year")
    write(root / "other.md", "something else")
    with Index.open(db) as index:
        ingest_path(index, root, prune=True)
        index.add_bookmark("plan.md", note="my important annotation")
        index.record_open("plan.md")
        (root / "plan.md").rename(root / "plan-2026.md")
        result = ingest_path(index, root, prune=True)
        assert (result["added"], result["removed"]) == (1, 1)
        marks = index.bookmarks()
        assert [(m["doc_id"], m["note"]) for m in marks] == [
            ("plan-2026.md", "my important annotation")
        ]
        assert index.recent_documents(1)[0]["doc_id"] == "plan-2026.md"


# -- finding 33: ANSI files ------------------------------------------------------------


def test_cp1252_files_decode_without_replacement_characters():
    assert _decode(b"caf\xe9 r\xe9sum\xe9") == "café résumé"
    assert _decode(b"\x93quoted\x94 r\xe9sum\xe9") == "“quoted” résumé"
    assert "�" not in _decode(b"undefined \x81 byte")


# -- finding 34: runs left behind by a dead process -------------------------------------


def test_start_run_closes_runs_left_running(db):
    with Index.open(db) as index:
        dead = index.start_run("notes")
    with Index.open(db) as index:
        assert index.last_run()["status"] == "running"
        index.start_run("notes")
        row = index.conn.execute(
            "SELECT status, error, finished_at FROM index_runs WHERE id = ?", (dead,)
        ).fetchone()
        assert row["status"] == "failed" and row["error"] == INTERRUPTED_RUN_ERROR
        assert row["finished_at"] is not None
