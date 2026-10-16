"""Server/UI glue found during the live browser walkthrough (tools/ui_check.py).

Covers relaxed keyword matching in the HTTP API (``lexical_match="auto"``
and ``meta.relaxed``), the open-file success and refusal paths with an
injected opener, and static regressions in the frontend.
"""

from __future__ import annotations

import importlib.util
import re
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from onefind import serve
from onefind.jobs import EmbedderCache
from onefind.serve import create_app

CSRF = {"X-OneFind-Request": "1"}
ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "onefind" / "static"


class Opener:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, bool]] = []

    def __call__(self, path: Path, reveal: bool) -> None:
        self.calls.append((Path(path), reveal))


@pytest.fixture()
def notes(tmp_path):
    root = tmp_path / "notes"
    (root / "garden").mkdir(parents=True)
    (root / "rollback.md").write_text(
        "# Rollback plan\n\nHow to roll back a release safely. The owner signs off.\n",
        encoding="utf-8",
    )
    (root / "garden" / "tomatoes.md").write_text(
        "# Tomatoes\n\nTomatoes need sun and steady watering in summer.\n", encoding="utf-8"
    )
    (root / "garden" / "basil.md").write_text(
        "# Basil\n\nBasil grows next to the tomatoes and likes warm soil.\n", encoding="utf-8"
    )
    return root


def _client(home: Path, **kwargs):
    kwargs.setdefault("opener", Opener())
    app = create_app(None, home=str(home), embedders=EmbedderCache(factory=lambda name: None), **kwargs)
    return app, TestClient(app)


def _index(client, folder) -> None:
    assert client.post("/api/collections", json={"name": "Notes"}, headers=CSRF).status_code == 201
    assert client.post("/api/collections/active", json={"name": "Notes"}, headers=CSRF).status_code == 200
    started = client.post("/api/index", json={"path": str(folder), "embed": False}, headers=CSRF)
    assert started.status_code == 202, started.text
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{started.json()['id']}").json()
        if job["status"] in {"succeeded", "failed", "cancelled"}:
            assert job["status"] == "succeeded", job
            return
        time.sleep(0.02)
    raise AssertionError("indexing did not finish")


@pytest.fixture()
def app_client(tmp_path, notes):
    opener = Opener()
    app, client = _client(tmp_path / "home", opener=opener)
    with client:
        _index(client, notes)
        yield app, client, opener


# ---- relaxed keyword matching ------------------------------------------------------------


def test_keyword_search_tops_up_with_partial_matches(app_client):
    _app, client, _opener = app_client
    response = client.post("/api/search", json={"query": "tomatoes zeppelin", "mode": "lexical"})
    assert response.status_code == 200, response.text
    payload = response.json()
    titles = {item["title"] for item in payload["results"]}
    assert {"Tomatoes", "Basil"} <= titles
    assert payload["meta"]["relaxed"] is True
    assert payload["meta"]["strict_count"] == 0


def test_all_words_matches_rank_before_partial_ones(app_client):
    _app, client, _opener = app_client
    payload = client.post("/api/search", json={"query": "basil tomatoes", "mode": "lexical"}).json()
    assert [item["title"] for item in payload["results"]] == ["Basil", "Tomatoes"]
    assert payload["meta"]["strict_count"] == 1
    assert payload["meta"]["relaxed"] is True  # topped up below the all-words hit


def test_enough_all_words_matches_are_not_relaxed(app_client):
    _app, client, _opener = app_client
    payload = client.post("/api/search", json={"query": "basil tomatoes", "mode": "lexical", "k": 1}).json()
    assert [item["title"] for item in payload["results"]] == ["Basil"]
    assert payload["meta"]["relaxed"] is False


def test_no_word_matches_gives_an_empty_result(app_client):
    _app, client, _opener = app_client
    payload = client.post("/api/search", json={"query": "zeppelin flibbertigibbet", "mode": "lexical"}).json()
    assert payload["results"] == []
    assert payload["meta"]["strict_count"] == 0


def test_stream_result_carries_relaxed(app_client):
    _app, client, _opener = app_client
    response = client.post("/api/search/stream", json={"query": "tomatoes zeppelin", "mode": "lexical"})
    lines = [line for line in response.text.splitlines() if line.strip()]
    import json

    final = json.loads(lines[-1])
    assert final["type"] == "result"
    assert final["meta"]["relaxed"] is True


