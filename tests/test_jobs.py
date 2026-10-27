"""Background indexing jobs and the shared embedder cache (contract section 5)."""

from __future__ import annotations

import hashlib
import sys
import threading
import time
import types

import numpy as np
import pytest

import onefind.ingest as ingest_module
import onefind.jobs as jobs_module
from onefind.embed import DEFAULT_MODEL
from onefind.errors import DataError, UsageError
from onefind.jobs import (
    STEP_NAMES,
    EmbedderCache,
    JobManager,
    JobNotFoundError,
    model_stack_available,
)
from onefind.store import Index

WAIT = 30.0  # generous upper bound; jobs on tiny corpora finish in milliseconds


class FakeEmbedder:
    revision = "test-revision-1"
    dimension = 3

    def __init__(self, name: str = "fake-mini") -> None:
        self.name = name
        self.calls = 0

    def encode_documents(self, texts):
        self.calls += 1
        return np.stack([self._vector(text) for text in texts])

    def encode_query(self, text):
        return self._vector(text)

    @staticmethod
    def _vector(text):
        digest = hashlib.sha256(text.encode("utf-8")).digest()[:3]
        vector = np.frombuffer(digest, dtype=np.uint8).astype(np.float32) + 1.0
        return vector / np.linalg.norm(vector)


class GatedEmbedder(FakeEmbedder):
    """Blocks inside the first encode until the test opens the gate."""

    def __init__(self, name: str = "fake-mini") -> None:
        super().__init__(name)
        self.entered = threading.Event()
        self.gate = threading.Event()

    def encode_documents(self, texts):
        self.entered.set()
        assert self.gate.wait(WAIT), "test never released the gate"
        return super().encode_documents(texts)


def fake_cache(embedder_cls=FakeEmbedder):
    """An EmbedderCache whose factory builds fakes (keeps the instances)."""
    created: list = []

    def factory(name):
        instance = embedder_cls(name)
        created.append(instance)
        return instance

    cache = EmbedderCache(factory=factory)
    cache.created = created
    return cache


def write_notes(folder, count, *, prefix="note"):
    folder.mkdir(parents=True, exist_ok=True)
    for number in range(count):
        (folder / f"{prefix}-{number:03d}.md").write_text(
            f"# {prefix.title()} {number}\n\nRollback plan number {number} for the release.\n",
            encoding="utf-8",
        )
    return folder


def run(manager, db, root, **options):
    """Start a job and wait for it to settle."""
    return manager.wait(manager.start_index(db, root, **options)["id"], WAIT)


def assert_settled(job):
    assert job["status"] in {"succeeded", "failed", "cancelled"}
    assert job["finished_at"] is not None
    assert not any(step["status"] == "active" for step in job["steps"])


def steps_of(job):
    return {step["name"]: step["status"] for step in job["steps"]}


@pytest.fixture
def corpus(tmp_path):
    return write_notes(tmp_path / "notes", 5)


@pytest.fixture
def db(tmp_path):
    return tmp_path / "collection.db"


@pytest.fixture
def no_model_stack(monkeypatch):
    monkeypatch.setattr(jobs_module, "model_stack_available", lambda: False)


# -- success paths ------------------------------------------------------------------


def test_lexical_job_succeeds_and_reports_result(corpus, db, no_model_stack):
    manager = JobManager(EmbedderCache())
    started = manager.start_index(db, corpus, label="Notes")

    assert started["kind"] == "index"
    assert started["status"] in {"queued", "running", "succeeded"}
    assert started["label"] == "Notes"
    assert [step["name"] for step in started["steps"]] == list(STEP_NAMES)
    assert {
        "id", "kind", "db", "root", "status", "phase", "done", "total", "message",
        "started_at", "finished_at", "result", "error", "steps",
    } <= set(started)

    job = manager.wait(started["id"], WAIT)
    assert job["status"] == "succeeded", job["error"]
    assert job["phase"] == "done"
    assert job["error"] is None
    assert job["embed"] is False
    assert steps_of(job) == {"scan": "done", "index": "done", "vectors": "skipped", "ready": "done"}
    assert job["result"]["added"] == 5
    assert job["result"]["files_indexed"] == 5
    assert job["result"]["total_documents"] == 5
    assert job["message"].startswith("Indexing finished")
    assert job["health"]["documents"] == 5 and job["health"]["vectors"] is None
    assert job["counts"]["found"] == 5 and job["counts"]["read"] == 5
    assert_settled(job)

    with Index.open(db) as index:
        assert index.count("documents") == 5
        assert not index.has_vectors()
        assert index.last_run()["status"] == "succeeded"


