"""Regression tests for backend review findings in the HTTP API (serve.py)."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

import onefind.serve as serve_module
import onefind.store as store_module
from onefind.errors import UsageError
from onefind.ingest import ingest_path
from onefind.jobs import EmbedderCache
from onefind.library import Library
from onefind.serve import create_app
from onefind.store import Index

from test_serve_v3 import (  # noqa: E402 - shared fixtures/helpers
    CSRF,
    WAIT,
    FakeEmbedder,
    Opener,
    index_folder,
    make_client,
    wait_job,
)


@pytest.fixture()
def notes(tmp_path):
    root = tmp_path / "notes"
    root.mkdir()
    (root / "garden.md").write_text("# Garden\n\nTomatoes need sun.\n", encoding="utf-8")
    (root / "release.md").write_text("# Release\n\nRollback plan.\n", encoding="utf-8")
    return root


@pytest.fixture()
def home(tmp_path):
    return tmp_path / "home"


# -- finding 4: Check during a running job ---------------------------------------------


def test_check_during_a_job_is_busy_not_damage(home, notes):
    from test_serve_v3 import GatedEmbedder

    GatedEmbedder.gate.clear()
    GatedEmbedder.entered.clear()
    app, client = make_client(home, factory=GatedEmbedder)
    with client:
        index_folder(client, notes)
        (notes / "new.md").write_text("fresh note", encoding="utf-8")
        job = client.post("/api/index", json={"path": str(notes), "embed": True}, headers=CSRF)
        assert job.status_code == 202
        try:
            assert GatedEmbedder.entered.wait(WAIT)
            started = time.monotonic()
            response = client.post("/api/maintenance/check", headers=CSRF)
            assert response.status_code == 409, response.text
            assert response.json()["error"]["code"] == "collection_busy"
            assert time.monotonic() - started < 2.0
        finally:
            GatedEmbedder.gate.set()
        wait_job(client, job.json()["id"])
        report = client.post("/api/maintenance/check", headers=CSRF).json()
        assert report["ok"] is True, report


# -- finding 5: the runtime lock is not held while the model encodes a query ----------


def test_stats_stay_responsive_while_a_search_waits_for_the_model(home, notes):
    class SlowQuery(FakeEmbedder):
        entered = threading.Event()
        gate = threading.Event()

        def encode_query(self, text):
            type(self).entered.set()
            type(self).gate.wait(WAIT)
            return super().encode_query(text)

    app, client = make_client(home, factory=SlowQuery)
    with client:
        index_folder(client, notes, embed=True)
        result: dict = {}

        def search():
            result["response"] = client.post(
                "/api/search", json={"query": "tomatoes", "mode": "hybrid"}
            )

        worker = threading.Thread(target=search)
        worker.start()
        try:
            assert SlowQuery.entered.wait(WAIT)
            started = time.monotonic()
            stats = client.get("/api/stats")
            elapsed = time.monotonic() - started
            assert stats.status_code == 200
            assert elapsed < 1.0, f"/api/stats waited {elapsed:.2f}s behind the encoder"
        finally:
            SlowQuery.gate.set()
            worker.join(WAIT)
        response = result["response"]
        assert response.status_code == 200, response.text
        stages = {stage["name"]: stage["ms"] for stage in response.json()["meta"]["stages"]}
        assert stages["embed"] > 0


# -- finding 6: a registered collection named like the ad-hoc one ----------------------


def test_switching_to_registered_collection_with_the_adhoc_name(home, tmp_path, notes):
    library = Library(home)
    registered = library.create("Notes")
    adhoc = tmp_path / "elsewhere" / "notes.db"
    adhoc.parent.mkdir()
    with Index.open(adhoc) as index:
        ingest_path(index, notes)
    app, client = make_client(home, index_path=str(adhoc), register=False)
    with client:
        listing = client.get("/api/collections").json()["collections"]
        assert {(item["name"], item.get("registered", True)) for item in listing} >= {
            ("Notes", True),
            ("notes", False),
        }
        response = client.post("/api/collections/active", json={"name": "Notes"}, headers=CSRF)
        assert response.status_code == 200
        assert response.json()["collection"]["name"] == "Notes"
        assert response.json()["documents"] == 0
        assert Path(app.state.runtime.active["db"]) == Path(registered["db"])
        # The ad-hoc name itself is still a harmless no-op while it is open.


def test_activating_the_open_adhoc_collection_is_a_no_op(home, tmp_path, notes):
    adhoc = tmp_path / "solo.db"
    with Index.open(adhoc) as index:
        ingest_path(index, notes)
    app, client = make_client(home, index_path=str(adhoc), register=False)
    with client:
        response = client.post("/api/collections/active", json={"name": "solo"}, headers=CSRF)
        assert response.status_code == 200 and response.json()["documents"] == 2


# -- finding 7: vectors stored but sqlite-vec missing ----------------------------------


def test_stats_without_sqlite_vec_still_offer_keyword_search(home, notes, monkeypatch):
    pytest.importorskip("sqlite_vec")
    app, client = make_client(home)
    with client:
        index_folder(client, notes, embed=True)
    monkeypatch.setattr(store_module, "_try_load_vec", lambda conn: False)
    app, client = make_client(home)
    with client:
        stats = client.get("/api/stats").json()
        assert stats["documents"] == 2
        assert stats["modes"] == ["lexical"]
        assert stats["status"] == "ready"
        assert "sqlite-vec" in stats["status_detail"]
        assert stats["vectors_unavailable"] is True
        search = client.post("/api/search", json={"query": "tomatoes", "mode": "lexical"})
        assert search.status_code == 200 and search.json()["results"]


# -- finding 9/25: forgetting a missing folder ------------------------------------------


def test_remove_root_endpoint_forgets_a_missing_folder(home, tmp_path, notes):
    other = tmp_path / "other"
    other.mkdir()
    (other / "x.md").write_text("other note", encoding="utf-8")
    app, client = make_client(home)
    with client:
        index_folder(client, notes)
        job = client.post("/api/index", json={"path": str(other)}, headers=CSRF)
        assert wait_job(client, job.json()["id"])["status"] == "succeeded"
        (other / "x.md").unlink()
        other.rmdir()
        changes = client.get("/api/index/changes").json()
        missing = [r for r in changes["roots"] if r["exists"] is False]
        assert len(missing) == 1 and changes["stale"] is True
        response = client.post(
            "/api/roots/remove", json={"root_id": missing[0]["root_id"]}, headers=CSRF
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["removed"] == 1 and body["forgotten"] is True
        assert body["stats"]["documents"] == 2
        assert client.get("/api/index/changes").json()["stale"] is False
        unknown = client.post("/api/roots/remove", json={"root_id": 999}, headers=CSRF)
        assert unknown.status_code == 404
        by_path = client.post("/api/roots/remove", json={"root": str(notes)}, headers=CSRF)
        assert by_path.status_code == 200 and by_path.json()["removed"] == 2


# -- finding 13: creating a collection on an existing file -----------------------------


def test_create_collection_on_existing_file_suggests_opening_it(home, tmp_path):
    existing = tmp_path / "existing.db"
    Index.open(existing).close()
    app, client = make_client(home)
    with client:
        response = client.post(
            "/api/collections", json={"name": "New", "db_path": str(existing)}, headers=CSRF
        )
        assert response.status_code == 409
        error = response.json()["error"]
        assert error["code"] == "path_exists" and error["action"] == "open_collection"


# -- finding 14: opening a crafted collection never launches programs -------------------


def _crafted_collection(tmp_path, notes, source: str, root: str) -> Path:
    db = tmp_path / "crafted.db"
    with Index.open(db) as index:
        ingest_path(index, notes)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE collection_roots SET path = ?", (root,))
    conn.execute("UPDATE documents SET source = ? WHERE doc_id = 'garden.md'", (source,))
    conn.commit()
    conn.close()
    return db


def test_open_document_refuses_programs_from_a_crafted_collection(home, tmp_path, notes):
    payload_dir = tmp_path / "payload"
    payload_dir.mkdir()
    (payload_dir / "run.bat").write_text("@echo off\n", encoding="utf-8")
    db = _crafted_collection(tmp_path, notes, "run.bat", str(payload_dir.resolve()))
    opener = Opener()
    app, client = make_client(home, opener=opener)
    with client:
        opened = client.post("/api/collections/open", json={"db_path": str(db)}, headers=CSRF)
        assert opened.status_code == 200
        response = client.post("/api/documents/open", json={"doc_id": "garden.md"}, headers=CSRF)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "unsupported_file"
        assert opener.calls == []


def test_open_document_refuses_a_note_symlink_to_a_program(home, tmp_path, notes):
    (notes / "payload.exe").write_bytes(b"MZ")
    link = notes / "innocent.md"
    try:
        os.symlink(notes / "payload.exe", link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")
    db = _crafted_collection(tmp_path, notes, "innocent.md", str(notes.resolve()))
    opener = Opener()
    app, client = make_client(home, opener=opener)
    with client:
        client.post("/api/collections/open", json={"db_path": str(db)}, headers=CSRF)
        response = client.post("/api/documents/open", json={"doc_id": "garden.md"}, headers=CSRF)
        assert response.status_code == 403
        assert opener.calls == []


# -- finding 15: cross-site requests and network paths in the folder picker -------------


def test_cross_site_requests_are_blocked_even_for_gets(home, tmp_path):
    app, client = make_client(home)
    with client:
        for site in ("cross-site", "same-site"):
            response = client.get(
                "/api/fs/list", params={"path": str(tmp_path)}, headers={"Sec-Fetch-Site": site}
            )
            assert response.status_code == 403
            assert response.json()["error"]["code"] == "csrf_check_failed"
            search = client.post(
                "/api/search", json={"query": "x"}, headers={"Sec-Fetch-Site": site}
            )
            assert search.status_code == 403
        same_origin = client.get(
            "/api/fs/list", params={"path": str(tmp_path)}, headers={"Sec-Fetch-Site": "same-origin", "X-OneFind-Request": "1"}
        )
        assert same_origin.status_code == 200
        # Without the CSRF header the folder listing is refused, even same-origin.
        bare = client.get("/api/fs/list", params={"path": str(tmp_path)})
        assert bare.status_code == 403
        assert bare.json()["error"]["code"] == "csrf_check_failed"


def test_folder_picker_refuses_network_paths_without_touching_them(home, monkeypatch):
    touched: list[str] = []
    real_resolve = Path.resolve

    def spy(self, *args, **kwargs):
        touched.append(str(self))
        return real_resolve(self, *args, **kwargs)

    app, client = make_client(home)
    with client:
        monkeypatch.setattr(Path, "resolve", spy)
        for raw in (r"\\192.0.2.10\share", "//192.0.2.10/share", r"\\?\UNC\host\x", r"\\.\pipe\x"):
            started = time.monotonic()
            response = client.get(
                "/api/fs/list", params={"path": raw}, headers={"X-OneFind-Request": "1"}
            )
            assert response.status_code == 400, raw
            assert response.json()["error"]["code"] == "invalid_path"
            assert time.monotonic() - started < 1.0
        assert not any("192.0.2.10" in item or "host" in item for item in touched)


# -- finding 16: network roots of an opened collection are never contacted -------------


def test_network_roots_are_not_scanned_or_opened_automatically(home, tmp_path, notes, monkeypatch):
    db = _crafted_collection(tmp_path, notes, "garden.md", r"\\192.0.2.10\share\notes")
    opener = Opener()
    app, client = make_client(home, opener=opener)
    scanned: list[str] = []
    monkeypatch.setattr(serve_module, "scan_changes", lambda index, root, *a, **k: scanned.append(root))
    with client:
        opened = client.post("/api/collections/open", json={"db_path": str(db)}, headers=CSRF)
        assert opened.status_code == 200
        assert opened.json()["network_roots"] == [r"\\192.0.2.10\share\notes"]
        changes = client.get("/api/index/changes").json()
        assert scanned == []
        assert changes["roots"][0]["network"] is True and changes["stale"] is False
        response = client.post("/api/documents/open", json={"doc_id": "garden.md"}, headers=CSRF)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "confirmation_required"
        assert opener.calls == []
        reindex = client.post(
            "/api/index", json={"path": r"\\192.0.2.10\share\notes"}, headers=CSRF
        )
        assert reindex.status_code == 409
        assert reindex.json()["error"]["action"] == "confirm_network"


# -- finding 17: reset token handling ---------------------------------------------------


def test_reset_with_non_ascii_token_is_a_wrong_token(tmp_path):
    app = create_app(tmp_path / "r.db", reset_token="secret-token-123456")
    with TestClient(app) as client:
        response = client.post("/api/reset", json={"confirm_token": "\u00fc" * 20})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "wrong_token"
        good = client.post("/api/reset", json={"confirm_token": "secret-token-123456"})
        assert good.status_code == 200


@pytest.mark.parametrize("token", ["abc", "x" * 257])
def test_unusable_reset_tokens_are_rejected_up_front(tmp_path, token):
    with pytest.raises(UsageError, match="16-256"):
        create_app(tmp_path / "r.db", reset_token=token)


# -- finding 19: history counts strong results only ------------------------------------


def test_history_records_zero_for_a_weak_only_search(home, notes, monkeypatch):
    app, client = make_client(home)
    with client:
        index_folder(client, notes)
        runtime = app.state.runtime
        real = runtime._build_results

        def weak(index, outcome, payload):
            items = real(index, outcome, payload)
            for item in items:
                item["confidence"] = "weak"
            return items

        monkeypatch.setattr(runtime, "_build_results", weak)
        response = client.post("/api/search", json={"query": "tomatoes", "mode": "lexical"})
        assert response.status_code == 200 and response.json()["results"]
        history = client.get("/api/history").json()["items"]
        assert history[0]["query"] == "tomatoes" and history[0]["result_count"] == 0


# -- finding 28: "Update index" on a single-file root ----------------------------------


def test_update_of_a_single_file_root_does_not_pull_in_siblings(home, tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    for number in range(1, 4):
        (docs / f"d{number}.md").write_text(f"document {number}", encoding="utf-8")
    app, client = make_client(home)
    with client:
        client.post("/api/collections", json={"name": "S"}, headers=CSRF)
        job = client.post("/api/index", json={"path": str(docs / "d1.md")}, headers=CSRF)
        assert wait_job(client, job.json()["id"])["status"] == "succeeded"
        assert client.get("/api/index/changes").json()["stale"] is False
        update = client.post("/api/index", json={"path": str(docs)}, headers=CSRF)
        assert wait_job(client, update.json()["id"])["status"] == "succeeded"
        assert client.get("/api/stats").json()["documents"] == 1
        whole = client.post("/api/index", json={"path": str(docs), "scope": "folder"}, headers=CSRF)
        assert wait_job(client, whole.json()["id"])["status"] == "succeeded"
        assert client.get("/api/stats").json()["documents"] == 3
