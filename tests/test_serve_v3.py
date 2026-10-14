"""v1.2 HTTP API (docs/11-implementation-contract.md section 6).

Every test runs against a temporary ONEFIND home, a fake embedding model and
an injected file opener, so nothing touches the real per-user library or
launches desktop applications.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from onefind.ingest import ingest_path
from onefind.jobs import EmbedderCache
from onefind.serve import _is_loopback, create_app
from onefind.store import Document, Index

CSRF = {"X-OneFind-Request": "1"}
WAIT = 60.0


class FakeEmbedder:
    revision = "test-revision-1"
    dimension = 3

    def __init__(self, name: str = "fake-mini") -> None:
        self.name = name

    def encode_documents(self, texts):
        return np.stack([self._vector(text) for text in texts])

    def encode_query(self, text):
        return self._vector(text)

    @staticmethod
    def _vector(text):
        digest = hashlib.sha256(text.encode("utf-8")).digest()[:3]
        vector = np.frombuffer(digest, dtype=np.uint8).astype(np.float32) + 1.0
        return vector / np.linalg.norm(vector)


class GatedEmbedder(FakeEmbedder):
    """Blocks inside encode_documents until the test opens the gate."""

    gate = threading.Event()
    entered = threading.Event()

    def encode_documents(self, texts):
        type(self).entered.set()
        type(self).gate.wait(WAIT)
        return super().encode_documents(texts)


class Opener:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, bool]] = []

    def __call__(self, path: Path, reveal: bool) -> None:
        self.calls.append((Path(path), reveal))


@pytest.fixture()
def notes(tmp_path):
    root = tmp_path / "notes"
    (root / "projects").mkdir(parents=True)
    (root / "journal").mkdir()
    (root / "rollback.md").write_text(
        "---\ntags: [ops, release]\n---\n# Rollback plan\n\nHow to roll back a release "
        "safely. The owner signs off on every rollback.\n",
        encoding="utf-8",
    )
    (root / "projects" / "garden.md").write_text(
        "# Garden project\n\nTomatoes need sun and steady watering in summer.\n",
        encoding="utf-8",
    )
    (root / "projects" / "budget.txt").write_text(
        "Budget notes: the release budget covers testing and rollback drills.\n",
        encoding="utf-8",
    )
    (root / "journal" / "monday.md").write_text(
        "# Monday\n\nWrote about tomatoes and the garden again.\n", encoding="utf-8"
    )
    (root / "journal" / "copy.md").write_text(
        "# Monday\n\nWrote about tomatoes and the garden again.\n", encoding="utf-8"
    )
    return root


@pytest.fixture()
def home(tmp_path):
    return tmp_path / "onefind-home"


def make_client(home, *, factory=FakeEmbedder, opener=None, **kwargs):
    app = create_app(
        kwargs.pop("index_path", None),
        home=str(home),
        embedders=EmbedderCache(factory=factory),
        opener=opener or Opener(),
        **kwargs,
    )
    return app, TestClient(app)


def wait_job(client, job_id: str) -> dict:
    deadline = time.monotonic() + WAIT
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish")


def index_folder(client, folder, *, embed=False, name="Notes") -> dict:
    response = client.post("/api/collections", json={"name": name}, headers=CSRF)
    assert response.status_code in {201, 409}, response.text
    assert client.post("/api/collections/active", json={"name": name}, headers=CSRF).status_code == 200
    started = client.post("/api/index", json={"path": str(folder), "embed": embed}, headers=CSRF)
    assert started.status_code == 202, started.text
    job = wait_job(client, started.json()["id"])
    assert job["status"] == "succeeded", job
    return job


def ndjson(response) -> list[dict]:
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


@pytest.fixture()
def indexed(home, notes):
    """A library-mode client with the notes folder indexed (keyword only)."""
    opener = Opener()
    app, client = make_client(home, opener=opener)
    with client:
        index_folder(client, notes)
        yield client, app, opener


@pytest.fixture()
def embedded(home, notes):
    """Like ``indexed`` but with (fake) meaning-search vectors."""
    opener = Opener()
    app, client = make_client(home, opener=opener)
    with client:
        index_folder(client, notes, embed=True)
        yield client, app, opener


# ---- static assets & guards ------------------------------------------------------


def test_assets_have_media_types_cache_policy_and_no_traversal(home):
    _app, client = make_client(home)
    with client:
        script = client.get("/assets/api.js")
        assert script.status_code == 200
        assert script.headers["content-type"].startswith("text/javascript")
        assert script.headers["cache-control"] == "no-cache"
        assert client.get("/assets/styles.css").headers["content-type"].startswith("text/css")
        for name in ("App.js", "index.html", "..%2Fserve.py", "x.py", "-a.js", "missing.js"):
            assert client.get(f"/assets/{name}").status_code == 404, name
        assert client.get("/api/stats").headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api/collections", {"name": "X"}),
        ("POST", "/api/collections/open", {"db_path": "x.db"}),
        ("POST", "/api/collections/active", {"name": "X"}),
        ("POST", "/api/collections/rename", {"name": "X", "new_name": "Y"}),
        ("POST", "/api/collections/remove", {"name": "X"}),
        ("POST", "/api/index", {"path": "."}),
        ("POST", "/api/jobs/abc/cancel", None),
        ("POST", "/api/documents/open", {"doc_id": "a"}),
        ("POST", "/api/documents/remove", {"doc_ids": ["a"]}),
        ("POST", "/api/history", {"query": "q", "mode": "lexical", "result_count": 1}),
        ("POST", "/api/history/clear", None),
        ("POST", "/api/saved", {"name": "n", "query": "q", "mode": "lexical"}),
        ("DELETE", "/api/saved/1", None),
        ("POST", "/api/bookmarks", {"doc_id": "a"}),
        ("DELETE", "/api/bookmarks/a", None),
        ("POST", "/api/maintenance/backup", None),
        ("POST", "/api/maintenance/restore", {"backup_path": "x.db"}),
        ("POST", "/api/maintenance/check", None),
        ("POST", "/api/maintenance/repair", None),
        ("POST", "/api/maintenance/compact", None),
        ("POST", "/api/maintenance/clear", {"confirm_name": "X"}),
    ],
)
def test_mutating_endpoints_require_csrf_header(home, method, path, body):
    _app, client = make_client(home)
    with client:
        for headers in ({}, {"X-OneFind-Request": "0"}):
            response = client.request(method, path, json=body, headers=headers)
            assert response.status_code == 403, (path, response.text)
            assert response.json()["error"]["code"] == "csrf_check_failed"
            assert response.headers["x-frame-options"] == "DENY"
        assert not (home / "collections.json").exists()


def test_search_endpoints_are_csrf_exempt(indexed):
    client, _app, _opener = indexed
    assert client.post("/api/search", json={"query": "rollback", "mode": "lexical"}).status_code == 200
    stream = client.post("/api/search/stream", json={"query": "rollback", "mode": "lexical"})
    assert stream.status_code == 200
    assert ndjson(stream)[-1]["type"] == "result"


def test_loopback_detection():
    assert _is_loopback("127.0.0.1") and _is_loopback("::1") and _is_loopback("localhost")
    assert not _is_loopback("192.168.1.20")
    assert not _is_loopback("203.0.113.5")
    assert not _is_loopback(None)


def test_unexpected_errors_never_leak_details(indexed, monkeypatch):
    client, _app, _opener = indexed

    def broken(_self):
        raise RuntimeError("secret internal detail C:/private/path")

    monkeypatch.setattr(Index, "facets", broken)
    response = client.get("/api/facets")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "secret" not in response.text and "private" not in response.text


# ---- stats ------------------------------------------------------------------------


def test_stats_for_an_empty_library(home):
    _app, client = make_client(home)
    with client:
        body = client.get("/api/stats").json()
    assert body["ready"] is False and body["modes"] == []
    assert body["collection"] is None
    assert body["status"] == "no_index"
    assert body["status_detail"]
    assert body["job"] is None and body["vectors_missing"] == 0
    assert body["size_class"] == "small" and body["size_note"] is None


def test_stats_ready_and_private(indexed, home):
    client, _app, _opener = indexed
    response = client.get("/api/stats")
    body = response.json()
    assert body["ready"] is True and body["documents"] == 5
    assert body["collection"] == {"name": "Notes"}
    assert body["status"] == "ready"
    assert body["status_detail"].startswith("5 notes ready.")
    assert body["modes"] == ["lexical"]
    assert body["roots"] == 1 and body["last_indexed_at"]
    assert body["model_loaded"] is False and body["job"] is None
    db = client.get("/api/collections").json()["collections"][0]["db"]
    for needle in (db, json.dumps(db)[1:-1], Path(db).name, str(home)):
        assert needle not in response.text


def test_stats_with_vectors(embedded):
    client, _app, _opener = embedded
    body = client.get("/api/stats").json()
    assert body["modes"] == ["lexical", "semantic", "hybrid"]
    assert body["vectors"] == 5 and body["vectors_missing"] == 0
    assert "meaning search available" in body["status_detail"]
    assert body["model_loaded"] is True  # the job loaded it into the shared cache


def test_stats_reports_missing_vectors(embedded, notes):
    client, app, _opener = embedded
    runtime = app.state.runtime
    with runtime.lock:
        conn = runtime.index.conn
        conn.execute("DELETE FROM vec_float WHERE rowid = (SELECT MIN(row_id) FROM chunks)")
        conn.commit()
    body = client.get("/api/stats").json()
    assert body["status"] == "ready"
    assert body["vectors_missing"] == 1
    assert "Vectors missing for 1 note" in body["status_detail"]


def test_size_classes():
    from onefind.serve import _Runtime

    assert _Runtime._size_note(10) is None
    assert "medium" in _Runtime._size_note(6_000)
    assert "comfort zone" in _Runtime._size_note(60_000)


# ---- collections -------------------------------------------------------------------


def test_collections_lifecycle(home, tmp_path):
    _app, client = make_client(home)
    with client:
        created = client.post("/api/collections", json={"name": "Work"}, headers=CSRF)
        assert created.status_code == 201, created.text
        entry = created.json()
        assert entry["name"] == "Work" and entry["exists"] and entry["active"]
        assert Path(entry["db"]).parent == (home / "collections").absolute()
        assert client.get("/api/stats").json()["collection"] == {"name": "Work"}

        taken = client.post("/api/collections", json={"name": "work"}, headers=CSRF)
        assert taken.status_code == 409 and taken.json()["error"]["code"] == "name_taken"
        bad = client.post("/api/collections", json={"name": "  !!  "}, headers=CSRF)
        assert bad.status_code == 400

        assert client.post("/api/collections", json={"name": "Home"}, headers=CSRF).status_code == 201
        listing = client.get("/api/collections").json()
        assert listing["active"] == "Work"
        assert listing["home"] == str(home.absolute())
        assert [c["name"] for c in listing["collections"]] == ["Work", "Home"]
        for item in listing["collections"]:
            assert {"name", "db", "exists", "active", "documents", "vectors", "db_bytes", "created_at"} <= set(item)

        switched = client.post("/api/collections/active", json={"name": "home"}, headers=CSRF)
        assert switched.status_code == 200
        assert switched.json()["collection"] == {"name": "Home"}
        assert switched.json()["status"] == "empty"
        missing = client.post("/api/collections/active", json={"name": "Nope"}, headers=CSRF)
        assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"

        renamed = client.post(
            "/api/collections/rename", json={"name": "Home", "new_name": "Personal"}, headers=CSRF
        )
        assert renamed.status_code == 200 and renamed.json()["name"] == "Personal"
        assert client.get("/api/stats").json()["collection"] == {"name": "Personal"}
        clash = client.post(
            "/api/collections/rename", json={"name": "Personal", "new_name": "WORK"}, headers=CSRF
        )
        assert clash.status_code == 409

        db = Path(renamed.json()["db"])
        unconfirmed = client.post(
            "/api/collections/remove", json={"name": "Personal", "delete_file": True}, headers=CSRF
        )
        assert unconfirmed.status_code == 400
        assert unconfirmed.json()["error"]["code"] == "confirmation_required"
        removed = client.post(
            "/api/collections/remove",
            json={"name": "Personal", "delete_file": True, "confirm_name": "Personal"},
            headers=CSRF,
        )
        assert removed.status_code == 200, removed.text
        assert removed.json()["active"] == "Work"
        assert not db.exists()
        assert client.get("/api/stats").json()["collection"] == {"name": "Work"}

        kept = client.post("/api/collections/remove", json={"name": "Work"}, headers=CSRF)
        assert kept.status_code == 200 and kept.json()["active"] is None
        assert client.get("/api/stats").json()["collection"] is None
        assert Path(entry["db"]).exists()  # forgotten, not deleted


def test_open_existing_collection_file(home, tmp_path, notes):
    moved = tmp_path / "elsewhere" / "moved.db"
    moved.parent.mkdir()
    with Index.open(moved) as index:
        ingest_path(index, notes)
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a database at all" * 10)
    _app, client = make_client(home)
    with client:
        missing = client.post("/api/collections/open", json={"db_path": str(tmp_path / "nope.db")}, headers=CSRF)
        assert missing.status_code == 404
        invalid = client.post("/api/collections/open", json={"db_path": str(junk)}, headers=CSRF)
        assert invalid.status_code == 400
        opened = client.post("/api/collections/open", json={"db_path": str(moved)}, headers=CSRF)
        assert opened.status_code == 200, opened.text
        assert opened.json()["name"] == "moved" and opened.json()["documents"] == 5
        stats = client.get("/api/stats").json()
        assert stats["collection"] == {"name": "moved"} and stats["documents"] == 5
        again = client.post("/api/collections/open", json={"db_path": str(moved), "name": "Other"}, headers=CSRF)
        assert again.status_code == 200 and again.json()["name"] == "moved"


def test_index_path_is_registered_under_its_stem(home, tmp_path, notes):
    db = tmp_path / "demo.db"
    with Index.open(db) as index:
        ingest_path(index, notes)
    _app, client = make_client(home, index_path=db)
    with client:
        listing = client.get("/api/collections").json()
        assert listing["active"] == "demo"
        assert [c["name"] for c in listing["collections"]] == ["demo"]
        assert client.get("/api/stats").json()["documents"] == 5


def test_unregistered_index_path_runs_ad_hoc(home, tmp_path, notes):
    db = tmp_path / "adhoc.db"
    with Index.open(db) as index:
        ingest_path(index, notes)
    _app, client = make_client(home, index_path=db, register=False)
    with client:
        listing = client.get("/api/collections").json()
        assert listing["active"] == "adhoc"
        assert listing["collections"][0]["registered"] is False
        assert listing["collections"][0]["documents"] == 5
        assert not (home / "collections.json").exists()
        backup = client.post("/api/maintenance/backup", headers=CSRF)
        assert backup.status_code == 200
        assert Path(backup.json()["path"]).parent == (home / "backups").absolute()


# ---- indexing jobs ------------------------------------------------------------------


def test_index_job_end_to_end_lexical(home, notes):
    _app, client = make_client(home)
    with client:
        job = index_folder(client, notes)
        assert job["result"]["added"] == 5
        assert job["embed"] is False
        jobs = client.get("/api/jobs").json()["jobs"]
        assert [j["id"] for j in jobs] == [job["id"]]
        assert client.get("/api/jobs", params={"active": 1}).json()["jobs"] == []
        cancelled = client.post(f"/api/jobs/{job['id']}/cancel", headers=CSRF)
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "succeeded"
        unknown = client.get("/api/jobs/doesnotexist")
        assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "not_found"
        assert client.post("/api/jobs/doesnotexist/cancel", headers=CSRF).status_code == 404

        lexical = client.post("/api/search", json={"query": "tomatoes", "mode": "lexical"})
        assert {r["doc_id"] for r in lexical.json()["results"]} >= {"projects/garden.md"}


def test_index_job_with_fake_embedder_enables_meaning_search(embedded):
    client, _app, _opener = embedded
    hybrid = client.post("/api/search", json={"query": "rollback owner", "mode": "hybrid", "k": 5})
    assert hybrid.status_code == 200, hybrid.text
    body = hybrid.json()
    assert body["meta"]["mode"] == "hybrid" and body["meta"]["precision"] == "float"
    names = [stage["name"] for stage in body["meta"]["stages"]]
    assert names == ["parse", "lexical", "embed", "semantic", "fuse", "build"]
    top = body["results"][0]
    assert top["explain"]["mode"] == "hybrid"
    assert top["confidence"] in {"strong", "moderate", "weak"}
    semantic = client.post("/api/search", json={"query": "garden", "mode": "semantic", "precision": "binary"})
    assert semantic.status_code == 200
    assert semantic.json()["results"][0]["score"]["metric"] == "Hamming distance"


def test_index_creates_a_collection_when_none_exists(home, notes):
    _app, client = make_client(home)
    with client:
        started = client.post("/api/index", json={"path": str(notes), "embed": False}, headers=CSRF)
        assert started.status_code == 202
        assert wait_job(client, started.json()["id"])["status"] == "succeeded"
        stats = client.get("/api/stats").json()
        assert stats["collection"] == {"name": "notes"} and stats["documents"] == 5


def test_index_validation_errors(home, tmp_path):
    _app, client = make_client(home)
    with client:
        missing = client.post("/api/index", json={"path": str(tmp_path / "gone")}, headers=CSRF)
        assert missing.status_code == 404
        assert missing.json()["error"] == {
            "code": "path_not_found",
            "message": missing.json()["error"]["message"],
            "retryable": False,
            "action": "index",
        }
        binary = tmp_path / "photo.png"
        binary.write_bytes(b"\x89PNG")
        assert client.post("/api/index", json={"path": str(binary)}, headers=CSRF).status_code == 400
        bad = client.post("/api/index", json={"path": str(tmp_path), "embed": "maybe"}, headers=CSRF)
        assert bad.status_code == 422 and bad.json()["error"]["code"] == "validation_error"
        empty = tmp_path / "empty"
        empty.mkdir()
        assert client.post("/api/collections", json={"name": "E"}, headers=CSRF).status_code == 201
        job = client.post("/api/index", json={"path": str(empty), "embed": False}, headers=CSRF).json()
        failed = wait_job(client, job["id"])
        assert failed["status"] == "failed"
        assert failed["error"]["code"] == "nothing_to_index"


def test_running_job_blocks_destructive_actions(home, notes):
    GatedEmbedder.gate.clear()
    GatedEmbedder.entered.clear()
    _app, client = make_client(home, factory=GatedEmbedder)
    with client:
        try:
            assert client.post("/api/collections", json={"name": "Busy"}, headers=CSRF).status_code == 201
            started = client.post("/api/index", json={"path": str(notes), "embed": True}, headers=CSRF)
            assert started.status_code == 202
            assert GatedEmbedder.entered.wait(WAIT)

            response = client.get("/api/stats")
            stats = response.json()
            assert stats["status"] == "indexing"
            assert stats["status_detail"].startswith("Indexing")
            assert stats["job"]["id"] == started.json()["id"]
            assert "db" not in stats["job"]
            db = client.get("/api/collections").json()["collections"][0]["db"]
            assert db not in response.text and json.dumps(db)[1:-1] not in response.text

            again = client.post("/api/index", json={"path": str(notes)}, headers=CSRF)
            assert again.status_code == 409 and again.json()["error"]["code"] == "collection_busy"
            for path, body in (
                ("/api/collections/remove", {"name": "Busy"}),
                ("/api/maintenance/clear", {"confirm_name": "Busy"}),
                ("/api/maintenance/compact", None),
            ):
                blocked = client.post(path, json=body, headers=CSRF)
                assert blocked.status_code == 409, path
                assert blocked.json()["error"]["code"] == "collection_busy"
            assert client.get("/api/jobs", params={"active": "1"}).json()["jobs"]
        finally:
            GatedEmbedder.gate.set()
        assert wait_job(client, started.json()["id"])["status"] == "succeeded"
        assert client.get("/api/stats").json()["status"] == "ready"


def test_cancel_running_job(home, notes):
    GatedEmbedder.gate.clear()
    GatedEmbedder.entered.clear()
    _app, client = make_client(home, factory=GatedEmbedder)
    with client:
        assert client.post("/api/collections", json={"name": "C"}, headers=CSRF).status_code == 201
        job = client.post("/api/index", json={"path": str(notes), "embed": True}, headers=CSRF).json()
        assert GatedEmbedder.entered.wait(WAIT)
        cancelled = client.post(f"/api/jobs/{job['id']}/cancel", headers=CSRF)
        assert cancelled.status_code == 200
        GatedEmbedder.gate.set()
        assert wait_job(client, job["id"])["status"] == "cancelled"


def test_index_changes_reports_stale_roots(indexed, notes):
    client, _app, _opener = indexed
    clean = client.get("/api/index/changes").json()
    assert clean["stale"] is False
    assert clean["roots"][0]["root"] == str(notes.resolve())
    assert clean["roots"][0]["unchanged"] == 5
    (notes / "new-note.md").write_text("# New\n\nfresh\n", encoding="utf-8")
    (notes / "journal" / "copy.md").unlink()
    changed = client.get("/api/index/changes").json()
    assert changed["stale"] is True
    assert changed["roots"][0]["new"] == 1 and changed["roots"][0]["deleted"] == 1


@pytest.mark.skipif(
    os.environ.get("ONEFIND_SKIP_MODEL_TESTS") == "1", reason="model tests disabled"
)
def test_index_job_with_the_real_model(home, notes):
    pytest.importorskip("sentence_transformers")
    from onefind.envcheck import DEFAULT_MODEL

    app = create_app(None, home=str(home), opener=Opener())
    with TestClient(app) as client:
        try:
            job = index_folder(client, notes, embed=True)
        except AssertionError as exc:  # model not cached locally / offline
            pytest.skip(f"real model unavailable: {exc}")
        assert job["model"] == DEFAULT_MODEL
        body = client.post("/api/search", json={"query": "vegetables need sunlight", "mode": "semantic"}).json()
        assert body["results"][0]["doc_id"] in {"projects/garden.md", "journal/monday.md", "journal/copy.md"}
        assert body["results"][0]["explain"]["semantic_score"] > 0


# ---- search ---------------------------------------------------------------------------


def test_search_result_fields_explain_and_meta(indexed):
    client, _app, _opener = indexed
    response = client.post("/api/search", json={"query": "rollback owner", "mode": "lexical", "k": 5})
    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "rollback owner"
    top = body["results"][0]
    assert top["doc_id"] == "rollback.md"
    assert top["folder"] == "" and top["ext"] == ".md"
    assert top["tags"] == ["ops", "release"]
    assert top["modified_at"].endswith("+00:00")
    assert top["bookmarked"] is False and top["duplicates"] == []
    assert top["confidence"] == "strong"
    assert top["explain"]["matched_terms"] == ["rollback", "owner"]
    assert top["explain"]["missing_terms"] == []
    assert top["explain"]["reasons"]
    assert any(segment["highlight"] for segment in top["snippet"]["segments"])
    meta = body["meta"]
    assert [stage["name"] for stage in meta["stages"]] == ["parse", "lexical", "build"]
    assert meta["abstained"] is False and isinstance(meta["weak_count"], int)
    assert meta["filters"] == {}
    no_explain = client.post("/api/search", json={"query": "rollback", "mode": "lexical", "explain": False})
    assert no_explain.json()["results"][0]["explain"] is None
    assert no_explain.json()["results"][0]["confidence"] in {"strong", "moderate", "weak"}


def test_search_duplicates_and_bookmarks_are_reported(indexed):
    client, _app, _opener = indexed
    assert client.post("/api/bookmarks", json={"doc_id": "journal/monday.md"}, headers=CSRF).status_code == 200
    results = client.post("/api/search", json={"query": "tomatoes garden", "mode": "lexical"}).json()["results"]
    by_id = {r["doc_id"]: r for r in results}
    assert by_id["journal/monday.md"]["duplicates"] == ["journal/copy.md"]
    assert by_id["journal/monday.md"]["bookmarked"] is True
    assert by_id["journal/copy.md"]["bookmarked"] is False


def test_search_filters(indexed):
    client, _app, _opener = indexed

    def ids(query, filters):
        response = client.post("/api/search", json={"query": query, "mode": "lexical", "filters": filters})
        assert response.status_code == 200, response.text
        return {r["doc_id"] for r in response.json()["results"]}, response.json()["meta"]["filters"]

    assert ids("tomatoes", {})[0] == {"projects/garden.md", "journal/monday.md", "journal/copy.md"}
    found, applied = ids("tomatoes", {"folders": ["projects"]})
    assert found == {"projects/garden.md"}
    assert applied == {"folders": ["projects"]}
    assert ids("tomatoes", {"folders": ["journal"]})[0] == {"journal/monday.md", "journal/copy.md"}
    assert ids("release", {})[0] == {"rollback.md", "projects/budget.txt"}
    assert ids("release", {"exts": ["txt"]})[0] == {"projects/budget.txt"}
    assert ids("release", {"tags": ["ops"]})[0] == {"rollback.md"}
    assert ids("release", {"folders": [""]})[0] == {"rollback.md"}
    assert ids("release", {"modified_after": "2999-01-01"})[0] == set()
    for bad in ({"colour": "red"}, {"exts": ["bad ext"]}, {"modified_after": "yesterday"}):
        response = client.post("/api/search", json={"query": "x", "mode": "lexical", "filters": bad})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_filters"


def test_search_records_history_only_when_asked(indexed):
    client, _app, _opener = indexed
    client.post("/api/search", json={"query": "garden", "mode": "lexical", "record": False})
    assert client.get("/api/history").json()["items"] == []
    client.post("/api/search", json={"query": "garden", "mode": "lexical", "filters": {"exts": [".md"]}})
    items = client.get("/api/history").json()["items"]
    assert len(items) == 1
    assert items[0]["query"] == "garden" and items[0]["precision"] is None
    assert items[0]["filters"] == {"exts": [".md"]} and items[0]["result_count"] >= 1


def test_meaning_search_without_vectors_suggests_embedding(indexed):
    client, _app, _opener = indexed
    for mode in ("semantic", "hybrid"):
        response = client.post("/api/search", json={"query": "garden", "mode": mode})
        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "no_embeddings" and error["action"] == "embed"


def test_search_without_collection_is_no_index(home):
    _app, client = make_client(home)
    with client:
        response = client.post("/api/search", json={"query": "x", "mode": "lexical"})
        assert response.status_code == 503 and response.json()["error"]["code"] == "no_index"


# ---- streaming search -----------------------------------------------------------------


def test_stream_emits_stage_events_in_order_then_result(indexed):
    client, _app, _opener = indexed
    response = client.post("/api/search/stream", json={"query": "rollback", "mode": "lexical"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    events = ndjson(response)
    stages = [(e["name"], e["status"]) for e in events if e["type"] == "stage"]
    assert stages == [
        ("parse", "start"), ("parse", "done"),
        ("lexical", "start"), ("lexical", "done"),
        ("build", "start"), ("build", "done"),
    ]
    assert all(isinstance(e["ms"], (int, float)) for e in events if e["type"] == "stage")
    assert events[-1]["type"] == "result"
    assert events[-1]["results"][0]["doc_id"] == "rollback.md"
    assert [s["name"] for s in events[-1]["meta"]["stages"]] == ["parse", "lexical", "build"]


def test_stream_hybrid_stages(embedded):
    client, _app, _opener = embedded
    events = ndjson(client.post("/api/search/stream", json={"query": "garden", "mode": "hybrid"}))
    done = [e["name"] for e in events if e["type"] == "stage" and e["status"] == "done"]
    assert done == ["parse", "lexical", "embed", "semantic", "fuse", "build"]
    assert events[-1]["type"] == "result"


def test_stream_errors_are_events(indexed, home):
    client, _app, _opener = indexed
    events = ndjson(client.post("/api/search/stream", json={"query": "garden", "mode": "semantic"}))
    assert events[-1]["type"] == "error"
    assert events[-1]["error"]["code"] == "no_embeddings"
    assert events[-1]["error"]["status"] == 400 and events[-1]["status"] == 400
    bad_filter = ndjson(client.post(
        "/api/search/stream", json={"query": "garden", "mode": "lexical", "filters": {"x": 1}}
    ))
    assert bad_filter[-1]["error"]["code"] == "invalid_filters"
    invalid = client.post("/api/search/stream", json={"query": " ", "mode": "lexical"})
    assert invalid.status_code == 422
    _app2, empty = make_client(home / "other")
    with empty:
        events = ndjson(empty.post("/api/search/stream", json={"query": "x", "mode": "lexical"}))
        assert events == [events[-1]] and events[-1]["error"]["code"] == "no_index"


def test_stream_flushes_stage_events_before_the_result(home, notes, monkeypatch):
    """Over a real socket, stage lines arrive while the search is still running."""
    uvicorn = pytest.importorskip("uvicorn")
    import socket

    import httpx

    import onefind.serve as serve_module

    with Index.open(home.parent / "stream.db") as index:
        ingest_path(index, notes)
    app, _client = make_client(home, index_path=home.parent / "stream.db")

    gate = threading.Event()
    original = serve_module.search_detailed

    def gated_search(*args, on_stage=None, **kwargs):
        def relay(name, status, ms):
            on_stage(name, status, ms)
            if (name, status) == ("lexical", "done"):
                gate.wait(WAIT)  # hold the search until the client saw this line
        return original(*args, on_stage=relay, **kwargs)

    monkeypatch.setattr(serve_module, "search_detailed", gated_search)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + WAIT
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        events = []
        released_by_client = False
        with httpx.stream(
            "POST",
            f"http://127.0.0.1:{port}/api/search/stream",
            json={"query": "rollback", "mode": "lexical"},
            timeout=WAIT,
        ) as response:
            assert response.headers["content-type"].startswith("application/x-ndjson")
            for line in response.iter_lines():
                if not line.strip():
                    continue
                event = json.loads(line)
                events.append(event)
                if event.get("name") == "lexical" and event.get("status") == "done":
                    released_by_client = not gate.is_set()
                    gate.set()
        assert released_by_client, "stage events were buffered until the search finished"
        assert [e["type"] for e in events][-1] == "result"
        assert [e["name"] for e in events if e["type"] == "stage"][-1] == "build"
    finally:
        gate.set()
        server.should_exit = True
        thread.join(10)


# ---- facets & documents ---------------------------------------------------------------


def test_facets(indexed, home):
    client, _app, _opener = indexed
    facets = client.get("/api/facets").json()
    assert {"folder": "journal", "count": 2} in facets["folders"]
    assert {"ext": ".txt", "count": 1} in facets["types"]
    assert {"tag": "ops", "count": 1} in facets["tags"]
    assert facets["modified"]["min"] is not None
    _app2, empty = make_client(home / "empty")
    with empty:
        assert empty.get("/api/facets").json()["folders"] == []


def test_document_detail(indexed):
    client, _app, _opener = indexed
    doc = client.get("/api/documents/journal/monday.md")
    assert doc.status_code == 200
    body = doc.json()
    assert body["doc_id"] == "journal/monday.md" and body["folder"] == "journal"
    assert body["duplicates"] == ["journal/copy.md"]
    assert body["truncated"] is False and "tomatoes" in body["body"]
    assert body["modified_at"].endswith("+00:00")
    missing = client.get("/api/documents/nope/never.md")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"


def test_document_body_is_capped(indexed):
    client, app, _opener = indexed
    with app.state.runtime.lock:
        app.state.runtime.index.add_documents([Document("big.md", "word " * 60_000, title="Big")])
    body = client.get("/api/documents/big.md").json()
    assert body["truncated"] is True and len(body["body"]) == 200_000


def test_open_document_uses_root_and_source(indexed, notes):
    client, _app, opener = indexed
    response = client.post("/api/documents/open", json={"doc_id": "projects/garden.md"}, headers=CSRF)
    assert response.status_code == 200, response.text
    assert opener.calls == [((notes / "projects" / "garden.md").resolve(), False)]
    client.post("/api/documents/open", json={"doc_id": "rollback.md", "reveal": True}, headers=CSRF)
    assert opener.calls[-1] == ((notes / "rollback.md").resolve(), True)
    recent = client.get("/api/recent-documents").json()["items"]
    assert {item["doc_id"] for item in recent[:2]} == {"rollback.md", "projects/garden.md"}
    assert all(item["last_opened_at"] for item in recent[:2])
    assert all(item["last_opened_at"] is None for item in recent[2:])
    assert {"doc_id", "title", "source", "folder", "last_opened_at", "updated_at"} <= set(recent[0])


def test_open_document_with_root_qualified_id(home, notes, tmp_path):
    second = tmp_path / "second"
    second.mkdir()
    (second / "rollback.md").write_text("# Other rollback\n\nsecond folder\n", encoding="utf-8")
    opener = Opener()
    _app, client = make_client(home, opener=opener)
    with client:
        index_folder(client, notes)
        job = client.post("/api/index", json={"path": str(second), "embed": False}, headers=CSRF).json()
        assert wait_job(client, job["id"])["status"] == "succeeded"
        hits = client.post("/api/search", json={"query": "second folder", "mode": "lexical"}).json()["results"]
        qualified = hits[0]["doc_id"]
        assert qualified.startswith("~") and qualified.endswith("/rollback.md")
        assert client.post("/api/documents/open", json={"doc_id": qualified}, headers=CSRF).status_code == 200
        assert opener.calls[-1][0] == (second / "rollback.md").resolve()


def test_open_document_refuses_paths_outside_the_root(indexed, notes, tmp_path):
    client, app, opener = indexed
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret", encoding="utf-8")
    runtime = app.state.runtime
    with runtime.lock:
        root_id = runtime.index.roots()[0]["root_id"]
        runtime.index.add_documents([
            Document("evil.md", "x", title="Evil", source="../secret.txt", root_id=root_id),
            Document("abs.md", "x", title="Abs", source=str(secret), root_id=root_id),
        ])
    for doc_id in ("evil.md", "abs.md"):
        response = client.post("/api/documents/open", json={"doc_id": doc_id}, headers=CSRF)
        assert response.status_code == 403, doc_id
        assert response.json()["error"]["code"] == "path_outside_root"
    for doc_id in ("../secret.txt", "../../etc/passwd", str(secret)):
        response = client.post("/api/documents/open", json={"doc_id": doc_id}, headers=CSRF)
        assert response.status_code == 404
    assert opener.calls == []


def test_open_document_refuses_symlink_escape(indexed, notes, tmp_path):
    client, app, opener = indexed
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n\nlinked", encoding="utf-8")
    link = notes / "link.md"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")
    runtime = app.state.runtime
    with runtime.lock:
        root_id = runtime.index.roots()[0]["root_id"]
        runtime.index.add_documents([Document("link.md", "x", title="Link", source="link.md", root_id=root_id)])
    response = client.post("/api/documents/open", json={"doc_id": "link.md"}, headers=CSRF)
    assert response.status_code == 403
    assert opener.calls == []


def test_open_document_missing_file_and_disabled(home, notes):
    opener = Opener()
    _app, client = make_client(home, opener=opener)
    with client:
        index_folder(client, notes)
        (notes / "rollback.md").unlink()
        gone = client.post("/api/documents/open", json={"doc_id": "rollback.md"}, headers=CSRF)
        assert gone.status_code == 404
        assert gone.json()["error"]["code"] == "file_missing"
        assert gone.json()["error"]["action"] == "reindex"
    app, client = make_client(home, opener=opener, allow_open=False)
    with client:
        response = client.post("/api/documents/open", json={"doc_id": "projects/garden.md"}, headers=CSRF)
        assert response.status_code == 403 and response.json()["error"]["code"] == "open_disabled"
    app = create_app(None, home=str(home), opener=opener, embedders=EmbedderCache(factory=FakeEmbedder))
    with TestClient(app, client=("203.0.113.5", 4000)) as remote:
        response = remote.post("/api/documents/open", json={"doc_id": "projects/garden.md"}, headers=CSRF)
        assert response.status_code == 403 and response.json()["error"]["code"] == "open_disabled"
    assert opener.calls == []


def test_open_failure_is_friendly(indexed):
    client, app, _opener = indexed

    def broken(_path, _reveal):
        raise OSError("ShellExecute failed with secret detail")

    app.state.opener = broken
    response = client.post("/api/documents/open", json={"doc_id": "rollback.md"}, headers=CSRF)
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "open_failed"
    assert "secret" not in response.text


def test_remove_documents_keeps_files(indexed, notes):
    client, _app, _opener = indexed
    response = client.post("/api/documents/remove", json={"doc_ids": ["rollback.md", "nope"]}, headers=CSRF)
    assert response.status_code == 200 and response.json() == {"removed": 1}
    assert client.get("/api/documents/rollback.md").status_code == 404
    assert (notes / "rollback.md").exists()
    assert client.post("/api/documents/remove", json={"doc_ids": []}, headers=CSRF).status_code == 422


# ---- history, saved, bookmarks --------------------------------------------------------


def test_history_endpoints(indexed):
    client, _app, _opener = indexed
    assert client.get("/api/history").json() == {"items": []}
    recorded = client.post(
        "/api/history",
        json={"query": "rollback", "mode": "hybrid", "precision": "int8", "filters": {"tags": "ops"}, "result_count": 3},
        headers=CSRF,
    )
    assert recorded.status_code == 200
    item = recorded.json()["items"][0]
    assert item["query"] == "rollback" and item["precision"] == "int8"
    assert item["filters"] == {"tags": ["ops"]} and item["result_count"] == 3
    lexical = client.post(
        "/api/history", json={"query": "garden", "mode": "lexical", "precision": "float", "result_count": 1}, headers=CSRF
    )
    assert lexical.json()["items"][0]["precision"] is None
    invalid = client.post("/api/history", json={"query": "x", "mode": "fuzzy", "result_count": 1}, headers=CSRF)
    assert invalid.status_code == 422
    assert client.post("/api/history", json={"query": "x", "filters": {"bad": 1}}, headers=CSRF).status_code == 400
    cleared = client.post("/api/history/clear", headers=CSRF)
    assert cleared.status_code == 200 and cleared.json()["items"] == []
    assert client.get("/api/history").json()["items"] == []


def test_saved_queries(indexed):
    client, _app, _opener = indexed
    saved = client.post(
        "/api/saved",
        json={"name": "Ops", "query": "rollback", "mode": "lexical", "precision": "float", "filters": {"exts": ["md"]}},
        headers=CSRF,
    )
    assert saved.status_code == 200, saved.text
    entry = saved.json()
    assert entry["name"] == "Ops" and entry["precision"] is None and entry["filters"] == {"exts": [".md"]}
    assert [item["id"] for item in client.get("/api/saved").json()["items"]] == [entry["id"]]
    assert client.delete(f"/api/saved/{entry['id']}", headers=CSRF).status_code == 200
    assert client.delete(f"/api/saved/{entry['id']}", headers=CSRF).status_code == 404
    assert client.delete("/api/saved/not-a-number", headers=CSRF).status_code == 404
    assert client.get("/api/saved").json()["items"] == []


def test_bookmarks(indexed):
    client, _app, _opener = indexed
    added = client.post("/api/bookmarks", json={"doc_id": "projects/garden.md", "note": "seeds"}, headers=CSRF)
    assert added.status_code == 200 and added.json()["note"] == "seeds"
    assert [b["doc_id"] for b in client.get("/api/bookmarks").json()["items"]] == ["projects/garden.md"]
    assert client.get("/api/documents/projects/garden.md").json()["bookmarked"] is True
    unknown = client.post("/api/bookmarks", json={"doc_id": "nope.md"}, headers=CSRF)
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "not_found"
    removed = client.delete("/api/bookmarks/projects/garden.md", headers=CSRF)
    assert removed.json() == {"removed": True, "doc_id": "projects/garden.md"}
    assert client.get("/api/bookmarks").json()["items"] == []


def test_recent_documents_limit(indexed):
    client, _app, _opener = indexed
    items = client.get("/api/recent-documents", params={"limit": 2}).json()["items"]
    assert len(items) == 2 and all(item["last_opened_at"] is None for item in items)
    assert "mtime" not in items[0] and items[0]["modified_at"]


# ---- filesystem ---------------------------------------------------------------------


def test_fs_list(home, tmp_path):
    folder = tmp_path / "browse"
    (folder / "Zeta").mkdir(parents=True)
    (folder / "alpha").mkdir()
    (folder / ".git").mkdir()
    (folder / "note.md").write_text("x", encoding="utf-8")
    (folder / "B.txt").write_text("x", encoding="utf-8")
    (folder / "image.png").write_bytes(b"x")
    (folder / ".hidden.md").write_text("x", encoding="utf-8")
    _app, client = make_client(home)
    with client:
        body = client.get("/api/fs/list", params={"path": str(folder)}, headers=CSRF).json()
        assert body["path"] == str(folder.resolve())
        assert body["parent"] == str(folder.resolve().parent)
        assert [(e["name"], e["kind"]) for e in body["entries"]] == [
            ("alpha", "dir"), ("Zeta", "dir"), ("B.txt", "file"), ("image.png", "file"), ("note.md", "file"),
        ]
        supported = {e["name"]: e["supported"] for e in body["entries"]}
        assert supported["note.md"] and supported["B.txt"] and not supported["image.png"]
        assert body["supported_files"] == 2
        assert body["entries"][0]["path"] == str(folder.resolve() / "alpha")
        assert set(body["entries"][0]) == {"name", "path", "kind", "supported"}
        assert any(place["label"] == "Home" for place in body["places"])
        default = client.get("/api/fs/list", headers=CSRF).json()
        assert default["path"] == str(Path.home().resolve())
        missing = client.get("/api/fs/list", params={"path": str(tmp_path / "nope")}, headers=CSRF)
        assert missing.status_code == 404 and missing.json()["error"]["code"] == "path_not_found"
        not_dir = client.get("/api/fs/list", params={"path": str(folder / "note.md")}, headers=CSRF)
        assert not_dir.status_code == 404


def test_fs_list_caps_entries(home, tmp_path):
    folder = tmp_path / "many"
    folder.mkdir()
    for number in range(510):
        (folder / f"n{number:03}.md").write_bytes(b"x")
    _app, client = make_client(home)
    with client:
        body = client.get("/api/fs/list", params={"path": str(folder)}, headers=CSRF).json()
    assert len(body["entries"]) == 500 and body["truncated"] is True
    assert body["supported_files"] == 510


def test_fs_list_permission_denied(home, tmp_path, monkeypatch):
    import onefind.serve as serve_module

    def denied(_path):
        raise PermissionError("nope")

    monkeypatch.setattr(serve_module.os, "scandir", denied)
    _app, client = make_client(home)
    with client:
        response = client.get("/api/fs/list", params={"path": str(tmp_path)}, headers=CSRF)
    assert response.status_code == 403 and response.json()["error"]["code"] == "permission_denied"


# ---- maintenance ----------------------------------------------------------------------


def test_backup_and_restore_as_new(indexed, home):
    client, _app, _opener = indexed
    backup = client.post("/api/maintenance/backup", headers=CSRF)
    assert backup.status_code == 200, backup.text
    info = backup.json()
    assert Path(info["path"]).parent == (home / "backups").absolute()
    assert info["bytes"] > 0 and info["created_at"]
    items = client.get("/api/backups").json()["items"]
    assert [item["path"] for item in items] == [info["path"]]

    restored = client.post(
        "/api/maintenance/restore",
        json={"backup_path": info["path"], "mode": "new", "name": "Notes (restored)"},
        headers=CSRF,
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["collection"]["name"] == "Notes (restored)"
    stats = client.get("/api/stats").json()
    assert stats["collection"] == {"name": "Notes (restored)"} and stats["documents"] == 5
    taken = client.post(
        "/api/maintenance/restore", json={"backup_path": info["path"], "mode": "new", "name": "notes"}, headers=CSRF
    )
    assert taken.status_code == 409 and taken.json()["error"]["code"] == "name_taken"


def test_restore_replace_requires_confirmation_and_backs_up_first(indexed, home):
    client, _app, _opener = indexed
    info = client.post("/api/maintenance/backup", headers=CSRF).json()
    client.post("/api/documents/remove", json={"doc_ids": ["rollback.md"]}, headers=CSRF)
    assert client.get("/api/stats").json()["documents"] == 4

    body = {"backup_path": info["path"], "mode": "replace"}
    unconfirmed = client.post("/api/maintenance/restore", json=body, headers=CSRF)
    assert unconfirmed.status_code == 400
    wrong = client.post("/api/maintenance/restore", json={**body, "confirm_name": "notes"}, headers=CSRF)
    assert wrong.status_code == 400

    replaced = client.post("/api/maintenance/restore", json={**body, "confirm_name": "Notes"}, headers=CSRF)
    assert replaced.status_code == 200, replaced.text
    payload = replaced.json()
    assert payload["mode"] == "replace" and payload["stats"]["documents"] == 5
    safety = Path(payload["safety_backup"]["path"])
    assert safety.exists() and safety != Path(info["path"])
    with Index.open(safety) as index:
        assert index.count("documents") == 4
    assert client.get("/api/stats").json()["documents"] == 5
    assert len(client.get("/api/backups").json()["items"]) == 2


def test_restore_rejects_unsafe_or_invalid_files(indexed, tmp_path):
    client, _app, _opener = indexed
    text = tmp_path / "notes.txt"
    text.write_text("hello", encoding="utf-8")
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"garbage" * 100)
    other = tmp_path / "plain.db"
    import sqlite3

    conn = sqlite3.connect(other)
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()
    for path, code in ((text, "invalid_backup"), (junk, "invalid_backup"), (other, "invalid_backup"),
                       (tmp_path / "missing.db", "path_not_found")):
        response = client.post("/api/maintenance/restore", json={"backup_path": str(path), "mode": "new", "name": "X"}, headers=CSRF)
        assert response.status_code in {400, 404}, path
        assert response.json()["error"]["code"] == code
    bad_mode = client.post("/api/maintenance/restore", json={"backup_path": str(junk), "mode": "merge"}, headers=CSRF)
    assert bad_mode.status_code == 422
    assert [c["name"] for c in client.get("/api/collections").json()["collections"]] == ["Notes"]


def test_check_repair_compact_clear(indexed):
    client, app, _opener = indexed
    check = client.post("/api/maintenance/check", headers=CSRF)
    assert check.status_code == 200 and check.json()["ok"] is True
    with app.state.runtime.lock:
        app.state.runtime.index.conn.execute("DELETE FROM fts WHERE doc_id = 'rollback.md'")
        app.state.runtime.index.conn.commit()
    assert client.post("/api/maintenance/check", headers=CSRF).json()["ok"] is False
    assert client.get("/api/stats").json()["status"] == "needs_attention"
    repaired = client.post("/api/maintenance/repair", headers=CSRF)
    assert repaired.status_code == 200 and repaired.json()["fts_rebuilt"] == 5
    assert client.get("/api/stats").json()["status"] == "ready"
    compacted = client.post("/api/maintenance/compact", headers=CSRF).json()
    assert {"before", "after"} <= set(compacted)

    wrong = client.post("/api/maintenance/clear", json={"confirm_name": "notes"}, headers=CSRF)
    assert wrong.status_code == 400 and wrong.json()["error"]["code"] == "confirmation_required"
    cleared = client.post("/api/maintenance/clear", json={"confirm_name": "Notes"}, headers=CSRF)
    assert cleared.status_code == 200
    assert cleared.json()["stats"]["documents"] == 0
    assert cleared.json()["stats"]["status"] == "empty"


def test_maintenance_without_collection(home):
    _app, client = make_client(home)
    with client:
        for path in ("/api/maintenance/backup", "/api/maintenance/check", "/api/maintenance/repair"):
            response = client.post(path, headers=CSRF)
            assert response.status_code == 503 and response.json()["error"]["code"] == "no_index"
        assert client.get("/api/backups").json() == {"items": []}
        assert client.get("/api/index/changes").json() == {"roots": [], "stale": False}
        for path in ("/api/history", "/api/saved", "/api/bookmarks", "/api/recent-documents"):
            assert client.get(path).json() == {"items": []}


def test_shutdown_cancels_running_jobs(home, notes):
    GatedEmbedder.gate.clear()
    GatedEmbedder.entered.clear()
    app, client = make_client(home, factory=GatedEmbedder)
    with client:
        assert client.post("/api/collections", json={"name": "S"}, headers=CSRF).status_code == 201
        job = client.post("/api/index", json={"path": str(notes), "embed": True}, headers=CSRF).json()
        assert GatedEmbedder.entered.wait(WAIT)
        threading.Timer(0.2, GatedEmbedder.gate.set).start()
    assert app.state.jobs.get(job["id"])["status"] in {"cancelled", "succeeded"}
