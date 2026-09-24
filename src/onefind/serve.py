"""Secure localhost web application for OneFind.

The frontend is packaged under ``onefind/static`` and uses only local HTML,
CSS, and JavaScript. It has no CDN, font, analytics, or cloud dependency.
"""

from __future__ import annotations

import math
import re
import secrets
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from .errors import OneFindError, UsageError
from .store import Index

_STATIC_DIR = Path(__file__).with_name("static")
_FTS_START = "\ue000"
_FTS_END = "\ue001"

try:
    from fastapi import Body, FastAPI, Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import FileResponse, JSONResponse, Response
    from pydantic import BaseModel, Field, field_validator
    from starlette.middleware.trustedhost import TrustedHostMiddleware

    SERVE_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without [serve]
    Body = FastAPI = Request = BaseModel = Field = field_validator = None  # type: ignore[assignment]
    RequestValidationError = None  # type: ignore[assignment]
    FileResponse = JSONResponse = Response = TrustedHostMiddleware = None  # type: ignore[assignment]
    SERVE_AVAILABLE = False


if SERVE_AVAILABLE:

    class SearchBody(BaseModel):  # type: ignore[misc, valid-type]
        query: str = Field(min_length=1, max_length=512)
        mode: str = Field(default="hybrid")
        precision: str = Field(default="float")
        k: int = Field(default=10, ge=1, le=50)

        @field_validator("query")
        @classmethod
        def _query_ok(cls, value: str) -> str:
            value = value.strip()
            if not value:
                raise ValueError("query must contain non-whitespace characters")
            return value

        @field_validator("mode")
        @classmethod
        def _mode_ok(cls, value: str) -> str:
            if value not in {"lexical", "semantic", "hybrid"}:
                raise ValueError("mode must be lexical, semantic, or hybrid")
            return value

        @field_validator("precision")
        @classmethod
        def _precision_ok(cls, value: str) -> str:
            if value not in {"float", "int8", "binary"}:
                raise ValueError("precision must be float, int8, or binary")
            return value

    class ResetBody(BaseModel):  # type: ignore[misc, valid-type]
        confirm_token: str = Field(min_length=16, max_length=256)

else:  # pragma: no cover
    SearchBody = ResetBody = None  # type: ignore[assignment]


def _error(status_code: int, code: str, message: str, retryable: bool = False) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "retryable": retryable,
            }
        },
    )


def _collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _segments_with_terms(text: str, query: str) -> list[dict[str, object]]:
    """Split plain text into safe highlighted segments using Python offsets."""
    text = _collapse_whitespace(text)
    tokens = sorted(
        {
            token.casefold()
            for token in re.findall(r"[\w'-]+", query, flags=re.UNICODE)
            if len(token.strip()) >= 2
        },
        key=len,
        reverse=True,
    )
    if not tokens or len(text) > 1200:
        text = text[:1200]
        return [{"text": text, "highlight": False}] if text else []

    lowered = text.casefold()
    ranges: list[tuple[int, int]] = []
    for token in tokens:
        start = 0
        while (position := lowered.find(token, start)) != -1:
            ranges.append((position, position + len(token)))
            start = position + len(token)
            if len(ranges) >= 200:
                break
    if not ranges:
        return [{"text": text, "highlight": False}]

    ranges.sort()
    merged: list[list[int]] = []
    for start, end in ranges:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    segments: list[dict[str, object]] = []
    cursor = 0
    for start, end in merged[:80]:
        if start > cursor:
            segments.append({"text": text[cursor:start], "highlight": False})
        segments.append({"text": text[start:end], "highlight": True})
        cursor = end
    if cursor < len(text):
        segments.append({"text": text[cursor:], "highlight": False})
    return segments


