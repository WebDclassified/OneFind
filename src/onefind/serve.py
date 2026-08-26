"""T-11: localhost-only FastAPI demo for OneFind (docs/03 + docs/04).

Architecture
- Static UI from `demo/index.html` served at GET /
- JSON API at /api/* with three endpoints (stats, search, reset)
- Embedder loaded lazily on the first semantic/hybrid query so cold
  startup is fast; subsequent queries reuse the loaded model.
- Hard-bound to 127.0.0.1; no auth, no file traversal.

This module is imported only when the [serve] extra is installed; the
`serve` CLI command raises a clean UsageError if FastAPI is missing.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

from .errors import ScrydbError, UsageError
from .store import Index

_DEMO_HTML = Path(__file__).resolve().parent.parent.parent / "demo" / "index.html"

# Optional pydantic at module level so the models are not local to
# `create_app` (pydantic forward-ref resolution chokes on local classes).
try:
    from fastapi import Body, FastAPI, HTTPException
    from fastapi.responses import FileResponse, HTMLResponse
    from pydantic import BaseModel, Field, field_validator
except ImportError:  # pragma: no cover - serve extra not installed
    BaseModel = Field = field_validator = Body = FastAPI = HTTPException = None  # type: ignore
    FileResponse = HTMLResponse = None  # type: ignore

    class _MissingPydantic:
        def __getattr__(self, name):  # pragma: no cover
            raise UsageError('serve needs the [serve] extra - pip install -e ".[serve]"')

    BaseModel = _MissingPydantic()  # type: ignore


if BaseModel is not None and isinstance(BaseModel, type):

    class SearchBody(BaseModel):  # type: ignore[misc, valid-type]
        query: str = Field(min_length=1, max_length=512)
        mode: str = Field(default="hybrid")
        precision: str = Field(default="float")
        k: int = Field(default=10, ge=1, le=50)

        @field_validator("mode")
        @classmethod
        def _mode_ok(cls, v: str) -> str:
            if v not in ("lexical", "semantic", "hybrid"):
                raise ValueError("mode must be one of: lexical, semantic, hybrid")
            return v

        @field_validator("precision")
        @classmethod
        def _prec_ok(cls, v: str) -> str:
            if v not in ("float", "int8", "binary"):
                raise ValueError("precision must be one of: float, int8, binary")
            return v

    class ResetBody(BaseModel):  # type: ignore[misc, valid-type]
        confirm_token: str


def create_app(index_path: str | Path, *, reset_token: str | None = None):  # type: ignore[no-untyped-def]
    if BaseModel is None or not isinstance(BaseModel, type):
        raise UsageError('serve needs the [serve] extra - pip install -e ".[serve]"')
    if SearchBody is None:  # type: ignore[unreachable]
        raise UsageError('serve needs the [serve] extra - pip install -e ".[serve]"')

    app = FastAPI(title="OneFind demo", docs_url=None, redoc_url=None)
    target = Path(index_path)
    _state: dict = {"index": None, "embedder_loaded": False, "reset_token": reset_token or uuid.uuid4().hex}

    @app.on_event("startup")
    def _startup() -> None:
        if not target.exists():
            return  # serve a "no index yet" page; /api/stats will explain
        _state["index"] = Index.open(target)

    @app.on_event("shutdown")
    def _shutdown() -> None:
        if _state["index"] is not None:
            _state["index"].close()

    @app.get("/", response_class=HTMLResponse)
    def root() -> str:
        if _DEMO_HTML.exists():
            return _DEMO_HTML.read_text(encoding="utf-8")
        return "<h1>OneFind demo</h1><p>index.html not found alongside the package.</p>"

    @app.get("/api/stats")
    def stats() -> dict:
        if _state["index"] is None:
            return {"documents": 0, "vectors": 0, "model": None,
                    "path": str(target)}
        stats_ = _state["index"].stats()
        return {
            "documents": stats_["documents"],
            "vectors":   stats_.get("vectors", 0),
            "model":     stats_.get("model"),
            "path":      stats_["path"],
        }

    @app.post("/api/search")
    def search(payload: SearchBody = Body(...)) -> dict:
        if _state["index"] is None:
            raise HTTPException(status_code=503, detail={
                "code": "no_index",
                "message": (f"no index at {target}. Run "
                            f"'OneFind index <path> --db {target} --embed' first."),
            })
        from .embed import SentenceEmbedder
        from .search import search as run_search

        index = _state["index"]
        body = payload
        if body.mode in ("semantic", "hybrid"):
            model_name = index.get_meta("model_name")
            if model_name is None:
                raise HTTPException(status_code=400, detail={
                    "code": "no_embeddings",
                    "message": "this index has no embeddings; rebuild with --embed",
                })
            if not _state["embedder_loaded"]:
                index.attach_embedder(SentenceEmbedder(model_name))
                _state["embedder_loaded"] = True

        try:
            start = time.perf_counter()
            hits = run_search(
                index, body.query, mode=body.mode, k=body.k, precision=body.precision,
            )
            took_ms = (time.perf_counter() - start) * 1000
        except ScrydbError as exc:
            raise HTTPException(status_code=400, detail={
                "code": "search_error", "message": str(exc),
            }) from exc

        return {
            "results": [
                {"rank": i + 1, "doc_id": h.doc_id, "title": h.title,
                 "score": h.score, "snippet": h.snippet}
                for i, h in enumerate(hits)
            ],
            "took_ms": round(took_ms, 1),
            "mode": body.mode,
            "precision": body.precision,
        }

    @app.post("/api/reset")
    def reset(payload: ResetBody = Body(...)) -> dict:
        if payload.confirm_token != _state["reset_token"]:
            raise HTTPException(status_code=409, detail={
                "code": "wrong_token", "message": "confirm_token mismatch",
            })
        if _state["index"] is not None:
            _state["index"].close()
        target.unlink(missing_ok=True)
        _state["index"] = Index.open(target)
        return {"reset": True, "path": str(target)}

    return app
