"""FastAPI contract, packaged assets, headers, and untrusted-content safety."""

import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient
from onefind.ingest import ingest_path
from onefind.serve import create_app
from onefind.store import Document, Index


@pytest.fixture()
def client(tmp_path):
    db = tmp_path / "demo.db"
    with Index.open(db) as index:
        ingest_path(index, "sample-data")
    app = create_app(db, reset_token="a-secure-test-token")
    with TestClient(app) as test_client:
        yield test_client, db


def test_root_serves_packaged_accessible_ui(client):
    test_client, _db = client
    response = test_client.get("/")
    assert response.status_code == 200
    assert "OneFind" in response.text
    assert 'id="search-input"' in response.text
    assert '/assets/app.js' in response.text
    assert "onsubmit=" not in response.text
    assert "onclick=" not in response.text


def test_static_assets_and_security_headers(client):
    test_client, _db = client
    css = test_client.get("/assets/styles.css")
    js = test_client.get("/assets/app.js")
    favicon = test_client.get("/assets/favicon.svg")
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    assert js.status_code == 200
    assert "text/javascript" in js.headers["content-type"]
    assert favicon.status_code == 200
    assert favicon.headers["content-type"].startswith("image/svg+xml")
    assert "script-src 'self'" in css.headers["content-security-policy"]
    assert css.headers["x-content-type-options"] == "nosniff"
    assert css.headers["x-frame-options"] == "DENY"
    assert test_client.get("/assets/../pyproject.toml").status_code in {307, 404}


def test_javascript_never_uses_response_html_sink(client):
    test_client, _db = client
    script = test_client.get("/assets/app.js").text
    assert ".innerHTML" not in script
    assert "insertAdjacentHTML" not in script
    assert "replaceChildren" in script
    assert "AbortController" in script


def test_stats_reports_capabilities_without_filesystem_path(client):
    test_client, _db = client
    response = test_client.get("/api/stats")
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["documents"] == 20
    assert body["modes"] == ["lexical"]
    assert body["precisions"] == []
    assert body["health"]["ok"] is True
    assert "path" not in body
    assert str(_db) not in response.text


def test_lexical_search_returns_structured_safe_snippet(client):
    test_client, _db = client
    response = test_client.post(
        "/api/search",
        json={"query": "chlorophyll", "mode": "lexical", "k": 5},
    )
    assert response.status_code == 200
    body = response.json()
    hit = body["results"][0]
    assert hit["doc_id"] == "photosynthesis.md"
    assert isinstance(hit["score"]["value"], float)
    assert hit["score"]["metric"] == "BM25 score"
    assert isinstance(hit["snippet"]["segments"], list)
    assert any(segment["highlight"] for segment in hit["snippet"]["segments"])
    assert body["meta"]["mode"] == "lexical"
    assert body["meta"]["precision"] is None


def test_untrusted_html_is_returned_as_text_segments(tmp_path):
    db = tmp_path / "unsafe.db"
    payload = '<img src=x onerror="alert(1)"> trigger <script>alert(2)</script>'
    with Index.open(db) as index:
        index.add_documents([Document("unsafe.md", "Unsafe title", payload)])
    app = create_app(db)
    with TestClient(app) as test_client:
        response = test_client.post(
            "/api/search",
            json={"query": "trigger", "mode": "lexical"},
        )
    assert response.status_code == 200
    text = json.dumps(response.json())
    assert "<img" in text and "<script>" in text
    assert response.headers["content-security-policy"].startswith("default-src 'self'")


def test_semantic_search_without_embeddings_uses_error_envelope(tmp_path):
    db = tmp_path / "lexonly.db"
    with Index.open(db) as index:
        index.add_documents([Document("d", "Title", "body")])
    app = create_app(db)
    with TestClient(app) as test_client:
        response = test_client.post(
            "/api/search", json={"query": "anything", "mode": "semantic"}
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "no_embeddings"
    assert "detail" not in response.json()


def test_validation_errors_use_stable_envelope(client):
    test_client, _db = client
    response = test_client.post(
        "/api/search", json={"query": "   ", "mode": "lexical"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_reset_requires_explicit_enable_and_correct_token(client):
    test_client, db = client
    wrong = test_client.post(
        "/api/reset", json={"confirm_token": "wrong-but-long-enough"}
    )
    assert wrong.status_code == 409
    assert wrong.json()["error"]["code"] == "wrong_token"
    assert db.exists()

    correct = test_client.post(
        "/api/reset", json={"confirm_token": "a-secure-test-token"}
    )
    assert correct.status_code == 200
    assert correct.json() == {"reset": True}
    assert db.exists()
    assert test_client.get("/api/stats").json()["documents"] == 0

    disabled_app = create_app(db)
    with TestClient(disabled_app) as disabled:
        response = disabled.post(
            "/api/reset", json={"confirm_token": "a-secure-test-token"}
        )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "reset_disabled"


def test_missing_index_serves_capability_aware_empty_stats(tmp_path):
    db = tmp_path / "ghost.db"
    app = create_app(db)
    with TestClient(app) as test_client:
        response = test_client.get("/api/stats")
        search = test_client.post(
            "/api/search", json={"query": "test", "mode": "lexical"}
        )
    assert response.status_code == 200
    assert response.json()["ready"] is False
    assert response.json()["modes"] == []
    assert search.status_code == 503
    assert search.json()["error"]["code"] == "no_index"