def test_vector_job_embeds_with_cached_model(corpus, db):
    cache = fake_cache()
    manager = JobManager(cache)
    job = run(manager, db, corpus, embed=True, model_name="fake-mini")

    assert job["status"] == "succeeded", job["error"]
    assert job["embed"] is True and job["model"] == "fake-mini"
    assert steps_of(job) == {"scan": "done", "index": "done", "vectors": "done", "ready": "done"}
    assert job["health"]["vectors"] == 5 and job["health"]["ok"]
    assert job["counts"]["embedded"] == 5
    assert cache.loaded("fake-mini")

    # A second job reuses the cached model instead of loading it again.
    write_notes(corpus, 2, prefix="extra")
    second = manager.wait(manager.start_index(db, corpus)["id"], WAIT)
    assert second["status"] == "succeeded", second["error"]
    assert second["result"]["added"] == 2 and second["result"]["unchanged"] == 5
    assert len(cache.created) == 1
    with Index.open(db) as index:
        assert index.health()["vectors"] == 7 and index.health()["ok"]


def test_embed_none_uses_model_when_stack_available(corpus, db):
    manager = JobManager(fake_cache())
    job = manager.wait(manager.start_index(db, corpus, model_name="fake-mini")["id"], WAIT)
    assert job["status"] == "succeeded", job["error"]
    assert job["embed"] is True
    assert steps_of(job)["vectors"] == "done"


def test_embed_none_without_stack_stays_lexical(corpus, db, no_model_stack):
    manager = JobManager(EmbedderCache())  # no factory: availability = stack check
    job = manager.wait(manager.start_index(db, corpus)["id"], WAIT)
    assert job["status"] == "succeeded"
    assert job["embed"] is False and job["model"] is None
    assert steps_of(job)["vectors"] == "skipped"


def test_embed_none_follows_existing_vectors_even_without_stack(corpus, db, no_model_stack):
    cache = fake_cache()
    cache.available = lambda: False  # stack "missing", but the index has vectors
    manager = JobManager(cache)
    first = run(manager, db, corpus, embed=True, model_name="fake-mini")
    assert first["status"] == "succeeded"
    write_notes(corpus, 1, prefix="late")
    job = manager.wait(manager.start_index(db, corpus)["id"], WAIT)
    assert job["status"] == "succeeded", job["error"]
    assert job["embed"] is True
    assert job["model"] == "fake-mini"  # model name comes from the index meta
    with Index.open(db) as index:
        assert index.health()["ok"] and index.health()["vectors"] == 6


def test_embed_false_on_vector_index_is_forced_and_noted(corpus, db):
    manager = JobManager(fake_cache())
    run(manager, db, corpus, embed=True, model_name="fake-mini")
    write_notes(corpus, 3, prefix="new")

    job = manager.wait(manager.start_index(db, corpus, embed=False)["id"], WAIT)
    assert job["status"] == "succeeded", job["error"]
    assert job["embed"] is True
    assert "already has meaning-search vectors" in job["message"]
    with Index.open(db) as index:
        health = index.health()
        assert health["ok"] and health["vectors"] == health["documents"] == 8


def test_existing_lexical_index_gets_backfilled_when_embedding(corpus, db, no_model_stack):
    lexical = JobManager(EmbedderCache())
    assert lexical.wait(lexical.start_index(db, corpus)["id"], WAIT)["status"] == "succeeded"

    manager = JobManager(fake_cache())
    job = run(manager, db, corpus, embed=True, model_name="fake-mini")
    assert job["status"] == "succeeded", job["error"]
    assert job["result"]["unchanged"] == 5
    with Index.open(db) as index:
        assert index.health()["vectors"] == 5 and index.health()["ok"]


