"""Auto-sync (docs/11-implementation-contract.md section 10).

Every test uses tiny poll intervals and temporary folders, so the suite stays
fast; waits are bounded polls, never fixed sleeps that hope for the best.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from onefind import watcher as watcher_module
from onefind.cli import main as cli_main
from onefind.errors import OneFindError
from onefind.ingest import ingest_path
from onefind.jobs import EmbedderCache, JobManager
from onefind.store import Index
from onefind.watcher import (
    AUTO_SYNC_LABEL,
    AUTO_SYNC_META_KEY,
    AutoSync,
    diff_snapshots,
    get_preference,
    is_network_root,
    set_preference,
    snapshot_root,
)

FAST = 0.05
WAIT = 20.0


# -- helpers ---------------------------------------------------------------------------


def wait_until(predicate, timeout: float = WAIT, step: float = 0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    raise AssertionError("condition not met in time")


def doc_ids(db: Path) -> list[str]:
    with Index.open(db, readonly=True) as index:
        return sorted(index.document_manifest())


def body_of(db: Path, doc_id: str) -> str | None:
    with Index.open(db, readonly=True) as index:
        document = index.get_document(doc_id)
    return document["body"] if document else None


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def background_threads() -> list[str]:
    return [
        thread.name
        for thread in threading.enumerate()
        if thread.name.startswith(("onefind-autosync", "onefind-index"))
    ]


class Recorder:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.lock = threading.Lock()

    def __call__(self, event: dict) -> None:
        with self.lock:
            self.events.append(dict(event, at=time.monotonic()))

    def kinds(self) -> list[str]:
        with self.lock:
            return [event["type"] for event in self.events]

    def of(self, kind: str) -> list[dict]:
        with self.lock:
            return [event for event in self.events if event["type"] == kind]


@pytest.fixture()
def collection(tmp_path):
    notes = tmp_path / "notes"
    write(notes / "alpha.md", "# Alpha\n\nThe first note about kestrels.\n")
    write(notes / "work" / "beta.md", "# Beta\n\nQuarterly planning notes.\n")
    db = tmp_path / "collection.db"
    with Index.open(db) as index:
        ingest_path(index, notes)
    return notes, db


@pytest.fixture()
def jobs():
    manager = JobManager()
    yield manager
    manager.shutdown(timeout=10)


@pytest.fixture()
def recorder():
    return Recorder()


@pytest.fixture()
def sync(jobs, recorder):
    autosync = AutoSync(jobs, interval=FAST, on_event=recorder, cooldown=0.3)
    yield autosync
    autosync.shutdown(timeout=10)


# -- pure helpers ---------------------------------------------------------------------------


def test_snapshot_is_stat_only_and_honors_excludes(tmp_path):
    root = tmp_path / "notes"
    write(root / "keep.md", "# Keep\n\nyes\n")
    write(root / "sub" / "deep.txt", "deep\n")
    write(root / "drafts" / "skip.md", "# Draft\n")
    write(root / ".hidden" / "secret.md", "# Hidden\n")
    write(root / "node_modules" / "pkg.md", "# Package\n")
    write(root / "image.png", "not a note")
    write(root / "notes.log.md", "# Log\n")
    snapshot, readable = snapshot_root(root, ["drafts", "*.log.md"])
    assert readable
    assert sorted(snapshot) == ["keep.md", "sub/deep.txt"]
    mtime_ns, size = snapshot["keep.md"]
    assert size == (root / "keep.md").stat().st_size and mtime_ns > 0
    missing, readable = snapshot_root(tmp_path / "nope")
    assert missing == {} and readable is False
    only, readable = snapshot_root(root, sources=["keep.md", "gone.md"])
    assert readable and sorted(only) == ["keep.md"]


def test_diff_snapshots_counts_new_changed_deleted():
    old = {"a.md": (1, 1), "b.md": (1, 1), "c.md": (1, 1)}
    new = {"a.md": (1, 1), "b.md": (2, 1), "d.md": (1, 1)}
    assert diff_snapshots(old, new) == {"new": 1, "changed": 1, "deleted": 1}
    assert diff_snapshots(None, new) == {"new": 0, "changed": 0, "deleted": 0}


def test_network_roots_are_recognised_without_touching_them():
    assert is_network_root(r"\\server\share\notes")
    assert is_network_root("//server/share")
    assert is_network_root(r"\\?\UNC\server\share")
    assert not is_network_root(None)
    assert not is_network_root(str(Path.cwd()))


def test_preference_lives_in_the_collection_file(collection):
    _notes, db = collection
    with Index.open(db) as index:
        assert get_preference(index) is False  # existing collections default to off
        set_preference(index, True)
    with Index.open(db, readonly=True) as index:
        assert index.get_meta(AUTO_SYNC_META_KEY) == "on"
        assert get_preference(index) is True
    with Index.open(db) as index:
        set_preference(index, False)
        assert index.get_meta("auto_sync") == "off"


def test_interval_comes_from_the_environment(monkeypatch, jobs):
    monkeypatch.setenv("ONEFIND_AUTOSYNC_INTERVAL", "0.5")
    assert AutoSync(jobs).interval == 0.5
    monkeypatch.setenv("ONEFIND_AUTOSYNC_INTERVAL", "nonsense")
    assert AutoSync(jobs).interval == watcher_module.DEFAULT_INTERVAL
    with pytest.raises(OneFindError):
        AutoSync(jobs, interval=0)


# -- syncing --------------------------------------------------------------------------------


def test_new_changed_and_deleted_notes_converge(collection, sync, recorder, jobs):
    notes, db = collection
    status = sync.start(db)
    assert status["enabled"] is True and status["state"] in watcher_module.STATES
    assert "collection.db" not in repr(status)  # never exposes the db path
    write(notes / "gamma.md", "# Gamma\n\nA brand new note about herons.\n")
    write(notes / "alpha.md", "# Alpha\n\nRewritten: now about falcons.\n")
    (notes / "work" / "beta.md").unlink()
    wait_until(lambda: doc_ids(db) == ["alpha.md", "gamma.md"])
    wait_until(lambda: "falcons" in (body_of(db, "alpha.md") or ""))
    synced = wait_until(lambda: recorder.of("synced"))
    assert synced[0]["added"] + synced[0]["updated"] + synced[0]["removed"] >= 1
    status = wait_until(lambda: (s := sync.status(db))["last_sync_at"] and s)
    assert status["syncs"] >= 1 and status["last_changes"] is not None
    # Every job it ran was an incremental, pruning job labelled "Auto-sync".
    labelled = [job for job in jobs.list() if job["label"] == AUTO_SYNC_LABEL]
    assert labelled and all(job["options"]["prune"] and job["options"]["incremental"] for job in labelled)
    assert all(job["embed"] is False for job in labelled)  # keyword-only stays keyword-only


def test_catches_up_with_changes_made_while_it_was_off(collection, sync):
    notes, db = collection
    write(notes / "offline.md", "# Written while OneFind was closed\n\nOwls.\n")
    sync.start(db)
    wait_until(lambda: "offline.md" in doc_ids(db))


def test_a_note_being_written_is_not_indexed_half_saved(collection, sync, recorder):
    notes, db = collection
    sync.start(db)
    wait_until(lambda: sync.status(db)["last_scan_at"])
    target = notes / "growing.md"
    stop = threading.Event()
    writer_done = threading.Event()

    def keep_writing():
        text = "# Growing\n\n"
        while not stop.is_set():
            text += "more words "
            target.write_text(text, encoding="utf-8")
            time.sleep(0.01)
        writer_done.set()

    started = time.monotonic()
    thread = threading.Thread(target=keep_writing, daemon=True)
    thread.start()
    time.sleep(0.8)  # many poll intervals while the file keeps changing
    stop.set()
    writer_done.wait(5)
    finished_writing = time.monotonic()
    assert not [e for e in recorder.of("sync_started") if started <= e["at"] < finished_writing - 0.05], (
        "auto-sync started while the file was still being written"
    )
    final = target.read_text(encoding="utf-8")
    wait_until(lambda: "growing.md" in doc_ids(db))
    wait_until(lambda: (body_of(db, "growing.md") or "").strip() == final.split("\n\n", 1)[1].strip())


def test_excluded_folders_are_never_synced(collection, sync, recorder):
    notes, db = collection
    with Index.open(db) as index:
        ingest_path(index, notes, exclude=["drafts"])  # saved on the folder
    sync.start(db)
    wait_until(lambda: sync.status(db)["last_scan_at"])
    write(notes / "drafts" / "wip.md", "# Work in progress\n")
    write(notes / ".obsidian" / "workspace.md", "# Editor state\n")
    write(notes / "node_modules" / "readme.md", "# Package\n")
    time.sleep(0.6)
    assert not recorder.of("changes")
    write(notes / "visible.md", "# Visible\n\nThis one counts.\n")
    wait_until(lambda: "visible.md" in doc_ids(db))
    assert not any(doc.startswith(("drafts/", ".obsidian/", "node_modules/")) for doc in doc_ids(db))


def test_touching_a_file_without_changing_it_runs_no_job(collection, sync, recorder, jobs):
    notes, db = collection
    sync.start(db)
    wait_until(lambda: sync.status(db)["last_scan_at"])
    time.sleep(0.3)
    later = time.time() + 5
    os.utime(notes / "alpha.md", (later, later))
    time.sleep(0.8)
    assert not recorder.of("sync_started")
    assert not [job for job in jobs.list() if job["label"] == AUTO_SYNC_LABEL]


class GatedJobs:
    """A JobManager stand-in that can pretend a manual job is running."""

    def __init__(self, inner: JobManager) -> None:
        self.inner = inner
        self.manual = threading.Event()
        self.fail_next = 0
        self.attempts = 0

    def active_for(self, db_path):
        if self.manual.is_set():
            return {"id": "manual", "label": "Notes", "status": "running"}
        return self.inner.active_for(db_path)

    def start_index(self, db_path, root, **options):
        self.attempts += 1
        if self.fail_next:
            self.fail_next -= 1
            return {"id": "broken", "status": "failed"}
        return self.inner.start_index(db_path, root, **options)

    def wait(self, job_id, timeout=None):
        if job_id == "broken":
            return {"id": job_id, "status": "failed", "error": {"message": "Disk on fire."}}
        return self.inner.wait(job_id, timeout)

    def get(self, job_id):
        if job_id == "broken":
            return {"id": job_id, "status": "failed"}
        return self.inner.get(job_id)

    def cancel(self, job_id):
        if job_id == "broken":
            return {"id": job_id, "status": "failed"}
        return self.inner.cancel(job_id)


def test_waits_while_a_manual_job_runs(collection, jobs, recorder):
    notes, db = collection
    gated = GatedJobs(jobs)
    gated.manual.set()
    sync = AutoSync(gated, interval=FAST, on_event=recorder)
    try:
        sync.start(db)
        write(notes / "later.md", "# Later\n\nWaits for the manual job.\n")
        status = wait_until(lambda: (s := sync.status(db))["state"] == "paused" and s)
        assert "Waiting for indexing" in status["message"]
        time.sleep(0.4)
        assert gated.attempts == 0 and "later.md" not in doc_ids(db)
        gated.manual.clear()
        wait_until(lambda: "later.md" in doc_ids(db))
    finally:
        sync.shutdown()


def test_errors_are_reported_and_retried_with_backoff(collection, jobs, recorder):
    notes, db = collection
    gated = GatedJobs(jobs)
    gated.fail_next = 4
    sync = AutoSync(gated, interval=FAST, on_event=recorder)
    try:
        sync.start(db)
        write(notes / "retry.md", "# Retry\n\nEventually indexed.\n")
        status = wait_until(lambda: (s := sync.status(db))["state"] == "error" and s)
        assert "Disk on fire." in status["message"]
        # Exponential backoff: 4 failures take 0.1 + 0.2 + 0.4 + 0.8 s of waiting,
        # far fewer attempts than one per 50 ms poll.
        time.sleep(0.5)
        assert gated.attempts <= 4
        wait_until(lambda: "retry.md" in doc_ids(db))
        wait_until(lambda: sync.status(db)["state"] == "idle")
        assert recorder.of("error")
    finally:
        sync.shutdown()


def test_a_missing_folder_keeps_its_notes(collection, sync):
    notes, db = collection
    sync.start(db)
    wait_until(lambda: sync.status(db)["last_scan_at"])
    moved = notes.with_name("moved")
    wait_until(lambda: _try_rename(notes, moved))
    status = wait_until(lambda: (s := sync.status(db))["message"] and s)
    assert "not available" in status["message"]
    time.sleep(0.3)
    assert doc_ids(db) == ["alpha.md", "work/beta.md"]


def _try_rename(source: Path, target: Path) -> bool:
    try:
        source.rename(target)
    except PermissionError:  # Windows: a scan has the folder open right now
        return False
    return True


def test_a_folder_that_suddenly_empties_is_not_pruned(tmp_path, sync):
    notes = tmp_path / "notes"
    for number in range(6):
        write(notes / f"note-{number}.md", f"# Note {number}\n\nBody {number}.\n")
    db = tmp_path / "c.db"
    with Index.open(db) as index:
        ingest_path(index, notes)
    sync.start(db)
    wait_until(lambda: sync.status(db)["last_scan_at"])
    for path in notes.glob("*.md"):
        path.unlink()
    status = wait_until(lambda: "seems to be gone" in (s := sync.status(db))["message"] and s)
    assert status["state"] == "idle"
    time.sleep(0.3)
    assert len(doc_ids(db)) == 6


def test_network_folders_are_skipped_unless_allowed(collection, sync, monkeypatch):
    notes, db = collection
    touched: list[str] = []
    real = watcher_module.snapshot_root

    def guarded(root, *args, **kwargs):
        touched.append(str(root))
        return real(root, *args, **kwargs)

    monkeypatch.setattr(watcher_module, "snapshot_root", guarded)
    sync.start(db, [{"path": r"\\nas.invalid\share\notes"}, {"path": str(notes)}])
    status = wait_until(lambda: (s := sync.status(db))["skipped_roots"] == 1 and s)
    assert "network" in status["message"]
    assert all(not path.startswith("\\\\") for path in touched)


def test_large_trees_are_polled_less_often(collection, jobs, monkeypatch):
    _notes, db = collection
    real = watcher_module.snapshot_root

    def slow(*args, **kwargs):
        time.sleep(0.03)
        return real(*args, **kwargs)

    monkeypatch.setattr(watcher_module, "snapshot_root", slow)
    sync = AutoSync(jobs, interval=FAST)
    try:
        sync.start(db)
        status = wait_until(lambda: (s := sync.status(db))["interval_s"] >= 1.0 and s)
        assert status["interval_s"] >= 0.03 * watcher_module.CPU_FACTOR * 0.9
    finally:
        sync.shutdown()


def test_preempt_keeps_the_watcher_quiet_for_a_moment(collection, sync, recorder):
    notes, db = collection
    sync.start(db)
    wait_until(lambda: sync.status(db)["last_scan_at"])
    assert sync.preempt(db, cooldown=0.6) is True
    write(notes / "after.md", "# After\n\nWaits for the cooldown.\n")
    status = wait_until(lambda: (s := sync.status(db))["state"] == "paused" and s)
    assert "Paused" in status["message"]
    time.sleep(0.2)
    assert "after.md" not in doc_ids(db)
    wait_until(lambda: "after.md" in doc_ids(db))


def test_restart_is_idempotent_and_one_thread_per_collection(collection, sync):
    _notes, db = collection
    sync.start(db)
    sync.start(db)
    sync.start(str(db))
    assert len([name for name in background_threads() if name.startswith("onefind-autosync")]) == 1
    assert sync.running(db) and len(sync.watched()) == 1


def test_stop_and_shutdown_leave_no_threads(collection, jobs):
    notes, db = collection
    sync = AutoSync(jobs, interval=FAST)
    sync.start(db)
    write(notes / "x.md", "# X\n\nx\n")
    wait_until(lambda: "x.md" in doc_ids(db))
    stopped = sync.stop(db)
    assert stopped["enabled"] is False and not sync.running(db)
    assert sync.status(db)["enabled"] is False
    sync.start(db)
    sync.shutdown()
    jobs.shutdown()
    wait_until(lambda: not background_threads(), timeout=10)
    with pytest.raises(OneFindError):
        sync.start(db)


def test_stops_by_itself_when_the_collection_file_disappears(collection, sync, recorder):
    _notes, db = collection
    sync.start(db)
    wait_until(lambda: sync.status(db)["last_scan_at"])

    def remove_files() -> bool:
        try:
            for path in (db, Path(f"{db}-wal"), Path(f"{db}-shm")):
                path.unlink(missing_ok=True)
        except PermissionError:
            return False
        return True

    wait_until(remove_files)
    wait_until(lambda: not sync.running(db))
    status = sync.status(db)
    assert status["enabled"] is False and "gone" in status["message"]
    assert "stopped" in recorder.kinds()


def test_status_of_an_unwatched_collection(tmp_path, jobs):
    sync = AutoSync(jobs, interval=FAST)
    status = sync.status(tmp_path / "unknown.db")
    assert status == {
        "enabled": False,
        "state": "idle",
        "last_scan_at": None,
        "last_sync_at": None,
        "pending": {"new": 0, "changed": 0, "deleted": 0},
        "message": "",
        "interval_s": FAST,
        "syncs": 0,
        "last_changes": None,
        "progress": None,
        "skipped_roots": 0,
    }
    with pytest.raises(OneFindError):
        sync.start(tmp_path / "unknown.db")


# -- HTTP API -------------------------------------------------------------------------------

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from onefind.serve import create_app  # noqa: E402

CSRF = {"X-OneFind-Request": "1"}


def search_ids(client, query: str) -> list[str]:
    response = client.post("/api/search", json={"query": query, "mode": "lexical", "k": 10})
    if response.status_code != 200:
        return []
    return [item["doc_id"] for item in response.json()["results"]]


def test_api_toggle_persists_and_syncs(collection):
    notes, db = collection
    app = create_app(db, autosync_interval=FAST)
    with TestClient(app) as client:
        status = client.get("/api/autosync").json()
        assert status["enabled"] is False and status["state"] == "idle"
        stats = client.get("/api/stats").json()
        assert stats["auto_sync"]["enabled"] is False
        assert str(db) not in client.get("/api/stats").text
        assert client.post("/api/autosync", json={"enabled": True}).status_code == 403  # CSRF
        response = client.post("/api/autosync", json={"enabled": True}, headers=CSRF)
        assert response.status_code == 200 and response.json()["enabled"] is True
        assert app.state.autosync.running(db)
        write(notes / "heron.md", "# Heron\n\nA grey heron by the canal.\n")
        wait_until(lambda: "heron.md" in search_ids(client, "heron"))
        stats = wait_until(lambda: (s := client.get("/api/stats").json())["auto_sync"]["syncs"] and s)
        assert stats["status"] == "ready" and stats["auto_sync"]["last_sync_at"]
        assert str(db) not in client.get("/api/stats").text
        response = client.post("/api/autosync", json={"enabled": False}, headers=CSRF)
        assert response.json()["enabled"] is False
        assert not app.state.autosync.running(db)
    with Index.open(db, readonly=True) as index:
        assert index.get_meta("auto_sync") == "off"
    assert not [name for name in background_threads() if name.startswith("onefind-autosync")]


def test_serve_honors_the_saved_preference(collection):
    notes, db = collection
    with Index.open(db) as index:
        set_preference(index, True)
    app = create_app(db, autosync_interval=FAST)
    with TestClient(app) as client:
        assert app.state.autosync.running(db)
        assert client.get("/api/autosync").json()["enabled"] is True
        write(notes / "owl.md", "# Owl\n\nA tawny owl at dusk.\n")
        wait_until(lambda: "owl.md" in search_ids(client, "tawny"))
    assert not app.state.autosync.running(db)

    quiet = create_app(db, autosync=False, autosync_interval=FAST)
    with TestClient(quiet) as client:
        assert not quiet.state.autosync.running(db)
        status = client.get("/api/autosync").json()
        assert status["enabled"] is True and status["state"] == "paused"
        assert "--no-auto-sync" in status["message"]


def test_auto_sync_jobs_do_not_turn_the_collection_into_indexing(collection):
    _notes, db = collection
    app = create_app(db, autosync_interval=FAST)
    with TestClient(app) as client:
        runtime = app.state.runtime
        runtime.active_job = lambda: {"id": "x", "label": AUTO_SYNC_LABEL, "status": "running",
                                      "phase": "read", "done": 1, "total": 2, "db": str(db)}
        stats = client.get("/api/stats").json()
        assert stats["status"] == "ready" and stats["job"] is None
        runtime.active_job = lambda: {"id": "y", "label": "Notes", "status": "running",
                                      "phase": "read", "done": 1, "total": 2, "db": str(db)}
        assert client.get("/api/stats").json()["status"] == "indexing"


def test_index_request_can_turn_auto_sync_on(collection):
    notes, db = collection
    app = create_app(db, autosync_interval=FAST)
    with TestClient(app) as client:
        response = client.post(
            "/api/index", json={"path": str(notes), "embed": False, "auto_sync": True}, headers=CSRF
        )
        assert response.status_code == 202
        app.state.jobs.wait(response.json()["id"], timeout=WAIT)
        assert client.get("/api/autosync").json()["enabled"] is True
        assert app.state.autosync.running(db)
        response = client.post("/api/index", json={"path": str(notes), "embed": False}, headers=CSRF)
        app.state.jobs.wait(response.json()["id"], timeout=WAIT)
        assert client.get("/api/autosync").json()["enabled"] is True  # null keeps it


def test_switching_collections_follows_the_active_one(tmp_path):
    home = tmp_path / "home"
    first_notes = tmp_path / "first"
    second_notes = tmp_path / "second"
    write(first_notes / "a.md", "# A\n\nfirst\n")
    write(second_notes / "b.md", "# B\n\nsecond\n")
    app = create_app(home=home, autosync_interval=FAST)
    with TestClient(app) as client:
        for name, folder in (("First", first_notes), ("Second", second_notes)):
            assert client.post("/api/collections", json={"name": name}, headers=CSRF).status_code == 201
            client.post("/api/collections/active", json={"name": name}, headers=CSRF)
            job = client.post("/api/index", json={"path": str(folder), "embed": False,
                                                  "auto_sync": name == "First"},
                              headers=CSRF).json()
            app.state.jobs.wait(job["id"], timeout=WAIT)
        # Second is active and has auto-sync off: nothing is watched.
        assert client.get("/api/autosync").json()["enabled"] is False
        assert app.state.autosync.watched() == []
        client.post("/api/collections/active", json={"name": "First"}, headers=CSRF)
        assert len(app.state.autosync.watched()) == 1
        write(first_notes / "c.md", "# C\n\nkingfisher\n")
        wait_until(lambda: "c.md" in search_ids(client, "kingfisher"))
        client.post("/api/collections/active", json={"name": "Second"}, headers=CSRF)
        assert app.state.autosync.watched() == []


def test_a_manual_index_cancels_a_running_auto_sync_job(tmp_path):
    class Gate:
        open = threading.Event()
        entered = threading.Event()

    class SlowEmbedder:
        revision = "slow-1"
        dimension = 3

        def __init__(self, name="slow-mini"):
            self.name = name

        def encode_documents(self, texts):
            Gate.entered.set()
            Gate.open.wait(WAIT)
            return np.stack([self._vector(text) for text in texts])

        def encode_query(self, text):
            return self._vector(text)

        @staticmethod
        def _vector(text):
            digest = hashlib.sha256(text.encode("utf-8")).digest()[:3]
            vector = np.frombuffer(digest, dtype=np.uint8).astype(np.float32) + 1.0
            return vector / np.linalg.norm(vector)

    notes = tmp_path / "notes"
    write(notes / "a.md", "# A\n\nalpha\n")
    db = tmp_path / "v.db"
    with Index.open(db) as index:
        if not index.vec_ready:
            pytest.skip("sqlite-vec is not available")
        Gate.open.set()
        index.attach_embedder(SlowEmbedder())
        ingest_path(index, notes)
        assert isinstance(index.get_meta("model_name"), str)
        set_preference(index, True)
    Gate.open.clear()
    Gate.entered.clear()
    cache = EmbedderCache(factory=lambda name: SlowEmbedder(name))
    app = create_app(db, embedders=cache, autosync_interval=FAST)
    with TestClient(app) as client:
        write(notes / "b.md", "# B\n\nbeta\n")
        wait_until(lambda: Gate.entered.is_set())
        running = app.state.jobs.active_for(db)
        assert running and running["label"] == AUTO_SYNC_LABEL
        assert client.get("/api/stats").json()["status"] != "indexing"
        Gate.open.set()  # let the cancelled batch finish quickly
        response = client.post("/api/index", json={"path": str(notes)}, headers=CSRF)
        assert response.status_code == 202, response.text
        assert response.json()["label"] != AUTO_SYNC_LABEL
        app.state.jobs.wait(response.json()["id"], timeout=WAIT)
        cancelled = [job for job in app.state.jobs.list() if job["label"] == AUTO_SYNC_LABEL]
        assert cancelled and cancelled[-1]["status"] in {"cancelled", "succeeded"}
        assert "b.md" in search_ids(client, "beta")


# -- CLI ------------------------------------------------------------------------------------


def test_cli_watch_syncs_until_the_duration_ends(collection, capsys):
    notes, db = collection

    def later():
        time.sleep(0.6)
        write(notes / "swift.md", "# Swift\n\nA common swift over the roofs.\n")

    thread = threading.Thread(target=later, daemon=True)
    thread.start()
    code = cli_main(["watch", "--db", str(db), "--interval", "0.05", "--duration", "3"])
    thread.join()
    out = capsys.readouterr().out
    assert code == 0
    assert "watching" in out and "stopped watching" in out
    assert "synced (1 added" in out
    assert "swift.md" in doc_ids(db)
    assert not [name for name in background_threads() if name.startswith("onefind-autosync")]


def test_cli_watch_set_saves_the_preference_and_status_shows_it(collection, capsys):
    _notes, db = collection
    assert cli_main(["watch", "--db", str(db), "--set", "on"]) == 0
    with Index.open(db, readonly=True) as index:
        assert get_preference(index) is True
    assert cli_main(["status", "--db", str(db), "--no-scan"]) == 0
    assert "auto-sync: on" in capsys.readouterr().out
    assert cli_main(["watch", "--db", str(db), "--set", "off"]) == 0
    with Index.open(db, readonly=True) as index:
        assert get_preference(index) is False


def test_cli_watch_needs_a_folder(tmp_path, capsys):
    db = tmp_path / "empty.db"
    Index.open(db).close()
    assert cli_main(["watch", "--db", str(db), "--duration", "0"]) == 4
    assert "no folders to watch" in capsys.readouterr().err


def test_cli_serve_accepts_no_auto_sync():
    from onefind.cli import build_parser

    args = build_parser().parse_args(["serve", "--no-auto-sync"])
    assert args.no_auto_sync is True
