"""T-11 acceptance: serve API contract via FastAPI TestClient."""

import pytest

pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")  # required by TestClient under starlette>=0.27

from scrydb.errors import UsageError  # noqa: E402
from scrydb.ingest import ingest_path  # noqa: E402
from scrydb.serve import create_app  # noqa: E402
from scrydb.store import Index  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    db = tmp_path / "demo.db"
    with Index.open(db) as idx:
        ingest_path(idx, "sample-data")
    from fastapi.testclient import TestClient

    app = create_app(db, reset_token="secret")
    with TestClient(app) as c:
        yield c, db


def test_root_serves_html(client):
    c, _ = client
    r = c.get("/")
    assert r.status_code == 200
    assert "scrydb demo" in r.text
    assert 'id="q"' in r.text


def test_stats_reports_documents(client):
    c, _ = client
    r = c.get("/api/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["documents"] == 4
    assert body["vectors"] == 0  # sample-data ingested without --embed


def test_lexical_search_roundtrip(client):
    c, _ = client
    r = c.post("/api/search", json={"query": "chlorophyll", "mode": "lexical"})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "lexical"
    assert body["results"][0]["doc_id"] == "photosynthesis"


def test_semantic_search_without_embeddings_returns_400(tmp_path):
    db = tmp_path / "lexonly.db"
    with Index.open(db) as idx:
        ingest_path(idx, "sample-data")
    from fastapi.testclient import TestClient

    app = create_app(db, reset_token="t")
    with TestClient(app) as c:
        r = c.post("/api/search", json={"query": "anything", "mode": "semantic"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "no_embeddings"


def test_reset_requires_correct_token(client):
    c, _ = client
    r = c.post("/api/reset", json={"confirm_token": "wrong"})
    assert r.status_code == 409
    r = c.post("/api/reset", json={"confirm_token": "secret"})
    assert r.status_code == 200
    assert r.json()["reset"] is True


def test_missing_index_serves_empty_stats(tmp_path):
    db = tmp_path / "ghost.db"
    from fastapi.testclient import TestClient

    app = create_app(db, reset_token="t")
    with TestClient(app) as c:
        r = c.get("/api/stats")
    assert r.status_code == 200
    assert r.json()["documents"] == 0