def test_single_file_and_prune(tmp_path, db, no_model_stack):
    folder = write_notes(tmp_path / "notes", 3)
    manager = JobManager(EmbedderCache())
    single = manager.wait(manager.start_index(db, folder / "note-000.md")["id"], WAIT)
    assert single["status"] == "succeeded", single["error"]
    assert single["result"]["added"] == 1

    full = manager.wait(manager.start_index(db, folder)["id"], WAIT)
    assert full["result"]["total_documents"] == 3  # single-file doc id differs from folder ids
    (folder / "note-002.md").unlink()
    pruned = manager.wait(manager.start_index(db, folder)["id"], WAIT)
    assert pruned["status"] == "succeeded"
    assert pruned["result"]["removed"] == 1


def test_exclude_and_options_pass_through(tmp_path, db, no_model_stack):
    folder = write_notes(tmp_path / "notes", 2)
    write_notes(folder / "drafts", 2, prefix="draft")
    manager = JobManager(EmbedderCache())
    job = manager.wait(
        manager.start_index(db, folder, exclude=["drafts"], prune=False, incremental=False)["id"],
        WAIT,
    )
    assert job["status"] == "succeeded", job["error"]
    assert job["result"]["added"] == 2
    assert job["options"] == {"exclude": ["drafts"], "prune": False, "incremental": False}


# -- progress & steps -----------------------------------------------------------------


def record_progress(manager, monkeypatch):
    snapshots: list[tuple[dict, dict]] = []
    original = JobManager._on_progress

    def spy(self, job, event):
        original(self, job, event)
        snapshots.append((dict(event), self.get(job.data["id"])))

    monkeypatch.setattr(JobManager, "_on_progress", spy)
    return snapshots


def test_progress_is_monotonic_per_phase_and_steps_advance(tmp_path, db, monkeypatch):
    monkeypatch.setattr(ingest_module, "BATCH_SIZE", 4)
    folder = write_notes(tmp_path / "notes", 13)
    manager = JobManager(fake_cache())
    snapshots = record_progress(manager, monkeypatch)
    job = run(manager, db, folder, embed=True, model_name="fake-mini")
    assert job["status"] == "succeeded", job["error"]
    assert snapshots

    last_done: dict[str, int] = {}
    for event, snapshot in snapshots:
        phase = event["phase"]
        assert snapshot["phase"] == phase
        assert snapshot["done"] == event["done"]
        assert snapshot["done"] >= last_done.get(phase, 0), f"{phase} went backwards"
        last_done[phase] = snapshot["done"]
        if snapshot["total"] is not None:
            assert snapshot["done"] <= snapshot["total"]
        assert snapshot["status"] == "running"
    phases = [event["phase"] for event, _ in snapshots]
    assert phases[0] == "scan" and phases[-1] == "done"
    assert {"read", "embed"} <= set(phases)

    rank = {"pending": 0, "active": 1, "done": 2}
    previous = {name: 0 for name in STEP_NAMES}
    for _event, snapshot in snapshots:
        steps = steps_of(snapshot)
        for name in STEP_NAMES:
            assert rank[steps[name]] >= previous[name], f"step {name} regressed"
            previous[name] = rank[steps[name]]
        # steps finish in order: a later step is never done before an earlier one
        statuses = [rank[steps[name]] for name in STEP_NAMES]
        done_flags = [value == 2 for value in statuses]
        assert done_flags == sorted(done_flags, reverse=True)

    first_read = next(s for e, s in snapshots if e["phase"] == "read")
    assert steps_of(first_read)["scan"] == "done" and steps_of(first_read)["index"] == "active"
    first_embed = next(s for e, s in snapshots if e["phase"] == "embed")
    assert steps_of(first_embed)["vectors"] == "active"
    final_event = snapshots[-1][1]
    assert steps_of(final_event)["ready"] == "active"
    assert job["counts"] == {"found": 13, "read": 13, "saved": 13, "embedded": 13}