def _segments_from_fts(snippet: str) -> list[dict[str, object]]:
    segments: list[dict[str, object]] = []
    buffer: list[str] = []
    highlighted = False
    for character in snippet or "":
        if character == _FTS_START:
            if buffer:
                segments.append({"text": "".join(buffer), "highlight": highlighted})
                buffer = []
            highlighted = True
        elif character == _FTS_END:
            if buffer:
                segments.append({"text": "".join(buffer), "highlight": True})
                buffer = []
            highlighted = False
        else:
            buffer.append(character)
    if buffer:
        segments.append({"text": "".join(buffer), "highlight": highlighted})
    return segments or [{"text": "", "highlight": False}]


def _plain_excerpt(body: str, query: str, limit: int = 320) -> str:
    text = _collapse_whitespace(body)
    if len(text) <= limit:
        return text
    tokens = [token.casefold() for token in re.findall(r"\w+", query.lower()) if len(token) >= 3]
    positions = [text.casefold().find(token) for token in tokens]
    positions = [position for position in positions if position >= 0]
    focus = min(positions) if positions else 0
    start = max(0, focus - limit // 3)
    end = min(len(text), start + limit)
    excerpt = text[start:end].strip()
    return ("… " if start else "") + excerpt + (" …" if end < len(text) else "")


def _score_metric(mode: str, precision: str | None) -> str:
    if mode == "lexical":
        return "BM25 score"
    if mode == "semantic":
        return "Hamming distance" if precision == "binary" else "cosine similarity"
    return "RRF fusion score"


def create_app(
    index_path: str | Path,
    *,
    reset_token: str | None = None,
    allowed_hosts: list[str] | None = None,
):
    """Create the local FastAPI application without opening network services."""
    if not SERVE_AVAILABLE:
        raise UsageError('serve needs the [serve] extra - pip install -e ".[serve]"')

    target = Path(index_path)
    if target.is_symlink():
        raise UsageError("refusing to serve a symlink as the index database")

    _state: dict[str, object] = {
        "index": None,
        "embedder_loaded": False,
        "reset_enabled": reset_token is not None,
        "reset_token": reset_token,
        "lock": threading.RLock(),
    }

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if target.exists():
            _state["index"] = Index.open(target)
        try:
            yield
        finally:
            index = _state.get("index")
            if index is not None:
                index.close()
                _state["index"] = None

    app = FastAPI(
        title="OneFind local demo",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    if allowed_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError):
        details = "; ".join(str(error.get("msg", "invalid value")) for error in exc.errors())
        return _error(422, "validation_error", details or "The request is invalid.")

    @app.get("/", response_class=FileResponse)
    def root() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html", media_type="text/html")

    @app.get("/assets/{filename}")
    def asset(filename: str):
        media_types = {
            "styles.css": "text/css",
            "app.js": "text/javascript",
            "favicon.svg": "image/svg+xml",
        }
        if filename not in media_types:
            return Response(status_code=404)
        return FileResponse(_STATIC_DIR / filename, media_type=media_types[filename])

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "service": "onefind"}

    @app.get("/api/stats")
    def stats() -> dict:
        lock: threading.RLock = _state["lock"]  # type: ignore[assignment]
        with lock:
            index = _state.get("index")
            if index is None:
                return {
                    "ready": False,
                    "documents": 0,
                    "vectors": 0,
                    "db_bytes": 0,
                    "model": None,
                    "model_dim": 0,
                    "health": None,
                    "modes": [],
                    "precisions": [],
                }
            snapshot = index.stats()
            documents = snapshot["documents"]
            vectors = snapshot.get("vectors", 0)
            modes = ["lexical"] if documents else []
            if documents and vectors and snapshot.get("model"):
                modes.extend(["semantic", "hybrid"])
            precisions = ["float"] if vectors else []
            if snapshot.get("int8_calibrated"):
                precisions.append("int8")
            if vectors:
                precisions.append("binary")
            return {
                "ready": documents > 0,
                "documents": documents,
                "vectors": vectors,
                "db_bytes": snapshot.get("db_bytes", 0),
                "model": snapshot.get("model"),
                "model_dim": snapshot.get("model_dim", 0),
                "health": snapshot.get("health"),
                "modes": modes,
                "precisions": precisions,
            }

    @app.post("/api/search")
    def search(payload: SearchBody = Body(...)):
        request_started = time.perf_counter()
        lock: threading.RLock = _state["lock"]  # type: ignore[assignment]
        with lock:
            index = _state.get("index")
            if index is None:
                return _error(
                    503,
                    "no_index",
                    f"No index is loaded at the configured path. Build one with 'onefind index'.",
                    retryable=True,
                )

            model_load_ms = 0.0
            try:
                if payload.mode != "lexical":
                    model_name = index.get_meta("model_name")
                    if not model_name or index.count("vectors") == 0:
                        return _error(
                            400,
                            "no_embeddings",
                            "This index has no embeddings. Rebuild it with --embed.",
                        )
                    if not _state["embedder_loaded"]:
                        model_started = time.perf_counter()
                        from .embed import SentenceEmbedder

                        index.attach_embedder(SentenceEmbedder(model_name))
                        _state["embedder_loaded"] = True
                        model_load_ms = (time.perf_counter() - model_started) * 1000

                from .search import search as run_search

                hits = run_search(
                    index,
                    payload.query,
                    mode=payload.mode,
                    k=payload.k,
                    precision=payload.precision,
                )
            except OneFindError as exc:
                return _error(400, "search_error", str(exc))
            except (ImportError, RuntimeError) as exc:
                return _error(
                    503,
                    "model_unavailable",
                    f"The local embedding model could not be loaded: {exc}",
                    retryable=True,
                )
            except Exception:
                return _error(
                    500,
                    "internal_error",
                    "The local search service could not complete the request.",
                    retryable=True,
                )

            documents = index.get_documents([hit.doc_id for hit in hits])
            results = []
            for rank, hit in enumerate(hits, start=1):
                document = documents.get(hit.doc_id, {})
                if hit.snippet:
                    snippet_segments = _segments_from_fts(hit.snippet)
                else:
                    snippet_segments = _segments_with_terms(
                        _plain_excerpt(str(document.get("body") or ""), payload.query),
                        payload.query,
                    )
                score = float(hit.score)
                if not math.isfinite(score):
                    return _error(500, "invalid_score", "A non-finite search score was produced.")
                results.append(
                    {
                        "rank": rank,
                        "doc_id": hit.doc_id,
                        "title": hit.title or str(document.get("title") or ""),
                        "source": hit.source or document.get("source"),
                        "snippet": {"segments": snippet_segments},
                        "score": {
                            "value": score,
                            "metric": _score_metric(
                                payload.mode,
                                None if payload.mode == "lexical" else payload.precision,
                            ),
                        },
                    }
                )

            took_ms = (time.perf_counter() - request_started) * 1000
            return {
                "results": results,
                "meta": {
                    "count": len(results),
                    "mode": payload.mode,
                    "precision": None if payload.mode == "lexical" else payload.precision,
                    "k": payload.k,
                    "took_ms": round(took_ms, 1),
                    "model_load_ms": round(model_load_ms, 1),
                },
            }

    @app.post("/api/reset")
    def reset(payload: ResetBody = Body(...)):
        lock: threading.RLock = _state["lock"]  # type: ignore[assignment]
        with lock:
            if not _state["reset_enabled"]:
                return _error(
                    403,
                    "reset_disabled",
                    "Reset is disabled. Start the server with an explicit --reset-token to enable it.",
                )
            expected = str(_state["reset_token"])
            if not secrets.compare_digest(payload.confirm_token, expected):
                return _error(409, "wrong_token", "The reset token is incorrect.")

            old_index = _state.get("index")
            if old_index is not None:
                old_index.close()
            target.unlink(missing_ok=True)
            Path(str(target) + "-wal").unlink(missing_ok=True)
            Path(str(target) + "-shm").unlink(missing_ok=True)
            _state["index"] = Index.open(target)
            _state["embedder_loaded"] = False
            return {"reset": True}

    return app
