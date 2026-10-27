"""v1.2.1 HTTP wiring: passage chunking in /api/stats, rebuild-vectors jobs,
the folder list with saved exclusions, the index collection guard, and the
folder picker's .jsonl flag (docs/11 sections 6, 9 and 10).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from test_serve_v3 import (  # noqa: E402 - shared fakes and fixtures
    CSRF,
    WAIT,
    GatedEmbedder,
    home,  # noqa: F401 - fixture
    index_folder,
    make_client,
    notes,  # noqa: F401 - fixture
    wait_job,
)

from onefind.store import Index

LONG_SENTENCE = "Day {n} at the weekly market: bread, cheese and apples were on sale early."


def write_long_note(folder: Path, sentences: int = 60) -> Path:
    """A note of about 14 words x ``sentences`` (well over one 200-word passage)."""
    body = "\n\n".join(
        " ".join(LONG_SENTENCE.format(n=n * 4 + i) for i in range(4)) for n in range(sentences // 4)
    )
    path = folder / "journal" / "long-market-diary.md"
    path.write_text(f"# Market diary\n\n{body}\n\nThe quokka lives on Rottnest Island.\n", encoding="utf-8")
    return path


def active_db(client) -> Path:
    entry = next(item for item in client.get("/api/collections").json()["collections"] if item["active"])
    return Path(entry["db"])


def old_style_collection(client, folder, name="Old notes") -> Path:
    """A collection whose passage setting is off (as before v1.2.1), indexed with vectors."""
    assert client.post("/api/collections", json={"name": name}, headers=CSRF).status_code == 201
    assert client.post("/api/collections/active", json={"name": name}, headers=CSRF).status_code == 200
    db = active_db(client)
    with Index.open(db) as index:
        index.set_chunking(None)
    started = client.post("/api/index", json={"path": str(folder), "embed": True}, headers=CSRF)
    assert started.status_code == 202, started.text
    assert wait_job(client, started.json()["id"])["status"] == "succeeded"
    return db


# ---- /api/stats ------------------------------------------------------------------------


def test_stats_report_passages_without_the_db_path(home, notes):
    write_long_note(notes)
    _app, client = make_client(home)
    with client:
        index_folder(client, notes, embed=True)
        response = client.get("/api/stats")
        stats = response.json()
        assert stats["chunking"] == {"version": 1, "max_words": 200, "overlap_words": 40}
        assert stats["long_notes"] == 1  # notes longer than one passage
        assert stats["passages"] > stats["documents"]
        assert stats["notes_over_model_window"] == 0  # every passage fits the model
        rebuild = stats["vectors_need_rebuild"]
        assert rebuild["needed"] is False and rebuild["recommended"] is False
        assert set(rebuild) >= {"needed", "documents", "reason", "chunking", "long_notes", "recommended"}
        db = str(active_db(client))
        assert db not in response.text and json.dumps(db)[1:-1] not in response.text


def test_stats_of_an_empty_library_have_passage_defaults(home):
    _app, client = make_client(home)
    with client:
        stats = client.get("/api/stats").json()
        assert stats["chunking"] is None and stats["vectors_need_rebuild"] is None
        assert stats["long_notes"] == 0 and stats["passages"] == 0


# ---- POST /api/maintenance/rebuild-vectors ---------------------------------------------------


def test_rebuild_vectors_turns_on_passage_search_for_an_old_collection(home, notes):
    write_long_note(notes)
    app, client = make_client(home)
    with client:
        old_style_collection(client, notes)
        before = client.get("/api/stats").json()
        assert before["chunking"] is None
        assert before["vectors_need_rebuild"]["recommended"] is True
        assert before["vectors_need_rebuild"]["needed"] is False
        assert before["long_notes"] == 1
        assert before["passages"] == before["documents"]

        preempted = []
        original = app.state.runtime.autosync.preempt
        app.state.runtime.autosync.preempt = lambda db, **kw: preempted.append(str(db)) or original(db, **kw)

        assert client.post("/api/maintenance/rebuild-vectors", json={"chunking": "default"}).status_code == 403
        started = client.post("/api/maintenance/rebuild-vectors", json={"chunking": "default"}, headers=CSRF)
        assert started.status_code == 202, started.text
        job = started.json()
        assert job["kind"] == "rebuild_vectors" and "db" not in job
        assert preempted, "a manual rebuild must pre-empt auto-sync like other manual work"
        finished = wait_job(client, job["id"])
        assert finished["status"] == "succeeded", finished
        assert finished["result"]["rebuilt"] == 1 and finished["result"]["vectors_embedded"] > 1
        assert [step["status"] for step in finished["steps"]] == ["done"] * 4

        after = client.get("/api/stats").json()
        assert after["chunking"]["max_words"] == 200
        assert after["vectors_need_rebuild"]["needed"] is False
        assert after["vectors_need_rebuild"]["recommended"] is False
        assert after["passages"] > after["documents"]
        assert after["status"] == "ready"

        found = client.post("/api/search", json={"query": "quokka Rottnest Island", "mode": "semantic", "k": 10}).json()
        passages = [item["passage"] for item in found["results"] if item["doc_id"].endswith("long-market-diary.md")]
        assert passages and passages[0] is not None
        passage = passages[0]
        assert passage["count"] >= 2 and 0 <= passage["seq"] < passage["count"]
        body = client.get("/api/documents/journal/long-market-diary.md").json()["body"]
        assert passage["text"] in body.replace("\r\n", "\n")
        short = [item for item in found["results"] if not item["doc_id"].endswith("long-market-diary.md")]
        assert short and all(item["passage"] is None for item in short)


def test_rebuild_needed_after_a_setting_change_shows_in_status_and_clears(home, notes):
    write_long_note(notes)
    _app, client = make_client(home)
    with client:
        index_folder(client, notes, embed=True)
        with Index.open(active_db(client)) as index:
            index.set_chunking({"max_words": 120, "overlap_words": 20})
        stats = client.get("/api/stats").json()
        assert stats["vectors_need_rebuild"]["needed"] is True
        assert stats["vectors_need_rebuild"]["documents"] == 1
        assert "need a rebuild" in stats["status_detail"]
        started = client.post("/api/maintenance/rebuild-vectors", json={}, headers=CSRF)
        assert started.status_code == 202
        assert started.json()["options"]["chunking"] == "keep"
        assert wait_job(client, started.json()["id"])["status"] == "succeeded"
        stats = client.get("/api/stats").json()
        assert stats["vectors_need_rebuild"]["needed"] is False
        assert stats["chunking"]["max_words"] == 120
        assert "need a rebuild" not in stats["status_detail"]


def test_rebuild_vectors_off_goes_back_to_one_vector_per_note(home, notes):
    write_long_note(notes)
    _app, client = make_client(home)
    with client:
        index_folder(client, notes, embed=True)
        started = client.post("/api/maintenance/rebuild-vectors", json={"chunking": "off"}, headers=CSRF)
        assert started.status_code == 202
        assert started.json()["options"]["chunking"] is None
        assert wait_job(client, started.json()["id"])["status"] == "succeeded"
        stats = client.get("/api/stats").json()
        assert stats["chunking"] is None and stats["passages"] == stats["documents"]
        assert stats["vectors_need_rebuild"]["recommended"] is True


def test_rebuild_vectors_validation_and_no_collection(home, notes):
    _app, client = make_client(home)
    with client:
        none = client.post("/api/maintenance/rebuild-vectors", json={}, headers=CSRF)
        assert none.status_code == 503 and none.json()["error"]["code"] == "no_index"
        index_folder(client, notes)
        bad = client.post("/api/maintenance/rebuild-vectors", json={"chunking": "huge"}, headers=CSRF)
        assert bad.status_code == 422 and bad.json()["error"]["code"] == "validation_error"
        # A keyword-only collection re-splits passages without the model.
        ok = client.post("/api/maintenance/rebuild-vectors", json={"chunking": "keep"}, headers=CSRF)
        assert ok.status_code == 202
        job = wait_job(client, ok.json()["id"])
        assert job["status"] == "succeeded"
        assert {step["name"]: step["status"] for step in job["steps"]}["vectors"] == "skipped"


def test_rebuild_vectors_is_refused_while_a_job_runs(home, notes):
    GatedEmbedder.gate.clear()
    GatedEmbedder.entered.clear()
    _app, client = make_client(home, factory=GatedEmbedder)
    with client:
        try:
            assert client.post("/api/collections", json={"name": "Busy"}, headers=CSRF).status_code == 201
            started = client.post("/api/index", json={"path": str(notes), "embed": True}, headers=CSRF)
            assert started.status_code == 202
            assert GatedEmbedder.entered.wait(WAIT)
            blocked = client.post("/api/maintenance/rebuild-vectors", json={"chunking": "default"}, headers=CSRF)
            assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "collection_busy"
        finally:
            GatedEmbedder.gate.set()
        assert wait_job(client, started.json()["id"])["status"] == "succeeded"


def test_a_running_rebuild_is_reported_in_stats(home, notes):
    write_long_note(notes)
    GatedEmbedder.gate.set()
    _app, client = make_client(home, factory=GatedEmbedder)
    with client:
        old_style_collection(client, notes, name="Gated")
        GatedEmbedder.gate.clear()
        GatedEmbedder.entered.clear()
        try:
            started = client.post("/api/maintenance/rebuild-vectors", json={"chunking": "default"}, headers=CSRF)
            assert started.status_code == 202
            assert GatedEmbedder.entered.wait(WAIT)
            stats = client.get("/api/stats").json()
            assert stats["status"] == "indexing"
            assert stats["status_detail"].startswith("Rebuilding vectors")
            assert stats["job"]["kind"] == "rebuild_vectors" and "db" not in stats["job"]
            again = client.post("/api/index", json={"path": str(notes)}, headers=CSRF)
            assert again.status_code == 409
        finally:
            GatedEmbedder.gate.set()
        assert wait_job(client, started.json()["id"])["status"] == "succeeded"


# ---- GET /api/roots, POST /api/index collection guard ------------------------------------------


def test_roots_list_saved_exclusions_without_scanning(home, notes):
    _app, client = make_client(home)
    with client:
        assert client.get("/api/roots").json() == {"roots": []}
        assert client.post("/api/collections", json={"name": "Notes"}, headers=CSRF).status_code == 201
        started = client.post(
            "/api/index", json={"path": str(notes), "embed": False, "exclude": ["journal", "*.txt"]}, headers=CSRF
        )
        assert wait_job(client, started.json()["id"])["status"] == "succeeded"
        roots = client.get("/api/roots").json()["roots"]
        assert len(roots) == 1
        root = roots[0]
        assert Path(root["path"]) == notes.resolve()
        assert root["exclude"] == ["journal", "*.txt"]
        assert root["documents"] == 2 and root["network"] is False and root["files_only"] is False


def test_index_refuses_a_request_meant_for_another_collection(home, notes):
    _app, client = make_client(home)
    with client:
        for name in ("First", "Second"):
            assert client.post("/api/collections", json={"name": name}, headers=CSRF).status_code == 201
        assert client.post("/api/collections/active", json={"name": "Second"}, headers=CSRF).status_code == 200
        wrong = client.post("/api/index", json={"path": str(notes), "collection": "First"}, headers=CSRF)
        assert wrong.status_code == 409
        assert wrong.json()["error"]["code"] == "collection_changed"
        assert "First" in wrong.json()["error"]["message"]
        assert client.get("/api/jobs").json()["jobs"] == []
        right = client.post("/api/index", json={"path": str(notes), "collection": "second"}, headers=CSRF)
        assert right.status_code == 202, right.text
        assert wait_job(client, right.json()["id"])["status"] == "succeeded"
        # Without the field the request goes to the active collection, as before.
        plain = client.post("/api/index", json={"path": str(notes)}, headers=CSRF)
        assert plain.status_code == 202
        wait_job(client, plain.json()["id"])


def test_index_with_a_collection_name_but_none_open(home, notes):
    _app, client = make_client(home)
    with client:
        response = client.post("/api/index", json={"path": str(notes), "collection": "Gone"}, headers=CSRF)
        assert response.status_code == 409 and response.json()["error"]["code"] == "collection_changed"


# ---- GET /api/fs/list .jsonl ---------------------------------------------------------------


def test_fs_list_marks_jsonl_datasets_supported(home, tmp_path):
    folder = tmp_path / "datasets"
    folder.mkdir()
    (folder / "facts.jsonl").write_text('{"_id": "1", "text": "x"}\n', encoding="utf-8")
    (folder / "notes.md").write_text("x", encoding="utf-8")
    (folder / "image.png").write_bytes(b"x")
    _app, client = make_client(home)
    with client:
        body = client.get("/api/fs/list", params={"path": str(folder)}, headers=CSRF).json()
        supported = {entry["name"]: entry["supported"] for entry in body["entries"]}
        assert supported == {"facts.jsonl": True, "image.png": False, "notes.md": True}
        # Indexing the folder reads notes only; a dataset is indexed when picked on its own.
        assert body["supported_files"] == 1