def test_model_phase_is_reported_while_loading(corpus, db):
    loading = threading.Event()
    release = threading.Event()

    def slow_factory(name):
        loading.set()
        assert release.wait(WAIT)
        return FakeEmbedder(name)

    manager = JobManager(EmbedderCache(factory=slow_factory))
    started = manager.start_index(db, corpus, embed=True, model_name="fake-mini")
    assert loading.wait(WAIT)
    during = manager.get(started["id"])
    assert during["status"] == "running"
    assert during["phase"] == "model"
    assert during["model"] == "fake-mini"
    assert "model" in during["message"].lower()
    assert manager.embedders.loaded("fake-mini") is False
    release.set()
    job = manager.wait(started["id"], WAIT)
    assert job["status"] == "succeeded", job["error"]
    assert manager.embedders.loaded("fake-mini") is True


# -- cancellation ---------------------------------------------------------------------


def test_cancel_mid_run_keeps_committed_batches(tmp_path, db, monkeypatch):
    monkeypatch.setattr(ingest_module, "BATCH_SIZE", 2)
    folder = write_notes(tmp_path / "notes", 8)
    cache = fake_cache(GatedEmbedder)
    manager = JobManager(cache)
    started = manager.start_index(db, folder, embed=True, model_name="fake-mini")

    # Wait until the first batch is being embedded, then cancel.
    deadline = time.monotonic() + WAIT
    while not cache.created and time.monotonic() < deadline:
        time.sleep(0.005)
    embedder = cache.created[0]
    assert embedder.entered.wait(WAIT)
    cancelling = manager.cancel(started["id"])
    assert cancelling["status"] == "running"
    assert "Cancelling" in cancelling["message"]
    embedder.gate.set()

    job = manager.wait(started["id"], WAIT)
    assert job["status"] == "cancelled"
    assert job["error"] is None
    assert job["result"] is None
    assert "cancelled" in job["message"].lower()
    assert_settled(job)
    assert steps_of(job)["scan"] == "done"
    assert steps_of(job)["index"] == "pending"  # interrupted, not failed
    with Index.open(db) as index:
        assert index.count("documents") == 2  # the committed first batch survives
        assert index.health()["ok"]
        assert index.last_run()["status"] == "cancelled"

    # Cancelling a finished job is a harmless no-op.
    again = manager.cancel(started["id"])
    assert again["status"] == "cancelled"

    # The next run resumes incrementally.
    resumed = manager.wait(manager.start_index(db, folder)["id"], WAIT)
    assert resumed["status"] == "succeeded", resumed["error"]
    assert resumed["result"]["unchanged"] == 2 and resumed["result"]["added"] == 6


def test_cancel_during_model_load_stops_before_indexing(corpus, db):
    loading = threading.Event()
    release = threading.Event()

    def slow_factory(name):
        loading.set()
        assert release.wait(WAIT)
        return FakeEmbedder(name)

    manager = JobManager(EmbedderCache(factory=slow_factory))
    started = manager.start_index(db, corpus, embed=True, model_name="fake-mini")
    assert loading.wait(WAIT)
    manager.cancel(started["id"])
    release.set()
    job = manager.wait(started["id"], WAIT)
    assert job["status"] == "cancelled"
    with Index.open(db) as index:
        assert index.count("documents") == 0
    # the model stays cached for the next job even though this one was cancelled
    assert manager.embedders.loaded("fake-mini")


# -- failures ---------------------------------------------------------------------------


def test_missing_folder_fails_with_friendly_error(tmp_path, db, no_model_stack):
    manager = JobManager(EmbedderCache())
    missing = tmp_path / "does-not-exist"
    job = manager.wait(manager.start_index(db, missing)["id"], WAIT)
    assert job["status"] == "failed"
    assert job["error"]["code"] == "path_not_found"
    assert "does not exist" in job["error"]["message"]
    assert job["error"]["action"] == "index"
    assert job["message"] == job["error"]["message"]
    assert job["result"] is None
    assert steps_of(job)["scan"] == "failed"
    assert_settled(job)
    assert "Traceback" not in job["error"]["message"]


def test_empty_folder_fails_with_nothing_to_index(tmp_path, db, no_model_stack):
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "photo.png").write_bytes(b"\x89PNG")
    manager = JobManager(EmbedderCache())
    job = manager.wait(manager.start_index(db, empty)["id"], WAIT)
    assert job["status"] == "failed"
    assert job["error"]["code"] == "nothing_to_index"
    assert ".md" in job["error"]["message"]