@pytest.mark.parametrize(("mode", "expected"), [("lexical", "auto"), ("hybrid", "auto"), ("semantic", "all")])
def test_search_passes_lexical_match_by_mode(app_client, monkeypatch, mode, expected):
    _app, client, _opener = app_client
    seen = {}
    real = serve.search_detailed

    def spy(index, query, **kwargs):
        seen.update(kwargs)
        kwargs["mode"] = "lexical"  # the fixture has no vectors
        return real(index, query, **kwargs)

    monkeypatch.setattr(serve, "search_detailed", spy)
    if mode != "lexical":
        # Pretend the collection has vectors so the request reaches the engine.
        runtime = _app.state.runtime
        monkeypatch.setattr(runtime, "ensure_embedder", lambda name: 0.0)
        monkeypatch.setattr(runtime, "bind_embedder", lambda index: setattr(index, "embedder", object()))
        with runtime.lock:
            index = runtime.current_index()
            index.set_meta("model_name", "fake")
            monkeypatch.setattr(type(index), "count", lambda self, table: 3)
            monkeypatch.setattr(type(index), "vec_ready", property(lambda self: True), raising=False)
    response = client.post("/api/search", json={"query": "tomatoes", "mode": mode})
    assert response.status_code == 200, response.text
    assert seen["lexical_match"] == expected


# ---- opening files ----------------------------------------------------------------------


def test_open_success_records_recent_document(app_client, notes):
    _app, client, opener = app_client
    response = client.post("/api/documents/open", json={"doc_id": "garden/basil.md"}, headers=CSRF)
    assert response.status_code == 200, response.text
    assert response.json() == {"opened": True, "doc_id": "garden/basil.md", "reveal": False}
    assert opener.calls == [((notes / "garden" / "basil.md").resolve(), False)]
    recent = client.get("/api/recent-documents").json()["items"]
    assert recent[0]["doc_id"] == "garden/basil.md"
    assert recent[0]["last_opened_at"]
    assert "modified_at" in recent[0] and "mtime" not in recent[0]


def test_open_refused_when_disabled(tmp_path, notes):
    opener = Opener()
    _app, client = _client(tmp_path / "home", opener=opener, allow_open=False)
    with client:
        _index(client, notes)
        response = client.post("/api/documents/open", json={"doc_id": "rollback.md"}, headers=CSRF)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "open_disabled"
    assert opener.calls == []


def test_open_missing_file_suggests_reindex(app_client, notes):
    _app, client, opener = app_client
    (notes / "rollback.md").unlink()
    response = client.post("/api/documents/open", json={"doc_id": "rollback.md"}, headers=CSRF)
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "file_missing" and error["action"] == "reindex"
    assert opener.calls == []


# ---- frontend regressions ----------------------------------------------------------------


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_toasts_move_into_the_topmost_modal_dialog():
    # A modal <dialog> makes the rest of the page inert and covers it, so a
    # toast in <body> would be invisible behind the Manage panel.
    dialogs = _read("dialogs.js")
    assert "export function placeToasts" in dialogs
    assert "modalStack.push(dialog)" in dialogs
    assert "placeToasts" in _read("toast.js")


def test_full_screen_panels_do_not_use_100vw():
    # 100vw includes a classic scrollbar: a fixed pane at right:0 then starts
    # off-screen on Windows.
    css = _read("styles.css")
    assert not re.search(r"\.preview-pane\s*\{[^}]*width:\s*100vw", css)
    assert not re.search(r"\.dialog\s*\{[^}]*width:\s*100vw", css)
    assert "width: min(600px, 100vw)" not in css


def test_relaxed_results_note_is_wired():
    assert 'id="results-note"' in _read("index.html")
    assert "strictCount" in _read("views-results.js")
    assert "strictCount" in _read("views-states.js")


def test_ui_check_tool_is_importable():
    pytest.importorskip("playwright")
    spec =importlib.util.spec_from_file_location("ui_check", ROOT / "tools" / "ui_check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    args = module.parse_args(["--scratch", str(ROOT / "build-ui-check"), "--port", "9999"])
    assert args.port == 9999
    assert args.out == (ROOT / "build-ui-check" / "shots").resolve()