def test_model_unavailable_suggests_lexical(corpus, db):
    def broken_factory(name):
        raise ImportError("sentence-transformers not installed")

    manager = JobManager(EmbedderCache(factory=broken_factory))
    job = manager.wait(manager.start_index(db, corpus, embed=True)["id"], WAIT)
    assert job["status"] == "failed"
    assert job["error"]["code"] == "model_unavailable"
    assert "keyword search only" in job["error"]["message"]
    assert job["error"]["action"] == "switch_mode"
    assert job["error"]["retryable"] is True
    assert not manager.embedders.loaded(DEFAULT_MODEL)  # failures are not cached


def test_model_mismatch_is_reported(corpus, db):
    manager = JobManager(fake_cache())
    run(manager, db, corpus, embed=True, model_name="fake-mini")
    job = manager.wait(manager.start_index(db, corpus, model_name="other-model")["id"], WAIT)
    assert job["status"] == "failed"
    assert job["error"]["code"] == "model_mismatch"
    with Index.open(db) as index:
        assert index.get_meta("model_name") == "fake-mini"
        assert index.health()["ok"]


def test_vectors_unavailable_when_sqlite_vec_missing(corpus, db, monkeypatch):
    import onefind.store as store_module

    monkeypatch.setattr(store_module, "_try_load_vec", lambda conn: False)
    manager = JobManager(fake_cache())
    explicit = manager.wait(manager.start_index(db, corpus, embed=True)["id"], WAIT)
    assert explicit["status"] == "failed"
    assert explicit["error"]["code"] == "vectors_unavailable"
    # embed=None falls back to lexical instead of failing
    auto = manager.wait(manager.start_index(db, corpus)["id"], WAIT)
    assert auto["status"] == "succeeded", auto["error"]
    assert auto["embed"] is False


def test_corrupt_database_fails_cleanly(corpus, tmp_path, no_model_stack):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"this is not sqlite" * 100)
    manager = JobManager(EmbedderCache())
    job = manager.wait(manager.start_index(bad, corpus)["id"], WAIT)
    assert job["status"] == "failed"
    assert job["error"]["code"] == "collection_unreadable"
    assert job["error"]["action"] == "repair"


def test_permission_error_is_translated(corpus, db, monkeypatch, no_model_stack):
    def denied(*_args, **_kwargs):
        raise PermissionError(13, "Access is denied", str(corpus))

    monkeypatch.setattr(ingest_module, "list_files", denied)
    manager = JobManager(EmbedderCache())
    job = manager.wait(manager.start_index(db, corpus)["id"], WAIT)
    assert job["status"] == "failed"
    assert job["error"]["code"] == "permission_denied"
    assert "not allowed to read" in job["error"]["message"]


def test_unexpected_exception_never_leaves_running(corpus, db, monkeypatch, no_model_stack):
    def boom(*_args, **_kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(ingest_module, "ingest_path", boom)
    manager = JobManager(EmbedderCache())
    job = manager.wait(manager.start_index(db, corpus)["id"], WAIT)
    assert job["status"] == "failed"
    assert job["error"]["code"] == "internal_error"
    assert "kaboom" in job["error"]["message"]
    assert_settled(job)
    assert manager.list(active_only=True) == []


# -- concurrency rules & bookkeeping ------------------------------------------------


def test_second_job_on_same_db_is_rejected(tmp_path, db, monkeypatch):
    monkeypatch.setattr(ingest_module, "BATCH_SIZE", 2)
    folder = write_notes(tmp_path / "notes", 4)
    cache = fake_cache(GatedEmbedder)
    manager = JobManager(cache)
    first = manager.start_index(db, folder, embed=True, model_name="fake-mini")
    deadline = time.monotonic() + WAIT
    while not cache.created and time.monotonic() < deadline:
        time.sleep(0.005)
    embedder = cache.created[0]
    try:
        assert embedder.entered.wait(WAIT)
        with pytest.raises(UsageError, match="already running"):
            manager.start_index(db, folder)
        # the same file through a different spelling is the same collection
        with pytest.raises(UsageError):
            manager.start_index(db.parent / "." / db.name, folder)
        assert manager.active_for(db)["id"] == first["id"]
        active = manager.list(active_only=True)
        assert [job["id"] for job in active] == [first["id"]]

        # another collection may index at the same time
        other = manager.start_index(
            tmp_path / "other.db", write_notes(tmp_path / "more", 2), embed=False
        )
        assert manager.wait(other["id"], WAIT)["status"] == "succeeded"
    finally:
        embedder.gate.set()
    assert manager.wait(first["id"], WAIT)["status"] == "succeeded"
    assert manager.active_for(db) is None
    # once finished, a new job is allowed again
    assert manager.wait(manager.start_index(db, folder)["id"], WAIT)["status"] == "succeeded"


def test_get_and_list_return_deep_copies(corpus, db, no_model_stack):
    manager = JobManager(EmbedderCache())
    job_id = manager.start_index(db, corpus)["id"]
    job = manager.wait(job_id, WAIT)
    job["status"] = "hacked"
    job["steps"][0]["status"] = "hacked"
    job["result"]["added"] = -1
    listed = manager.list()
    listed[0]["steps"].clear()
    listed[0]["counts"]["read"] = -1

    fresh = manager.get(job_id)
    assert fresh["status"] == "succeeded"
    assert fresh["steps"][0]["status"] == "done"
    assert fresh["result"]["added"] == 5
    assert fresh["counts"]["read"] == 5
    assert manager.list()[0] == fresh


def test_list_is_newest_first_and_history_is_capped(tmp_path, db, monkeypatch, no_model_stack):
    monkeypatch.setattr(jobs_module, "MAX_JOB_HISTORY", 3)
    manager = JobManager(EmbedderCache())
    ids = []
    for number in range(5):
        job = manager.start_index(db, tmp_path / f"missing-{number}")
        manager.wait(job["id"], WAIT)
        ids.append(job["id"])
    listed = [job["id"] for job in manager.list()]
    assert listed == list(reversed(ids[-3:]))
    with pytest.raises(JobNotFoundError):
        manager.get(ids[0])


def test_unknown_job_ids(no_model_stack):
    manager = JobManager()
    for call in (manager.get, manager.cancel, manager.wait):
        with pytest.raises(JobNotFoundError):
            call("nope")
    assert issubclass(JobNotFoundError, DataError)
    assert manager.list() == []


@pytest.mark.parametrize(
    ("db_path", "root", "embed"),
    [("", "notes", None), ("x.db", "", None), ("x.db", "  ", None), ("x.db", "notes", "yes")],
)
def test_start_index_validates_arguments(tmp_path, db_path, root, embed):
    manager = JobManager(EmbedderCache())
    with pytest.raises(UsageError):
        manager.start_index(db_path, root, embed=embed)
    assert manager.list() == []


def test_shutdown_cancels_active_jobs(tmp_path, db, monkeypatch):
    monkeypatch.setattr(ingest_module, "BATCH_SIZE", 2)
    folder = write_notes(tmp_path / "notes", 6)
    cache = fake_cache(GatedEmbedder)
    manager = JobManager(cache)
    started = manager.start_index(db, folder, embed=True, model_name="fake-mini")
    deadline = time.monotonic() + WAIT
    while not cache.created and time.monotonic() < deadline:
        time.sleep(0.005)
    assert cache.created[0].entered.wait(WAIT)
    threading.Timer(0.05, cache.created[0].gate.set).start()
    manager.shutdown(timeout=WAIT)
    assert manager.get(started["id"])["status"] == "cancelled"


def test_default_manager_creates_its_own_cache():
    manager = JobManager()
    assert isinstance(manager.embedders, EmbedderCache)


# -- embedder cache ---------------------------------------------------------------------


def test_cache_loads_each_model_once_under_concurrency():
    loads: list[str] = []
    lock = threading.Lock()

    def slow_factory(name):
        with lock:
            loads.append(name)
        time.sleep(0.05)
        return FakeEmbedder(name)

    cache = EmbedderCache(factory=slow_factory)
    barrier = threading.Barrier(12)
    results: list = [None] * 12

    def worker(slot):
        barrier.wait()
        results[slot] = cache.get("model-a" if slot % 2 else "model-b")

    threads = [threading.Thread(target=worker, args=(slot,)) for slot in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(WAIT)

    assert sorted(loads) == ["model-a", "model-b"]
    a_models = {id(result) for slot, result in enumerate(results) if slot % 2}
    b_models = {id(result) for slot, result in enumerate(results) if not slot % 2}
    assert len(a_models) == 1 and len(b_models) == 1 and a_models != b_models
    assert cache.loaded("model-a") and cache.loaded("model-b")
    assert cache.names() == ["model-a", "model-b"]


def test_cache_default_name_peek_put_clear():
    cache = EmbedderCache(factory=FakeEmbedder)
    assert not cache.loaded()
    assert cache.peek() is None
    model = cache.get()
    assert model.name == DEFAULT_MODEL
    assert cache.loaded(DEFAULT_MODEL) and cache.peek() is model
    assert cache.get(DEFAULT_MODEL) is model

    seeded = FakeEmbedder("seeded")
    cache.put("seeded", seeded)
    assert cache.get("seeded") is seeded
    cache.clear()
    assert not cache.loaded() and cache.names() == []
    assert cache.available() is True


def test_cache_does_not_remember_failures():
    attempts = {"count": 0}

    def flaky(name):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise OSError("download interrupted")
        return FakeEmbedder(name)

    cache = EmbedderCache(factory=flaky)
    with pytest.raises(OSError):
        cache.get("m")
    assert not cache.loaded("m")
    assert cache.get("m").name == "m"
    assert attempts["count"] == 2


def test_cache_availability_without_factory(monkeypatch):
    monkeypatch.setattr(jobs_module, "model_stack_available", lambda: False)
    assert EmbedderCache().available() is False
    monkeypatch.setattr(jobs_module, "model_stack_available", lambda: True)
    assert EmbedderCache().available() is True


def test_model_stack_available_does_not_import(monkeypatch):
    monkeypatch.delitem(sys.modules, "sentence_transformers", raising=False)
    result = model_stack_available()
    assert isinstance(result, bool)
    assert "sentence_transformers" not in sys.modules or result


# -- SentenceEmbedder lock -----------------------------------------------------------


def test_sentence_embedder_serializes_encoding(monkeypatch):
    state = {"inside": 0, "max": 0}
    state_lock = threading.Lock()

    class FakeSentenceTransformer:
        def __init__(self, name, device=None, **_kwargs):
            self.name = name

        def get_embedding_dimension(self):
            return 3

        def encode(self, texts, **_kwargs):
            with state_lock:
                state["inside"] += 1
                state["max"] = max(state["max"], state["inside"])
            time.sleep(0.01)
            with state_lock:
                state["inside"] -= 1
            return np.ones((len(texts), 3), dtype=np.float64)

    fake_module = types.ModuleType("sentence_transformers")
    fake_module.SentenceTransformer = FakeSentenceTransformer
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module)

    from onefind.embed import SentenceEmbedder

    embedder = SentenceEmbedder("fake/model")
    assert embedder.dimension == 3

    errors: list[BaseException] = []

    def worker():
        try:
            for _ in range(3):
                vectors = embedder.encode_documents(["a", "b"])
                assert vectors.dtype == np.float32 and vectors.shape == (2, 3)
                assert embedder.encode_query("q").shape == (3,)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(WAIT)
    assert not errors
    assert state["max"] == 1


def test_job_and_search_can_share_cached_embedder(corpus, db):
    """A search on the server connection keeps working while a job writes."""
    pytest.importorskip("sqlite_vec")
    cache = fake_cache()
    manager = JobManager(cache)
    first = run(manager, db, corpus, embed=True, model_name="fake-mini")
    assert first["status"] == "succeeded"

    from onefind.search import search

    with Index.open(db) as server_index:
        server_index.attach_embedder(cache.get("fake-mini"))
        write_notes(corpus, 3, prefix="more")
        job_id = manager.start_index(db, corpus)["id"]
        hits = search(server_index, "rollback plan", mode="hybrid", k=3)
        assert hits
        assert manager.wait(job_id, WAIT)["status"] == "succeeded"
        assert server_index.count("documents") == 8
