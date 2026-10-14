"""Secure localhost web application for OneFind.

The frontend is packaged under ``onefind/static`` and uses only local HTML,
CSS, and JavaScript. It has no CDN, font, analytics, or cloud dependency.

The HTTP API is specified in docs/11-implementation-contract.md section 6.
One process serves one *active* collection at a time (a named entry of the
:class:`~onefind.library.Library`, or an ad-hoc database passed on the
command line). All access to the server's own :class:`~onefind.store.Index`
connection is serialized by one lock; background indexing jobs open their
own connections (WAL lets searches keep running while a folder is indexed).
"""

from __future__ import annotations

import datetime
import json
import logging
import math
import os
import queue
import re
import secrets
import sqlite3
import stat as stat_module
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .errors import (
    CorpusNotFoundError,
    DataError,
    EmptyQueryError,
    EnvError,
    OneFindError,
    UsageError,
)
from .ingest import SUPPORTED_FILES, SUPPORTED_SUFFIXES, normalize_excludes, scan_changes
from .jobs import EmbedderCache, JobManager, JobNotFoundError
from .library import AlreadyRegisteredError, Library, NameTakenError, slugify, validate_name
from .search import PRECISIONS, SEARCH_MODES, SearchFilters, search_detailed
from .snippets import (
    FTS_END,
    FTS_START,
    build_snippet,
    collapse_whitespace,
    segments_from_fts,
    segments_with_terms,
)
from .store import Index, utcnow_iso
from .watcher import AUTO_SYNC_LABEL, AutoSync, get_preference, set_preference

_STATIC_DIR = Path(__file__).with_name("static")
# Legacy aliases: snippet logic now lives in onefind.snippets.
_FTS_START = FTS_START
_FTS_END = FTS_END
_collapse_whitespace = collapse_whitespace
_segments_from_fts = segments_from_fts
_segments_with_terms = segments_with_terms

logger = logging.getLogger("onefind.serve")

_ASSET_NAME = re.compile(r"[a-z0-9][a-z0-9-]*\.(js|css|svg)")
_ASSET_MEDIA_TYPES = {
    ".js": "text/javascript",
    ".css": "text/css",
    ".svg": "image/svg+xml",
}
# Endpoints that predate the CSRF header (or must stay reachable by simple
# clients); every other mutating /api/ endpoint requires X-OneFind-Request: 1.
_CSRF_EXEMPT = frozenset({"/api/search", "/api/search/stream", "/api/reset"})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# GET endpoints that read the local disk also require the CSRF header: a custom
# header forces a CORS preflight, so an old browser without Sec-Fetch-Site
# still cannot list folders for another page.
_CSRF_REQUIRED_GETS = frozenset({"/api/fs/list"})
_CSRF_HEADER = b"x-onefind-request"
_FETCH_SITE = b"sec-fetch-site"
_FOREIGN_SITES = frozenset({b"cross-site", b"same-site"})

SMALL_COLLECTION = 5_000
MEDIUM_COLLECTION = 50_000
MAX_DOCUMENT_CHARS = 200_000
MAX_FS_ENTRIES = 500
MAX_REMOVE_IDS = 1_000
_DB_SUFFIXES = (".db", ".sqlite", ".sqlite3")
_LOOPBACK_NAMES = frozenset({"localhost", "testclient"})

_SECURITY_HEADERS = [
    (
        b"content-security-policy",
        (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        ).encode("latin-1"),
    ),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
]

try:
    from fastapi import Body, FastAPI, Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
    from pydantic import BaseModel, Field, field_validator
    from starlette.middleware.trustedhost import TrustedHostMiddleware

    SERVE_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without [serve]
    Body = FastAPI = Request = BaseModel = Field = field_validator = None  # type: ignore[assignment]
    RequestValidationError = None  # type: ignore[assignment]
    FileResponse = JSONResponse = Response = StreamingResponse = None  # type: ignore[assignment]
    TrustedHostMiddleware = None  # type: ignore[assignment]
    SERVE_AVAILABLE = False


Opener = Callable[[Path, bool], None]


class ApiProblem(Exception):
    """An error that maps directly onto the JSON error envelope."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        action: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable
        self.action = action

    def envelope(self) -> dict:
        error: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.action:
            error["action"] = self.action
        return {"error": error}


if SERVE_AVAILABLE:

    def _check_mode(value: str) -> str:
        if value not in SEARCH_MODES:
            raise ValueError("mode must be lexical, semantic, or hybrid")
        return value

    def _check_precision(value: str | None) -> str | None:
        if value is not None and value not in PRECISIONS:
            raise ValueError("precision must be float, int8, or binary")
        return value

    class SearchBody(BaseModel):  # type: ignore[misc, valid-type]
        query: str = Field(min_length=1, max_length=512)
        mode: str = Field(default="hybrid")
        precision: str = Field(default="float")
        k: int = Field(default=10, ge=1, le=50)
        filters: dict | None = None
        explain: bool = True
        record: bool = True

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
            return _check_mode(value)

        @field_validator("precision")
        @classmethod
        def _precision_ok(cls, value: str) -> str:
            if value not in PRECISIONS:
                raise ValueError("precision must be float, int8, or binary")
            return value

    class ResetBody(BaseModel):  # type: ignore[misc, valid-type]
        confirm_token: str = Field(min_length=16, max_length=256)

    class CollectionCreateBody(BaseModel):  # type: ignore[misc, valid-type]
        name: str = Field(min_length=1, max_length=200)
        db_path: str | None = Field(default=None, max_length=4096)

    class CollectionOpenBody(BaseModel):  # type: ignore[misc, valid-type]
        db_path: str = Field(min_length=1, max_length=4096)
        name: str | None = Field(default=None, max_length=200)

    class CollectionNameBody(BaseModel):  # type: ignore[misc, valid-type]
        name: str = Field(min_length=1, max_length=200)

    class CollectionRenameBody(BaseModel):  # type: ignore[misc, valid-type]
        name: str = Field(min_length=1, max_length=200)
        new_name: str = Field(min_length=1, max_length=200)

    class CollectionRemoveBody(BaseModel):  # type: ignore[misc, valid-type]
        name: str = Field(min_length=1, max_length=200)
        delete_file: bool = False
        confirm_name: str | None = Field(default=None, max_length=200)

    class IndexBody(BaseModel):  # type: ignore[misc, valid-type]
        path: str = Field(min_length=1, max_length=4096)
        exclude: list[str] | None = Field(default=None, max_length=200)
        embed: bool | None = None
        prune: bool = True
        full: bool = False
        # "auto": a folder registered by indexing single files is refreshed
        # file by file; "folder": always index the whole folder.
        scope: str = Field(default="auto")
        confirm_network: bool = False
        # The wizard's "Keep this collection up to date automatically" box;
        # null leaves the collection's saved auto-sync preference alone.
        auto_sync: bool | None = None
        # The collection the request was made for (the UI sends it): a
        # queued update must never land in a collection switched to since.
        collection: str | None = Field(default=None, max_length=200)

        @field_validator("scope")
        @classmethod
        def _scope_ok(cls, value: str) -> str:
            if value not in {"auto", "folder"}:
                raise ValueError("scope must be 'auto' or 'folder'")
            return value

    class RebuildVectorsBody(BaseModel):  # type: ignore[misc, valid-type]
        # "keep": the current passage setting; "default": turn passage search
        # on (long notes are split); "off": one vector per note again.
        chunking: str = Field(default="keep")

        @field_validator("chunking")
        @classmethod
        def _chunking_ok(cls, value: str) -> str:
            value = str(value).strip().lower()
            if value not in {"keep", "default", "off"}:
                raise ValueError("chunking must be 'keep', 'default' or 'off'")
            return value

    class AutoSyncBody(BaseModel):  # type: ignore[misc, valid-type]
        enabled: bool

    class RootRemoveBody(BaseModel):  # type: ignore[misc, valid-type]
        root_id: int | None = None
        root: str | None = Field(default=None, max_length=4096)
        delete_documents: bool = True

    class RootRelocateBody(BaseModel):  # type: ignore[misc, valid-type]
        root_id: int
        path: str = Field(min_length=1, max_length=4096)

    class OpenDocumentBody(BaseModel):  # type: ignore[misc, valid-type]
        doc_id: str = Field(min_length=1, max_length=4096)
        reveal: bool = False
        confirm_network: bool = False

    class RemoveDocumentsBody(BaseModel):  # type: ignore[misc, valid-type]
        doc_ids: list[str] = Field(min_length=1, max_length=MAX_REMOVE_IDS)

    class HistoryBody(BaseModel):  # type: ignore[misc, valid-type]
        query: str = Field(min_length=1, max_length=4000)
        mode: str = Field(default="hybrid")
        precision: str | None = None
        filters: dict | None = None
        result_count: int = Field(default=0, ge=0, le=1_000_000)

        @field_validator("mode")
        @classmethod
        def _mode_ok(cls, value: str) -> str:
            return _check_mode(value)

        @field_validator("precision")
        @classmethod
        def _precision_ok(cls, value: str | None) -> str | None:
            return _check_precision(value)

    class SavedBody(BaseModel):  # type: ignore[misc, valid-type]
        name: str = Field(min_length=1, max_length=200)
        query: str = Field(min_length=1, max_length=4000)
        mode: str = Field(default="hybrid")
        precision: str | None = None
        filters: dict | None = None

        @field_validator("mode")
        @classmethod
        def _mode_ok(cls, value: str) -> str:
            return _check_mode(value)

        @field_validator("precision")
        @classmethod
        def _precision_ok(cls, value: str | None) -> str | None:
            return _check_precision(value)

    class BookmarkBody(BaseModel):  # type: ignore[misc, valid-type]
        doc_id: str = Field(min_length=1, max_length=4096)
        note: str | None = Field(default=None, max_length=4000)

    class RestoreBody(BaseModel):  # type: ignore[misc, valid-type]
        backup_path: str = Field(min_length=1, max_length=4096)
        mode: str = Field(default="new")
        name: str | None = Field(default=None, max_length=200)
        confirm_name: str | None = Field(default=None, max_length=200)

        @field_validator("mode")
        @classmethod
        def _mode_ok(cls, value: str) -> str:
            if value not in {"new", "replace"}:
                raise ValueError("mode must be 'new' or 'replace'")
            return value

    class ClearBody(BaseModel):  # type: ignore[misc, valid-type]
        confirm_name: str = Field(min_length=1, max_length=200)

else:  # pragma: no cover
    SearchBody = ResetBody = None  # type: ignore[assignment]


# -- small helpers -----------------------------------------------------------------


def _error(
    status_code: int,
    code: str,
    message: str,
    retryable: bool = False,
    action: str | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ApiProblem(status_code, code, message, retryable=retryable, action=action).envelope(),
    )


def _score_metric(mode: str, precision: str | None) -> str:
    if mode == "lexical":
        return "BM25 score"
    if mode == "semantic":
        return "Hamming distance" if precision == "binary" else "cosine similarity"
    return "RRF fusion score"


def _iso_from_epoch(value: Any) -> str | None:
    if value is None:
        return None
    try:
        moment = datetime.datetime.fromtimestamp(float(value), datetime.timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return moment.isoformat(timespec="seconds")


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    word = singular if count == 1 else (plural or singular + "s")
    return f"{count:,} {word}"


def _same_name(left: str | None, right: str | None) -> bool:
    """Exact comparison of collection names after trimming (for confirmations)."""
    return left is not None and right is not None and left.strip() == right.strip()


def _path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _is_within(target: Path, base: Path) -> bool:
    target_key, base_key = _path_key(target), _path_key(base)
    try:
        return os.path.commonpath([target_key, base_key]) == base_key
    except ValueError:  # different drives on Windows
        return False


def _is_loopback(host: str | None) -> bool:
    """True for loopback peers.

    ``testclient`` is the synthetic peer name of Starlette's in-process test
    client; a real socket peer is always reported as an IP address.
    """
    if not host:
        return False
    if host in _LOOPBACK_NAMES:
        return True
    import ipaddress

    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def is_network_path(path: str | Path | None) -> bool:
    r"""True for UNC (``\\host\share``) and device (``\\?\``, ``\\.\``) paths.

    Touching such a path makes Windows contact another machine (SMB), which
    can hand over the user's credentials, so OneFind never does it without
    an explicit user action.
    """
    if path is None:
        return False
    text = str(path).strip().replace("/", "\\")
    return text.startswith("\\\\")


def _network_host(path: str | Path) -> str:
    parts = [part for part in str(path).replace("/", "\\").split("\\") if part]
    if parts and parts[0] in {"?", "."} and len(parts) > 2 and parts[1].upper() == "UNC":
        return parts[2]
    return parts[0] if parts else str(path)


def _confirm_network(path: str | Path, confirmed: bool, what: str) -> None:
    if is_network_path(path) and not confirmed:
        raise ApiProblem(
            409,
            "confirmation_required",
            f"{what} is on a network location ({_network_host(path)}). OneFind only "
            "contacts other computers when you confirm. Continue anyway?",
            action="confirm_network",
        )


def _resolve_user_path(raw: str) -> Path:
    text = str(raw).strip()
    if not text or "\x00" in text:
        raise ApiProblem(400, "invalid_path", "Choose a folder or file path.")
    try:
        return Path(text).expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ApiProblem(
            404, "path_not_found", f"The path {text} could not be found.", action="index"
        ) from exc


def default_opener(path: Path, reveal: bool) -> None:
    """Open ``path`` with the OS default app, or show it in the file manager."""
    target = str(path)
    if sys.platform == "win32":
        if reveal:
            # explorer parses its own command line; Windows paths cannot contain '"'.
            subprocess.Popen(f'explorer /select,"{target}"')  # noqa: S603
        else:
            os.startfile(target)  # type: ignore[attr-defined]  # noqa: S606
        return
    if sys.platform == "darwin":
        command = ["open", "-R", target] if reveal else ["open", target]
    else:
        command = ["xdg-open", str(path.parent) if reveal else target]
    subprocess.Popen(  # noqa: S603 - fixed program, absolute path argument
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def _drive_places() -> list[dict]:
    if sys.platform != "win32":
        return [{"label": "Computer", "path": "/"}]
    drives: list[str] = []
    listdrives = getattr(os, "listdrives", None)
    if listdrives is not None:
        try:
            drives = list(listdrives())
        except OSError:
            drives = []
    else:  # pragma: no cover - Python < 3.12
        try:
            import ctypes

            mask = ctypes.windll.kernel32.GetLogicalDrives()  # type: ignore[attr-defined]
            drives = [f"{chr(65 + bit)}:\\" for bit in range(26) if mask & (1 << bit)]
        except Exception:  # noqa: BLE001
            drives = []
    return [{"label": drive.rstrip("\\/"), "path": drive} for drive in drives]


def _is_hidden(entry: os.DirEntry) -> bool:
    if entry.name.startswith("."):
        return True
    if sys.platform == "win32":
        try:
            attributes = entry.stat(follow_symlinks=False).st_file_attributes
        except (OSError, AttributeError):
            return False
        hidden = stat_module.FILE_ATTRIBUTE_HIDDEN | stat_module.FILE_ATTRIBUTE_SYSTEM
        return bool(attributes & hidden)
    return False


def list_directory(raw_path: str | None) -> dict:
    """Names-only listing used by the folder picker (contract section 6)."""
    network = ApiProblem(
        400,
        "invalid_path",
        "Network and device paths are not supported in the folder picker. Type the "
        "path into the index box instead.",
    )
    if raw_path is None or not str(raw_path).strip():
        target = Path.home()
    else:
        # Refuse before touching the file system: resolving or listing a UNC
        # path makes Windows open an SMB session to that host.
        if is_network_path(raw_path):
            raise network
        target = _resolve_user_path(raw_path)
        if is_network_path(target):  # e.g. a mapped drive that resolves to UNC
            raise network
    try:
        exists = target.is_dir()
    except OSError:
        exists = False
    if not exists:
        if target.exists():
            raise ApiProblem(
                404, "path_not_found", f"{target} is a file, not a folder.", action="index"
            )
        raise ApiProblem(
            404,
            "path_not_found",
            f"The folder {target} does not exist. It may have been moved or renamed.",
            action="index",
        )
    dirs: list[str] = []
    files: list[tuple[str, bool]] = []
    try:
        with os.scandir(target) as iterator:
            for entry in iterator:
                if _is_hidden(entry):
                    continue
                try:
                    is_dir = entry.is_dir()
                except OSError:
                    continue
                if is_dir:
                    dirs.append(entry.name)
                else:
                    files.append((entry.name, os.path.splitext(entry.name)[1].lower()))
    except PermissionError as exc:
        raise ApiProblem(
            403,
            "permission_denied",
            f"OneFind is not allowed to open {target}. Pick another folder.",
        ) from exc
    except OSError as exc:
        raise ApiProblem(
            404, "path_not_found", f"The folder {target} could not be read.", retryable=True
        ) from exc
    dirs.sort(key=str.casefold)
    files.sort(key=lambda item: item[0].casefold())
    entries = [
        {"name": name, "path": str(target / name), "kind": "dir", "supported": False}
        for name in dirs
    ]
    # "supported": the file can be indexed on its own (notes and .jsonl
    # datasets); "supported_files" counts what indexing the folder reads
    # (datasets are only read when picked one by one).
    entries += [
        {"name": name, "path": str(target / name), "kind": "file", "supported": suffix in SUPPORTED_FILES}
        for name, suffix in files
    ]
    total = len(entries)
    parent = target.parent if target.parent != target else None
    places: list[dict] = []
    home = Path.home()
    for label, candidate in (
        ("Home", home),
        ("Documents", home / "Documents"),
        ("Desktop", home / "Desktop"),
    ):
        try:
            if candidate.is_dir():
                places.append({"label": label, "path": str(candidate)})
        except OSError:
            continue
    places.extend(_drive_places())
    return {
        "path": str(target),
        "parent": str(parent) if parent is not None else None,
        "entries": entries[:MAX_FS_ENTRIES],
        "total": total,
        "truncated": total > MAX_FS_ENTRIES,
        "places": places,
        "supported_files": sum(1 for _name, suffix in files if suffix in SUPPORTED_SUFFIXES),
    }


def _passage_payload(hit: Any) -> dict | None:
    """The passage a meaning match came from, for a note split into several.

    ``{"seq": i, "count": n, "text": str}`` (seq 0 = the beginning), or None
    for keyword-only hits and one-passage notes. The preview uses ``text`` to
    find and mark the passage in the full note.
    """
    seq = getattr(hit, "passage_seq", None)
    count = getattr(hit, "passage_count", None)
    text = getattr(hit, "passage", None)
    if seq is None or not count or int(count) < 2 or not text:
        return None
    return {"seq": int(seq), "count": int(count), "text": str(text)}


def _public_job(job: dict | None) -> dict | None:
    """A job dict without the collection database path (for /api/stats)."""
    if job is None:
        return None
    clean = {key: value for key, value in job.items() if key != "db"}
    if isinstance(clean.get("result"), dict):
        clean["result"] = {k: v for k, v in clean["result"].items() if k != "db"}
    return clean


def _job_percent(job: dict) -> int | None:
    total = job.get("total")
    done = job.get("done") or 0
    if job.get("phase") in {"read", "write", "embed"} and total:
        return max(0, min(100, int(100 * int(done) / int(total))))
    return None


def _connect_for_read(path: Path) -> sqlite3.Connection:
    """Open an existing database for reading without leaving -wal/-shm behind.

    A ``mode=ro`` connection to a WAL database creates -wal/-shm but can
    neither checkpoint nor delete them on close. ``mode=rw`` never creates a
    missing file and never writes for a SELECT; as the last connection to
    close it removes them. ``mode=ro`` stays the fallback for read-only files.
    """
    uri = path.resolve().as_uri()
    try:
        return sqlite3.connect(f"{uri}?mode=rw", uri=True)
    except sqlite3.OperationalError:
        return sqlite3.connect(f"{uri}?mode=ro", uri=True)


def _validate_database_file(path: Path) -> None:
    """Sanity check of a OneFind database (only reads from it)."""
    try:
        conn = _connect_for_read(path)
    except (sqlite3.Error, ValueError) as exc:
        raise ApiProblem(
            400, "invalid_backup", "That file could not be opened as a OneFind collection."
        ) from exc
    try:
        try:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise ApiProblem(
                400, "invalid_backup", "That file is not a OneFind collection."
            ) from exc
        try:
            version = int(row[0]) if row else 0
        except (TypeError, ValueError):
            version = 0
        if version not in (2, 3):
            raise ApiProblem(
                400,
                "invalid_backup",
                "That file was made by an unsupported OneFind version and cannot be restored.",
            )
    finally:
        conn.close()


def _copy_database(source: Path, target: Path) -> None:
    """Consistent single-file copy of ``source`` using SQLite's backup API."""
    src = _connect_for_read(source)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()


def _delete_family(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        candidate.unlink(missing_ok=True)


def _problem_from_exception(exc: BaseException) -> ApiProblem:
    """Translate an exception into a user-facing error (no raw internals)."""
    if isinstance(exc, ApiProblem):
        return exc
    if isinstance(exc, NameTakenError):
        text = str(exc)
        return ApiProblem(409, "name_taken", text[:1].upper() + text[1:] + ". Choose another name.")
    if isinstance(exc, AlreadyRegisteredError):
        return ApiProblem(
            409,
            "already_registered",
            f"That file is already the collection “{exc.existing}”.",
            action="open_collection",
        )
    if isinstance(exc, JobNotFoundError):
        return ApiProblem(404, "not_found", "That indexing job is no longer known to OneFind.")
    if isinstance(exc, CorpusNotFoundError):
        return ApiProblem(404, "path_not_found", str(exc), action="index")
    if isinstance(exc, UsageError):
        return ApiProblem(400, "invalid_request", str(exc))
    if isinstance(exc, DataError):
        text = str(exc)
        lowered = text.lower()
        if "not found" in lowered or lowered.startswith("unknown collection"):
            return ApiProblem(404, "not_found", text[:1].upper() + text[1:])
        return ApiProblem(400, "data_error", text, action="repair")
    if isinstance(exc, EnvError):
        return ApiProblem(503, "environment", str(exc), retryable=True, action="switch_mode")
    if isinstance(exc, OneFindError):
        return ApiProblem(400, "request_failed", str(exc))
    if isinstance(exc, sqlite3.OperationalError) and any(
        word in str(exc).lower() for word in ("locked", "busy")
    ):
        return ApiProblem(
            409,
            "collection_busy",
            "The collection is busy (another task is writing to it). Try again in a moment.",
            retryable=True,
            action="retry",
        )
    if isinstance(exc, sqlite3.DatabaseError):
        return ApiProblem(
            500,
            "collection_unreadable",
            "The collection file could not be read. Run a check from Manage, or restore a backup.",
            action="repair",
        )
    if isinstance(exc, PermissionError):
        return ApiProblem(
            403, "permission_denied", "OneFind is not allowed to access that file or folder."
        )
    if isinstance(exc, FileNotFoundError):
        return ApiProblem(404, "path_not_found", "That file or folder could not be found.")
    return ApiProblem(
        500,
        "internal_error",
        "The local search service could not complete the request.",
        retryable=True,
        action="retry",
    )


class _PrecomputedQuery:
    """An embedder view whose ``encode_query`` returns a vector computed earlier.

    The server encodes the query outside its lock and hands the search this
    view, so the pipeline never waits for the shared model under the lock.
    """

    def __init__(self, inner: Any, text: str, vector: Any) -> None:
        self._inner = inner
        self._text = text
        self._vector = vector

    def encode_query(self, text: str):
        if str(text).strip() == self._text:
            return self._vector
        return self._inner.encode_query(text)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


# -- runtime state -------------------------------------------------------------------


class _Runtime:
    """Everything one app instance shares: active collection, index, jobs."""

    def __init__(
        self,
        *,
        index_path: Path | None,
        library: Library,
        register: bool,
        embedders: EmbedderCache,
        reset_token: str | None,
        allow_open: bool,
        autosync: bool = True,
        autosync_interval: float | None = None,
    ) -> None:
        self.lock = threading.RLock()
        self.index_path = index_path
        self.library = library
        self.register = register
        self.embedders = embedders
        self.jobs = JobManager(embedders)
        # One watcher at most: it follows the active collection (contract §10).
        self.autosync = AutoSync(self.jobs, interval=autosync_interval)
        self.autosync_allowed = bool(autosync)
        self.reset_token = reset_token
        self.allow_open = allow_open
        self.index: Index | None = None
        # {"name": str, "db": Path, "registered": bool}
        self.active: dict | None = None

    # -- lifecycle -------------------------------------------------------------

    def startup(self) -> None:
        with self.lock:
            if self.index_path is not None:
                self.active = self._adhoc_entry(self.index_path)
            else:
                try:
                    entry = self.library.active()
                except (OneFindError, OSError):
                    logger.warning("could not read the collections registry", exc_info=True)
                    entry = None
                if entry is not None:
                    self.active = {
                        "name": entry["name"],
                        "db": Path(entry["db"]),
                        "registered": True,
                    }
            self._open_active()
            self.follow_autosync()

    def shutdown(self) -> None:
        # Watchers first: they would otherwise start new jobs while jobs stop.
        self.autosync.shutdown(timeout=10.0)
        self.jobs.shutdown(timeout=5.0)
        with self.lock:
            self._close_index()

    def _adhoc_entry(self, target: Path) -> dict:
        name = target.stem or "Collection"
        if target.is_file():
            try:
                with Index.open(target) as probe:
                    name = probe.get_meta("collection_name") or name
            except (OneFindError, sqlite3.Error, OSError):
                pass
        entry = {"name": name, "db": target, "registered": False}
        if not self.register or not target.is_file():
            return entry
        try:
            found = self.library.find_by_db(target)
            if found is None:
                found = self._register_with_free_name(name, target)
            if found is not None:
                return {"name": found["name"], "db": target, "registered": True}
        except (OneFindError, OSError):
            logger.info("serving %s as an unlisted collection", target.name, exc_info=True)
        return entry

    def _register_with_free_name(self, name: str, target: Path) -> dict | None:
        for attempt in range(1, 21):
            candidate = name if attempt == 1 else f"{name} {attempt}"
            try:
                return self.library.register(candidate, target)
            except NameTakenError:
                continue
            except AlreadyRegisteredError as exc:
                return self.library.get(exc.existing)
        return None

    def _open_active(self) -> None:
        """Open the active collection's database when it exists (lock held)."""
        if self.active is None or self.index is not None:
            return
        db = Path(self.active["db"])
        try:
            if not db.is_file():
                return
            self.index = Index.open(db)
        except (OneFindError, sqlite3.Error, OSError):
            logger.warning("could not open collection %s", self.active["name"], exc_info=True)
            self.index = None
            return
        self.follow_autosync()

    def _close_index(self) -> None:
        if self.index is not None:
            try:
                self.index.close()
            except Exception:  # noqa: BLE001 - best effort
                pass
            self.index = None

    def current_index(self) -> Index | None:
        """The open index, lazily opened when its file appeared (lock held)."""
        if self.index is None:
            self._open_active()
        return self.index

    @contextmanager
    def using_index(self) -> Iterator[Index]:
        with self.lock:
            index = self.current_index()
            if index is None:
                raise ApiProblem(
                    503,
                    "no_index",
                    "No index is ready yet. Index a folder to start searching.",
                    retryable=True,
                    action="index",
                )
            yield index

    def active_job(self) -> dict | None:
        if self.active is None:
            return None
        return self.jobs.active_for(self.active["db"])

    def ensure_not_busy(self, db: str | Path | None = None) -> None:
        target = db if db is not None else (self.active["db"] if self.active else None)
        if target is not None:
            # A manual action always wins over auto-sync: its job is cancelled
            # (finished batches stay) and the watcher waits a moment.
            self.autosync.preempt(target)
        if target is not None and self.jobs.active_for(target) is not None:
            raise ApiProblem(
                409,
                "collection_busy",
                "An indexing job is running on this collection. Wait for it to finish or cancel it first.",
                retryable=True,
                action="retry",
            )

    def require_active(self) -> dict:
        if self.active is None:
            raise ApiProblem(
                503,
                "no_index",
                "No collection is open yet. Index a folder or open a collection first.",
                retryable=True,
                action="index",
            )
        return self.active

    def switch_to(self, entry: dict) -> None:
        """Make a registry entry the active collection (lock held by caller)."""
        db = Path(entry["db"])
        new_index: Index | None = None
        if db.is_file():
            try:
                new_index = Index.open(db)
            except (OneFindError, sqlite3.Error, OSError) as exc:
                raise ApiProblem(
                    409,
                    "collection_unreadable",
                    f"The collection “{entry['name']}” could not be opened. Run a check, "
                    "or restore it from a backup.",
                    action="repair",
                ) from exc
        self._close_index()
        self.index = new_index
        self.active = {"name": entry["name"], "db": db, "registered": True}
        self.follow_autosync()

    # -- auto-sync ---------------------------------------------------------------

    def _autosync_target(self) -> Path | None:
        if self.active is None or self.index is None:
            return None
        return Path(self.active["db"])

    def autosync_preference(self) -> bool:
        """The active collection's saved auto-sync preference (lock held)."""
        if self.index is None:
            return False
        try:
            return get_preference(self.index)
        except (OneFindError, sqlite3.Error):
            return False

    def follow_autosync(self) -> None:
        """Watch the active collection when its preference is on; stop the rest."""
        target = self._autosync_target()
        for db in self.autosync.watched():
            if target is None or _path_key(db) != _path_key(target):
                self.autosync.stop(db)
        if target is None:
            return
        if self.autosync_allowed and self.autosync_preference():
            try:
                self.autosync.start(target)
            except (OneFindError, OSError):
                logger.warning("auto-sync could not start", exc_info=True)
        else:
            self.autosync.stop(target)

    def stop_autosync(self, db: str | Path | None = None) -> None:
        """Stop watching ``db`` (default: the active collection) before file surgery."""
        target = db if db is not None else (self.active["db"] if self.active else None)
        if target is not None:
            self.autosync.stop(target)

    def set_autosync(self, enabled: bool) -> dict:
        """Save the preference in the active collection and start/stop watching."""
        with self.using_index() as index:
            set_preference(index, bool(enabled))
            self.follow_autosync()
            return self.autosync_payload()

    def autosync_payload(self) -> dict:
        """Auto-sync status of the active collection (never contains the db path)."""
        with self.lock:
            target = Path(self.active["db"]) if self.active is not None else None
            enabled = self.autosync_preference()
            status = (
                self.autosync.status(target) if target is not None else self.autosync.blank_status()
            )
            running = target is not None and self.autosync.running(target)
            available = target is not None and self.index is not None
        status["enabled"] = enabled
        status["available"] = available
        if enabled and not running:
            status["state"] = "paused"
            status["message"] = (
                "Auto-sync is turned off for this server (--no-auto-sync)."
                if not self.autosync_allowed
                else status.get("message") or "Auto-sync is not running."
            )
        if not enabled:
            status["state"] = "idle"
            status["message"] = ""
            status["pending"] = {"new": 0, "changed": 0, "deleted": 0}
            status["progress"] = None
        if target is not None and status.get("message"):
            for spelling in {str(target), str(target.absolute())}:
                status["message"] = status["message"].replace(spelling, "the collection file")
        return status

    def ensure_embedder(self, model_name: str) -> float:
        """Load the shared model outside the index lock; returns load time (ms)."""
        if self.embedders.loaded(model_name):
            return 0.0
        started = time.perf_counter()
        try:
            self.embedders.get(model_name)
        except Exception as exc:  # noqa: BLE001 - any load failure = unavailable
            logger.warning("embedding model %s could not be loaded", model_name, exc_info=True)
            raise ApiProblem(
                503,
                "model_unavailable",
                "The meaning-search model could not be loaded. Search by keyword for now, "
                'or install the model extra (pip install -e ".[model]").',
                retryable=True,
                action="switch_mode",
            ) from exc
        return (time.perf_counter() - started) * 1000.0

    def bind_embedder(self, index: Index) -> None:
        """Give the server's connection the cached model without writing to the db."""
        if index.embedder is not None:
            return
        model_name = index.get_meta("model_name")
        if not model_name:
            return
        embedder = self.embedders.peek(model_name)
        if embedder is None:
            return
        stored_dim = index.get_meta("model_dim")
        if stored_dim and int(stored_dim) != int(getattr(embedder, "dimension", 0)):
            raise ApiProblem(
                409,
                "model_mismatch",
                "This collection was built with a different meaning model. Reindex it to search by meaning.",
                action="reindex",
            )
        index.embedder = embedder

    # -- payloads --------------------------------------------------------------

    def stats_payload(self) -> dict:
        with self.lock:
            active = self.active
            job = self.active_job()
            if job is not None and job.get("label") == AUTO_SYNC_LABEL:
                # Auto-sync is background upkeep: it shows in "auto_sync", and
                # the collection stays "ready" instead of "indexing".
                job = None
            index = self.current_index()
            payload: dict[str, Any] = {
                "ready": False,
                "documents": 0,
                "vectors": 0,
                "db_bytes": 0,
                "model": None,
                "model_dim": 0,
                "health": None,
                "modes": [],
                "precisions": [],
                "collection": {"name": active["name"]} if active else None,
                "status": "no_index",
                "status_detail": "",
                "model_loaded": False,
                "last_indexed_at": None,
                "roots": 0,
                "size_class": "small",
                "size_note": None,
                "job": _public_job(job),
                "vectors_missing": 0,
                "vectors_unavailable": False,
                # Passage chunking (docs/11 section 9). long_notes = notes longer
                # than one passage (stats()["long_notes"]); notes_over_model_window
                # = notes with text the model cannot read (a passage, or a whole
                # note with chunking off, longer than its window).
                "long_notes": 0,
                "notes_over_model_window": 0,
                "passages": 0,
                "chunking": None,
                "vectors_need_rebuild": None,
            }
            healthy = True
            if index is not None:
                try:
                    snapshot = index.stats()
                except (OneFindError, sqlite3.Error):
                    logger.warning("stats failed", exc_info=True)
                    snapshot = None
                if snapshot is None:
                    healthy = False
                else:
                    documents = int(snapshot["documents"])
                    vectors = int(snapshot.get("vectors", 0) or 0)
                    modes = ["lexical"] if documents else []
                    if documents and vectors and snapshot.get("model"):
                        modes.extend(["semantic", "hybrid"])
                    precisions = ["float"] if vectors else []
                    if snapshot.get("int8_calibrated"):
                        precisions.append("int8")
                    if vectors:
                        precisions.append("binary")
                    health = snapshot.get("health") or {}
                    model = snapshot.get("model")
                    payload.update(
                        ready=documents > 0,
                        documents=documents,
                        vectors=vectors,
                        db_bytes=snapshot.get("db_bytes", 0),
                        model=model,
                        model_dim=snapshot.get("model_dim", 0),
                        health=snapshot.get("health"),
                        modes=modes,
                        precisions=precisions,
                        model_loaded=bool(model) and self.embedders.loaded(model),
                        last_indexed_at=snapshot.get("last_indexed_at"),
                        roots=snapshot.get("roots", 0),
                        vectors_missing=int(health.get("missing_vectors") or 0),
                        vectors_unavailable=bool(snapshot.get("vectors_unavailable")),
                        long_notes=int(snapshot.get("long_notes") or 0),
                        notes_over_model_window=int(health.get("long_documents") or 0),
                        passages=int(snapshot.get("passages") or 0),
                        chunking=snapshot.get("chunking"),
                        vectors_need_rebuild=snapshot.get("vectors_need_rebuild"),
                    )
                    # Missing vectors alone limit meaning search but are not
                    # damage; count mismatches and orphan vectors are.
                    healthy = (
                        health.get("documents") == health.get("chunks") == health.get("fts")
                        and not health.get("orphan_vectors")
                    )
            documents = payload["documents"]
            payload["size_class"] = (
                "small"
                if documents < SMALL_COLLECTION
                else "medium"
                if documents < MEDIUM_COLLECTION
                else "large"
            )
            payload["size_note"] = self._size_note(documents)
            status, detail = self._status(payload, active, index, job, healthy)
            payload["status"] = status
            payload["status_detail"] = detail
            payload["auto_sync"] = self.autosync_payload()
            return payload

    @staticmethod
    def _size_note(documents: int) -> str | None:
        if documents < SMALL_COLLECTION:
            return None
        if documents < MEDIUM_COLLECTION:
            return (
                f"{documents:,} notes is a medium-sized collection. Search stays quick; "
                "full rebuilds and building vectors take a few minutes."
            )
        return (
            f"{documents:,} notes is above OneFind's comfort zone (about 50,000). Keyword "
            "search stays fast, but meaning search compares every note — consider "
            "splitting this into smaller collections."
        )

    @staticmethod
    def _status(payload: dict, active, index, job, healthy: bool) -> tuple[str, str]:
        if job is not None:
            percent = _job_percent(job)
            doing = "Rebuilding vectors" if job.get("kind") == "rebuild_vectors" else "Indexing"
            return "indexing", f"{doing}… {percent}%" if percent is not None else f"{doing}…"
        if active is None:
            return "no_index", "No collection yet. Index a folder to start searching your notes."
        if index is None:
            if not Path(active["db"]).exists():
                if active["registered"]:
                    return (
                        "no_index",
                        f"The file for “{active['name']}” is missing. Open it from its new "
                        "location, or index a folder to rebuild it.",
                    )
                return "no_index", "No index is ready yet. Index a folder to start searching."
            return (
                "needs_attention",
                "The collection file could not be opened. Run a check, or restore a backup.",
            )
        if not healthy:
            return (
                "needs_attention",
                "The index is out of step with your notes. Open Manage and run Repair.",
            )
        documents = payload["documents"]
        if documents == 0:
            return "empty", "This collection is empty. Index a folder to add notes."
        ready = f"{_plural(documents, 'note')} ready."
        if payload.get("vectors_unavailable"):
            return (
                "ready",
                f"{ready} Keyword search available; meaning search needs the sqlite-vec "
                "extension — run 'onefind check'.",
            )
        missing = payload["vectors_missing"]
        if payload["vectors"] and missing:
            return (
                "ready",
                f"{ready} Vectors missing for {_plural(missing, 'note')} — meaning search is limited.",
            )
        rebuild = payload.get("vectors_need_rebuild") or {}
        if payload["vectors"] and rebuild.get("needed"):
            pending = int(rebuild.get("documents") or 0)
            return (
                "ready",
                f"{ready} Vectors need a rebuild for {_plural(pending, 'note')} — open Manage to "
                "rebuild them.",
            )
        if payload["vectors"]:
            return "ready", f"{ready} Keyword and meaning search available."
        return "ready", f"{ready} Keyword search available; build vectors to search by meaning."

    def collections_payload(self) -> dict:
        entries = self.library.list()
        with self.lock:
            active = self.active
            active_key = _path_key(active["db"]) if active else None
            result = []
            for entry in entries:
                is_active = active_key is not None and _path_key(entry["db"]) == active_key
                item = dict(entry)
                item["active"] = is_active
                item.update(self._counts(Path(entry["db"]), is_active))
                result.append(item)
            if active is not None and not active["registered"]:
                item = {
                    "name": active["name"],
                    "db": str(active["db"]),
                    "created_at": None,
                    "exists": Path(active["db"]).is_file(),
                    "active": True,
                    "registered": False,
                }
                item.update(self._counts(Path(active["db"]), True))
                result.append(item)
        return {
            "active": active["name"] if active else None,
            "collections": result,
            "home": str(self.library.home),
        }

    def _counts(self, db: Path, is_active: bool) -> dict:
        counts = {"documents": 0, "vectors": 0, "db_bytes": 0}
        try:
            if not db.is_file():
                return counts
            counts["db_bytes"] = db.stat().st_size
        except OSError:
            return counts
        try:
            if is_active and self.current_index() is not None:
                index = self.current_index()
                assert index is not None
                counts["documents"] = index.count("documents")
                counts["vectors"] = (
                    index.count("vectors") if index.has_vectors() and index.vec_ready else 0
                )
                return counts
            with Index.open(db) as other:
                counts["documents"] = other.count("documents")
                if other.has_vectors() and other.vec_ready:
                    counts["vectors"] = other.count("vectors")
        except (OneFindError, sqlite3.Error, OSError):
            pass
        return counts

    def entry_payload(self, entry: dict) -> dict:
        with self.lock:
            item = dict(entry)
            is_active = self.active is not None and _path_key(self.active["db"]) == _path_key(
                entry["db"]
            )
            item["active"] = is_active
            item.update(self._counts(Path(entry["db"]), is_active))
            return item

    def find_entry(self, name: str) -> dict:
        try:
            return self.library.get(name)
        except DataError as exc:
            if self.active is not None and not self.active["registered"] and _same_name(
                self.active["name"].casefold(), name.casefold()
            ):
                raise ApiProblem(
                    400,
                    "not_registered",
                    "This collection was opened directly from a file and is not in your "
                    "collections list.",
                ) from exc
            raise ApiProblem(404, "not_found", f"There is no collection named “{name}”.") from exc

    # -- search ----------------------------------------------------------------

    def run_search(self, payload: Any, on_stage=None) -> dict:
        request_started = time.perf_counter()
        filters: SearchFilters | None = None
        if payload.filters:
            try:
                filters = SearchFilters.from_dict(payload.filters)
            except UsageError as exc:
                raise ApiProblem(400, "invalid_filters", str(exc)) from exc
            if filters.is_empty():
                filters = None

        model_load_ms = 0.0
        if payload.mode != "lexical":
            with self.using_index() as index:
                model_name = index.get_meta("model_name")
                if model_name and not index.vec_ready:
                    raise ApiProblem(
                        503,
                        "vectors_unavailable",
                        "Vector storage (sqlite-vec) is not available, so meaning search "
                        "cannot run. Search by keyword instead.",
                        action="switch_mode",
                    )
                vectors = index.count("vectors") if model_name else 0
                needs_model = index.embedder is None
            if not model_name or vectors == 0:
                raise ApiProblem(
                    400,
                    "no_embeddings",
                    "This collection has no meaning-search vectors yet. Build vectors from "
                    "Manage, or search by keyword.",
                    action="embed",
                )
            if needs_model:
                model_load_ms = self.ensure_embedder(model_name)

        # Encode the query *before* taking the runtime lock: the shared model
        # may be busy with an indexing job's batch for seconds, and every other
        # request (stats polling, keyword search, bookmarks) needs that lock.
        query_vector = None
        embed_ms: float | None = None
        cleaned_query = str(payload.query).strip()
        stage_callback = on_stage
        embedder = (
            self.embedders.peek(model_name)
            if payload.mode != "lexical" and cleaned_query
            else None
        )
        if embedder is not None:
            started = time.perf_counter()
            try:
                query_vector = embedder.encode_query(cleaned_query)
            except (ImportError, RuntimeError) as exc:
                logger.warning("search failed", exc_info=True)
                raise ApiProblem(
                    503,
                    "model_unavailable",
                    "The local embedding model could not run this search. Try keyword search.",
                    retryable=True,
                    action="switch_mode",
                ) from exc
            embed_ms = (time.perf_counter() - started) * 1000.0
            if on_stage is not None:

                def stage_callback(name: str, status: str, ms: float) -> None:
                    # The pipeline's own "embed" stage is instant now; report
                    # the real encoding time instead.
                    if name == "embed" and status == "done" and embed_ms is not None:
                        ms = embed_ms
                    on_stage(name, status, ms)

        with self.using_index() as index:
            original_embedder = index.embedder
            if payload.mode != "lexical":
                self.bind_embedder(index)
                original_embedder = index.embedder
                if (
                    index.embedder is None or index.get_meta("model_name") != model_name
                ):  # the collection changed while loading or encoding
                    raise ApiProblem(
                        409,
                        "collection_changed",
                        "The collection changed while the search was starting. Try again.",
                        retryable=True,
                        action="retry",
                    )
                if query_vector is not None:
                    index.embedder = _PrecomputedQuery(
                        index.embedder, cleaned_query, query_vector
                    )
            try:
                outcome = search_detailed(
                    index,
                    payload.query,
                    mode=payload.mode,
                    k=payload.k,
                    precision=payload.precision,
                    filters=filters,
                    explain=bool(payload.explain),
                    on_stage=stage_callback,
                    # Natural-language questions rarely share every word with
                    # a note: "auto" tops up all-words hits with partial ones.
                    lexical_match="auto" if payload.mode in ("lexical", "hybrid") else "all",
                )
                if embed_ms is not None:
                    for record in outcome.stages:
                        if isinstance(record, dict) and record.get("name") == "embed":
                            record["ms"] = round(embed_ms, 3)
            except (EmptyQueryError, UsageError) as exc:
                raise ApiProblem(400, "search_error", str(exc)) from exc
            except EnvError as exc:
                raise ApiProblem(
                    503, "vectors_unavailable", str(exc), action="switch_mode"
                ) from exc
            except DataError as exc:
                if "changed during scanning" not in str(exc):
                    raise ApiProblem(400, "search_error", str(exc)) from exc
                # Safety net: the vector scan saw a write through this connection.
                raise ApiProblem(
                    409,
                    "index_changed",
                    "The index changed while searching. Try again.",
                    retryable=True,
                    action="retry",
                ) from exc
            except OneFindError as exc:
                raise ApiProblem(400, "search_error", str(exc)) from exc
            except (ImportError, RuntimeError) as exc:
                logger.warning("search failed", exc_info=True)
                raise ApiProblem(
                    503,
                    "model_unavailable",
                    "The local embedding model could not run this search. Try keyword search.",
                    retryable=True,
                    action="switch_mode",
                ) from exc
            finally:
                if payload.mode != "lexical":
                    index.embedder = original_embedder

            build_started = time.perf_counter()
            if on_stage is not None:
                on_stage("build", "start", 0.0)
            results = self._build_results(index, outcome, payload)
            build_ms = (time.perf_counter() - build_started) * 1000.0
            if on_stage is not None:
                on_stage("build", "done", build_ms)

            precision = None if payload.mode == "lexical" else payload.precision
            filter_dict = filters.to_dict() if filters is not None else {}
            if payload.record:
                try:
                    # History counts strong results only: a search where every
                    # hit is weak is recorded as 0 ("no strong matches").
                    index.record_search(
                        payload.query,
                        payload.mode,
                        precision,
                        filter_dict or None,
                        sum(1 for item in results if item.get("confidence") != "weak"),
                    )
                except Exception:  # noqa: BLE001 - history must never fail a search
                    logger.warning("could not record search history", exc_info=True)

        stages = [*outcome.stages, {"name": "build", "ms": round(build_ms, 3)}]
        took_ms = (time.perf_counter() - request_started) * 1000
        return {
            "query": payload.query,
            "results": results,
            "meta": {
                "count": len(results),
                "mode": payload.mode,
                "precision": precision,
                "k": payload.k,
                "took_ms": round(took_ms, 1),
                "model_load_ms": round(model_load_ms, 1),
                "stages": stages,
                "abstained": outcome.abstained,
                "weak_count": outcome.weak_count,
                "relaxed": bool(getattr(outcome, "relaxed", False)),
                "strict_count": getattr(outcome, "strict_count", None),
                "filters": filter_dict,
            },
        }

    def _build_results(self, index: Index, outcome, payload) -> list[dict]:
        hits = outcome.hits
        documents = index.get_documents([hit.doc_id for hit in hits])
        results = []
        for rank, hit in enumerate(hits, start=1):
            document = documents.get(hit.doc_id, {})
            score = float(hit.score)
            if not math.isfinite(score):
                raise ApiProblem(500, "invalid_score", "A non-finite search score was produced.")
            confidence = (
                outcome.confidences[rank - 1] if rank - 1 < len(outcome.confidences) else None
            )
            results.append(
                {
                    "rank": rank,
                    "doc_id": hit.doc_id,
                    "title": hit.title or str(document.get("title") or ""),
                    "source": hit.source or document.get("source"),
                    "snippet": {
                        "segments": build_snippet(hit, document.get("body"), payload.query)
                    },
                    "score": {
                        "value": score,
                        "metric": _score_metric(
                            payload.mode,
                            None if payload.mode == "lexical" else payload.precision,
                        ),
                    },
                    "explain": hit.explain,
                    "folder": document.get("folder"),
                    "ext": document.get("ext"),
                    "modified_at": _iso_from_epoch(document.get("mtime")),
                    "tags": list(document.get("tags") or []),
                    "bookmarked": index.is_bookmarked(hit.doc_id),
                    "duplicates": index.duplicates_of(hit.doc_id),
                    "confidence": confidence,
                    "passage": _passage_payload(hit),
                }
            )
        return results

    # -- documents -------------------------------------------------------------

    def resolve_document_file(
        self, index: Index, doc: dict, *, confirm_network: bool = False
    ) -> Path:
        """Locate a document's file from its root folder and relative source.

        Never derived from ``doc_id`` (which may be ``~N/...``). The result
        must resolve (symlinks included) inside that root, exist, and be a
        note type OneFind indexes: a collection file can come from another
        machine, so its paths are untrusted and must never launch a program.
        A root on a network location needs ``confirm_network``.
        """
        unsupported = ApiProblem(
            403,
            "unsupported_file",
            "OneFind only opens text and Markdown notes.",
        )
        missing = ApiProblem(
            404,
            "file_missing",
            "The file for this note could not be found. It may have been moved or "
            "deleted — reindex the folder to update the collection.",
            action="reindex",
        )
        root_id = doc.get("root_id")
        source = doc.get("source")
        if root_id is None or not source:
            raise missing
        root = next((r for r in index.roots() if r["root_id"] == root_id), None)
        if root is None:
            raise missing
        text = str(source)
        relative = PurePosixPath(text.replace("\\", "/"))
        if relative.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise unsupported
        _confirm_network(root["path"], confirm_network, "This note's folder")
        try:
            base = Path(root["path"]).resolve(strict=True)
        except (OSError, RuntimeError):
            raise missing from None
        if is_network_path(base):
            _confirm_network(base, confirm_network, "This note's folder")
        outside = ApiProblem(
            403,
            "path_outside_root",
            "This note's file is outside its indexed folder, so OneFind will not open it.",
            action="reindex",
        )
        if relative.is_absolute() or PureWindowsPath(text).drive or PureWindowsPath(text).root:
            raise outside
        try:
            target = base.joinpath(*relative.parts).resolve()
        except (OSError, RuntimeError, ValueError):
            raise missing from None
        if not _is_within(target, base) or target == base:
            raise outside
        if not target.is_file():
            raise missing
        # Checked again after resolving: a note.md symlink may point at a program.
        if target.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise unsupported
        return target

    # -- backups ---------------------------------------------------------------

    def backup_target(self, active: dict) -> Path:
        if active["registered"]:
            try:
                return self.library.backup_path_for(active["name"])
            except DataError:
                pass
        backups = self.library.backups_dir
        backups.mkdir(parents=True, exist_ok=True)
        stem = f"{slugify(active['name'])}-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"
        candidate = backups / f"{stem}.db"
        counter = 2
        while candidate.exists():
            candidate = backups / f"{stem}-{counter}.db"
            counter += 1
        return candidate

    def check_backup_path(self, raw: str) -> Path:
        source = _resolve_user_path(raw)
        inside = _is_within(source, self.library.backups_dir.resolve())
        if not inside and source.suffix.lower() != ".db":
            raise ApiProblem(
                400,
                "invalid_backup",
                "Choose a OneFind backup (a .db file, usually in the backups folder).",
            )
        if not source.is_file():
            raise ApiProblem(404, "path_not_found", f"The backup {source.name} could not be found.")
        _validate_database_file(source)
        return source


# -- the ASGI guard (headers, CSRF, last-resort errors) ----------------------------------


class _GuardMiddleware:
    """Security headers, CSRF header check, and a no-leak 500 fallback."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        method: str = scope.get("method", "GET")
        started = False

        async def guarded_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() not in {key for key, _ in _SECURITY_HEADERS}
                ]
                headers.extend(_SECURITY_HEADERS)
                names = {name.lower() for name, _ in headers}
                if path.startswith("/api/"):
                    headers = [(n, v) for n, v in headers if n.lower() != b"cache-control"]
                    headers.append((b"cache-control", b"no-store"))
                elif b"cache-control" not in names and (path == "/" or path.startswith("/assets/")):
                    headers.append((b"cache-control", b"no-cache"))
                message = {**message, "headers": headers}
            await send(message)

        if path.startswith("/api/"):
            # Browsers label every request with where it came from. Another
            # site (or another local server: localhost:3000 is "same-site")
            # must never reach the API, not even with a GET such as
            # /api/fs/list that touches the file system. Clients that send no
            # Sec-Fetch-Site header (CLI tools, tests) are unaffected.
            fetch_site = [
                value for name, value in scope.get("headers", []) if name.lower() == _FETCH_SITE
            ]
            if any(value.strip().lower() in _FOREIGN_SITES for value in fetch_site):
                response = _error(
                    403,
                    "csrf_check_failed",
                    "This request was blocked because it did not come from the OneFind app. "
                    "Reload the page and try again.",
                )
                await response(scope, receive, guarded_send)
                return

        needs_csrf = (method not in _SAFE_METHODS and path not in _CSRF_EXEMPT) or (
            path in _CSRF_REQUIRED_GETS
        )
        if path.startswith("/api/") and needs_csrf:
            supplied = [
                value for name, value in scope.get("headers", []) if name.lower() == _CSRF_HEADER
            ]
            if supplied != [b"1"]:
                response = _error(
                    403,
                    "csrf_check_failed",
                    "This request was blocked because it did not come from the OneFind app. "
                    "Reload the page and try again.",
                )
                await response(scope, receive, guarded_send)
                return

        try:
            await self.app(scope, receive, guarded_send)
        except Exception:  # noqa: BLE001 - never leak internals to the client
            logger.exception("unhandled error for %s %s", method, path)
            if started:
                raise
            response = _error(
                500,
                "internal_error",
                "The local search service could not complete the request.",
                retryable=True,
                action="retry",
            )
            await response(scope, receive, guarded_send)


# -- application factory ---------------------------------------------------------------


def create_app(
    index_path: str | Path | None = None,
    *,
    reset_token: str | None = None,
    allowed_hosts: list[str] | None = None,
    home: str | Path | None = None,
    allow_open: bool = True,
    opener: Opener | None = None,
    embedders: EmbedderCache | None = None,
    register: bool | None = None,
    autosync: bool = True,
    autosync_interval: float | None = None,
):
    """Create the local FastAPI application without opening network services.

    ``index_path`` serves that database as the active collection; without it
    the active collection of ``Library(home)`` is used. ``register`` controls
    whether ``index_path`` is added to the library (default: only when
    ``home`` or ``$ONEFIND_HOME`` names the library explicitly, so ad-hoc
    use never edits the per-user registry). ``opener(path, reveal)`` replaces
    the OS file opener and ``embedders`` the shared model cache (tests).
    ``autosync=False`` never watches folders, whatever a collection's saved
    auto-sync preference says; ``autosync_interval`` overrides the poll
    interval (default 5 s, or ``$ONEFIND_AUTOSYNC_INTERVAL``).
    """
    if not SERVE_AVAILABLE:
        raise UsageError('serve needs the [serve] extra - pip install -e ".[serve]"')

    target: Path | None = None
    if index_path is not None:
        target = Path(index_path)
        if target.is_symlink():
            raise UsageError("refusing to serve a symlink as the index database")
        target = target.absolute()

    if reset_token is not None and not 16 <= len(str(reset_token)) <= 256:
        # POST /api/reset only accepts 16-256 character tokens, so a shorter
        # or longer one could never be used.
        raise UsageError("--reset-token must be 16-256 characters")

    if register is None:
        register = home is not None or bool(os.environ.get("ONEFIND_HOME", "").strip())

    runtime = _Runtime(
        index_path=target,
        library=Library(home),
        register=bool(register),
        embedders=embedders if embedders is not None else EmbedderCache(),
        reset_token=reset_token,
        allow_open=allow_open,
        autosync=autosync,
        autosync_interval=autosync_interval,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        runtime.startup()
        try:
            yield
        finally:
            runtime.shutdown()

    app = FastAPI(
        title="OneFind local search",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.runtime = runtime
    app.state.opener = opener or default_opener
    app.state.allow_open = allow_open
    app.state.embedders = runtime.embedders
    app.state.jobs = runtime.jobs
    app.state.autosync = runtime.autosync

    if allowed_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    app.add_middleware(_GuardMiddleware)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError):
        details = "; ".join(str(error.get("msg", "invalid value")) for error in exc.errors())
        return _error(422, "validation_error", details or "The request is invalid.")

    @app.exception_handler(ApiProblem)
    async def api_problem(_request: Request, exc: ApiProblem):
        return JSONResponse(status_code=exc.status, content=exc.envelope())

    async def translated(_request: Request, exc: Exception):
        problem = _problem_from_exception(exc)
        if problem.status >= 500:
            logger.error("request failed", exc_info=exc)
        return JSONResponse(status_code=problem.status, content=problem.envelope())

    for kind in (OneFindError, sqlite3.Error, OSError):
        app.add_exception_handler(kind, translated)

    # -- page & assets -------------------------------------------------------------

    @app.get("/", response_class=FileResponse)
    def root() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html", media_type="text/html")

    @app.get("/assets/{filename}")
    def asset(filename: str):
        if not _ASSET_NAME.fullmatch(filename):
            return Response(status_code=404)
        path = _STATIC_DIR / filename
        if not path.is_file():
            return Response(status_code=404)
        return FileResponse(
            path,
            media_type=_ASSET_MEDIA_TYPES[path.suffix],
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "service": "onefind"}

    # -- status ------------------------------------------------------------------------

    @app.get("/api/stats")
    def stats() -> dict:
        return runtime.stats_payload()

    @app.get("/api/facets")
    def facets() -> dict:
        with runtime.lock:
            index = runtime.current_index()
            if index is None:
                return {"folders": [], "types": [], "tags": [], "modified": {"min": None, "max": None}}
            return index.facets()

    # -- search ------------------------------------------------------------------------

    @app.post("/api/search")
    def search(payload: SearchBody = Body(...)):
        try:
            return runtime.run_search(payload)
        except ApiProblem as problem:
            return JSONResponse(status_code=problem.status, content=problem.envelope())

    @app.post("/api/search/stream")
    def search_stream(payload: SearchBody = Body(...)):
        events: queue.Queue = queue.Queue()
        finished = object()

        def on_stage(name: str, status: str, ms: float) -> None:
            events.put(
                {"type": "stage", "name": name, "status": status, "ms": round(float(ms), 3)}
            )

        def worker() -> None:
            try:
                result = runtime.run_search(payload, on_stage=on_stage)
                events.put({"type": "result", **result})
            except BaseException as exc:  # noqa: BLE001 - always end the stream
                problem = _problem_from_exception(exc)
                if problem.status >= 500 and not isinstance(exc, ApiProblem):
                    logger.error("streamed search failed", exc_info=exc)
                error = problem.envelope()["error"]
                error["status"] = problem.status
                events.put({"type": "error", "status": problem.status, "error": error})
            finally:
                events.put(finished)

        threading.Thread(target=worker, name="onefind-search", daemon=True).start()

        def stream() -> Iterator[bytes]:
            while True:
                item = events.get()
                if item is finished:
                    return
                line = json.dumps(item, ensure_ascii=False, allow_nan=False)
                yield (line + "\n").encode("utf-8")

        return StreamingResponse(
            stream(),
            media_type="application/x-ndjson",
            headers={"X-Accel-Buffering": "no"},
        )

    # -- collections ---------------------------------------------------------------

    @app.get("/api/collections")
    def collections() -> dict:
        return runtime.collections_payload()

    @app.post("/api/collections")
    def create_collection(payload: CollectionCreateBody = Body(...)):
        db_path = None
        if payload.db_path and payload.db_path.strip():
            db_path = _resolve_user_path(payload.db_path)
            if db_path.suffix.lower() not in _DB_SUFFIXES:
                raise ApiProblem(
                    400, "invalid_path", "A collection file name must end in .db."
                )
            if db_path.exists():
                raise ApiProblem(
                    409,
                    "path_exists",
                    f"The file {db_path.name} already exists. Open it as a collection instead.",
                    action="open_collection",
                )
        try:
            entry = runtime.library.create(payload.name, db_path)
        except (NameTakenError, AlreadyRegisteredError):
            raise
        except DataError as exc:
            # Create-time failures (a folder that cannot be made, a file that
            # appeared meanwhile) are not collection damage: no Repair hint.
            if "already exists" in str(exc):
                raise ApiProblem(
                    409,
                    "path_exists",
                    "That file already exists. Open it as a collection instead.",
                    action="open_collection",
                ) from exc
            raise ApiProblem(400, "invalid_path", str(exc)) from exc
        with runtime.lock:
            if runtime.active is None:
                runtime.switch_to(entry)
        return JSONResponse(status_code=201, content=runtime.entry_payload(entry))

    @app.post("/api/collections/open")
    def open_collection(payload: CollectionOpenBody = Body(...)):
        db_path = _resolve_user_path(payload.db_path)
        if not db_path.is_file():
            raise ApiProblem(
                404, "path_not_found", f"The file {db_path.name} could not be found."
            )
        if db_path.suffix.lower() not in _DB_SUFFIXES:
            raise ApiProblem(
                400, "invalid_path", "Choose a OneFind collection file (it ends in .db)."
            )
        existing = runtime.library.find_by_db(db_path)
        if existing is None:
            try:
                entry = runtime.library.register(payload.name, db_path)
            except AlreadyRegisteredError as exc:
                entry = runtime.library.get(exc.existing)
            except NameTakenError:
                raise
            except DataError as exc:
                raise ApiProblem(
                    400,
                    "invalid_collection",
                    "That file is not a OneFind collection, or it was made by a newer version.",
                    action="open_collection",
                ) from exc
        else:
            entry = existing
        with runtime.lock:
            runtime.switch_to(entry)
            runtime.library.set_active(entry["name"])
            network_roots: list[str] = []
            index = runtime.current_index()
            if index is not None:
                try:
                    network_roots = [
                        root["path"] for root in index.roots() if is_network_path(root["path"])
                    ]
                except (OneFindError, sqlite3.Error):
                    network_roots = []
        payload_out = runtime.entry_payload(runtime.library.get(entry["name"]))
        # Folders on other computers are never contacted automatically; the UI
        # can warn about them right after opening a collection from elsewhere.
        payload_out["network_roots"] = network_roots
        return payload_out

    @app.post("/api/collections/active")
    def activate_collection(payload: CollectionNameBody = Body(...)):
        with runtime.lock:
            active = runtime.active
            try:
                entry = runtime.library.get(payload.name)
            except DataError:
                # No registered collection by that name: asking for the ad-hoc
                # --db collection that is already open is a no-op.
                if active is not None and not active["registered"] and _same_name(
                    active["name"].casefold(), payload.name.casefold()
                ):
                    return runtime.stats_payload()
                entry = runtime.find_entry(payload.name)  # raises the 400/404 problem
            if active is None or _path_key(active["db"]) != _path_key(entry["db"]):
                runtime.switch_to(entry)
            else:
                runtime.active = {"name": entry["name"], "db": Path(entry["db"]), "registered": True}
            runtime.library.set_active(entry["name"])
            return runtime.stats_payload()

    @app.post("/api/collections/rename")
    def rename_collection(payload: CollectionRenameBody = Body(...)):
        with runtime.lock:
            entry = runtime.find_entry(payload.name)
            updated = runtime.library.rename(entry["name"], payload.new_name)
            if runtime.active is not None and _path_key(runtime.active["db"]) == _path_key(
                updated["db"]
            ):
                runtime.active = {**runtime.active, "name": updated["name"], "registered": True}
            return runtime.entry_payload(updated)

    @app.post("/api/collections/remove")
    def remove_collection(payload: CollectionRemoveBody = Body(...)):
        with runtime.lock:
            entry = runtime.find_entry(payload.name)
            if payload.delete_file and not _same_name(payload.confirm_name, entry["name"]):
                raise ApiProblem(
                    400,
                    "confirmation_required",
                    f"To delete the collection file, type its name “{entry['name']}” to confirm.",
                )
            runtime.ensure_not_busy(entry["db"])
            runtime.stop_autosync(entry["db"])
            is_active = runtime.active is not None and _path_key(runtime.active["db"]) == _path_key(
                entry["db"]
            )
            if is_active:
                runtime._close_index()
            try:
                runtime.library.remove(entry["name"], delete_file=payload.delete_file)
            except Exception:
                if is_active:
                    runtime._open_active()
                runtime.follow_autosync()
                raise
            if is_active:
                runtime.active = None
                following = runtime.library.active()
                if following is not None:
                    try:
                        runtime.switch_to(following)
                    except ApiProblem:
                        runtime.active = {
                            "name": following["name"],
                            "db": Path(following["db"]),
                            "registered": True,
                        }
            runtime.follow_autosync()
            return {
                "removed": entry["name"],
                "deleted_file": bool(payload.delete_file),
                "active": runtime.active["name"] if runtime.active else None,
            }

    # -- filesystem -----------------------------------------------------------------

    @app.get("/api/fs/list")
    def fs_list(path: str | None = None) -> dict:
        return list_directory(path)

    # -- indexing jobs ---------------------------------------------------------------

    @app.post("/api/index")
    def start_index(payload: IndexBody = Body(...)):
        _confirm_network(payload.path, payload.confirm_network, "That folder")
        root = _resolve_user_path(payload.path)
        if is_network_path(root):
            _confirm_network(root, payload.confirm_network, "That folder")
        if not root.exists():
            raise ApiProblem(
                404,
                "path_not_found",
                f"The folder {root} does not exist. It may have been moved, renamed, or unplugged.",
                action="index",
            )
        if root.is_file() and root.suffix.lower() not in SUPPORTED_FILES:
            raise ApiProblem(
                400,
                "unsupported_file",
                "OneFind can index .txt, .md, .markdown, .rst, .org and .text files (and .jsonl datasets).",
            )
        exclude = normalize_excludes(payload.exclude) if payload.exclude is not None else None
        with runtime.lock:
            wanted = (payload.collection or "").strip()
            current = runtime.active["name"] if runtime.active else None
            # Names are unique regardless of case (library.validate_name).
            if wanted and (current is None or wanted.casefold() != current.strip().casefold()):
                # Sent for one collection, but another is active now (switched
                # in another tab, or while a queue of folders was running).
                raise ApiProblem(
                    409,
                    "collection_changed",
                    f"“{wanted}” is no longer the open collection, so nothing was indexed. "
                    "Switch back to it and try again.",
                    retryable=False,
                )
            if runtime.active is None:
                try:
                    base = validate_name(root.stem if root.is_file() else root.name)
                except UsageError:
                    base = "Notes"
                entry = None
                for attempt in range(1, 51):
                    candidate = base if attempt == 1 else f"{base} {attempt}"
                    try:
                        entry = runtime.library.create(candidate)
                        break
                    except NameTakenError:
                        continue
                if entry is None:
                    raise ApiProblem(409, "name_taken", "Create a collection first, then index into it.")
                runtime.switch_to(entry)
                runtime.library.set_active(entry["name"])
            active = runtime.active
            assert active is not None
            runtime.ensure_not_busy(active["db"])
            try:
                job = runtime.jobs.start_index(
                    active["db"],
                    str(root),
                    exclude=exclude,
                    embed=payload.embed,
                    prune=payload.prune,
                    incremental=not payload.full,
                    label=active["name"],
                    refresh_files=payload.scope == "auto",
                )
            except UsageError as exc:
                if "already running" in str(exc):
                    runtime.ensure_not_busy(active["db"])
                raise
            if payload.auto_sync is not None:
                # The watcher starts now but waits for this job to finish.
                try:
                    runtime.set_autosync(bool(payload.auto_sync))
                except (ApiProblem, OneFindError, sqlite3.Error):
                    logger.warning("could not save the auto-sync preference", exc_info=True)
        return JSONResponse(status_code=202, content=job)

    def _find_root(index: Index, root_id: int | None, raw_path: str | None) -> dict:
        root = None
        if root_id is not None:
            root = index.get_root_by_id(root_id)
        elif raw_path and raw_path.strip():
            text = raw_path.strip()
            root = index.get_root(text)
            if root is None and not is_network_path(text):
                try:
                    root = index.get_root(str(Path(text).expanduser().resolve()))
                except (OSError, RuntimeError, ValueError):
                    root = None
            if root is None:
                wanted = _path_key(text)
                root = next((r for r in index.roots() if _path_key(r["path"]) == wanted), None)
        else:
            raise ApiProblem(400, "invalid_request", "Say which folder: root_id or root.")
        if root is None:
            raise ApiProblem(404, "not_found", "That folder is not part of this collection.")
        return root

    @app.post("/api/roots/remove")
    def remove_root(payload: RootRemoveBody = Body(...)) -> dict:
        """Forget a folder (e.g. moved or deleted) and remove its notes.

        Files on disk are never touched. ``delete_documents=false`` keeps the
        notes searchable without a folder.
        """
        with runtime.using_index() as index:
            active = runtime.require_active()
            runtime.ensure_not_busy(active["db"])
            root = _find_root(index, payload.root_id, payload.root)
            report = index.forget_root(
                root["root_id"], delete_documents=bool(payload.delete_documents)
            )
            return {
                "removed": report["removed"],
                "root": root["path"],
                "root_id": root["root_id"],
                "forgotten": report["forgotten"],
                "stats": runtime.stats_payload(),
            }

    @app.post("/api/roots/relocate")
    def relocate_root(payload: RootRelocateBody = Body(...)) -> dict:
        """Point a moved folder at its new location, keeping every note's id."""
        target = _resolve_user_path(payload.path)
        if not target.is_dir():
            raise ApiProblem(
                404, "path_not_found", f"The folder {target} does not exist.", action="index"
            )
        with runtime.using_index() as index:
            active = runtime.require_active()
            runtime.ensure_not_busy(active["db"])
            root = _find_root(index, payload.root_id, None)
            updated = index.relocate_root(root["root_id"], str(target))
            return {"root": updated, "previous": root["path"]}

    @app.get("/api/roots")
    def list_roots() -> dict:
        """The active collection's folders with their saved exclude patterns.

        No file-system access (the wizard reads it to pre-fill a folder's
        saved patterns; /api/index/changes is the one that scans folders).
        """
        with runtime.lock:
            index = runtime.current_index()
            if index is None:
                return {"roots": []}
            roots = index.roots()
        return {
            "roots": [
                {
                    "root_id": root["root_id"],
                    "path": root["path"],
                    "exclude": list(root.get("exclude") or []),
                    "documents": root["documents"],
                    "added_at": root.get("added_at"),
                    "last_indexed_at": root.get("last_indexed_at"),
                    "files_only": bool(root.get("files_only")),
                    "network": is_network_path(root["path"]),
                }
                for root in roots
            ]
        }

    @app.post("/api/maintenance/rebuild-vectors", status_code=202)
    def rebuild_vectors(payload: RebuildVectorsBody = Body(...)):
        """Re-split notes into passages and re-embed them as a background job.

        ``chunking``: "keep" (rebuild under the current setting), "default"
        (turn passage search on) or "off" (one vector per note). Polled like
        an index job (``kind: "rebuild_vectors"``).
        """
        with runtime.lock:
            active = runtime.require_active()
            if runtime.current_index() is None:
                raise ApiProblem(
                    503,
                    "no_index",
                    "No index is ready yet. Index a folder to start searching.",
                    retryable=True,
                    action="index",
                )
            runtime.ensure_not_busy(active["db"])
            option = None if payload.chunking == "off" else payload.chunking
            try:
                job = runtime.jobs.start_rebuild_vectors(
                    active["db"], chunking=option, label=active["name"]
                )
            except UsageError as exc:
                if "already running" in str(exc):
                    runtime.ensure_not_busy(active["db"])
                raise
        return JSONResponse(status_code=202, content=_public_job(job))

    @app.get("/api/jobs")
    def list_jobs(active: str | None = None) -> dict:
        active_only = str(active or "").strip().lower() in {"1", "true", "yes"}
        return {"jobs": runtime.jobs.list(active_only=active_only)}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict:
        return runtime.jobs.get(job_id)

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict:
        return runtime.jobs.cancel(job_id)

    # -- auto-sync ---------------------------------------------------------------------

    @app.get("/api/autosync")
    def autosync_status() -> dict:
        return runtime.autosync_payload()

    @app.post("/api/autosync")
    def autosync_toggle(payload: AutoSyncBody = Body(...)) -> dict:
        """Turn auto-sync on/off for the active collection (saved in its file)."""
        return runtime.set_autosync(payload.enabled)

    @app.get("/api/index/changes")
    def index_changes() -> dict:
        with runtime.lock:
            index = runtime.current_index()
            if index is None:
                return {"roots": [], "stale": False}
            reports = []
            for root in index.roots():
                if is_network_path(root["path"]):
                    # Never contact another computer automatically (the idle
                    # screen calls this on its own); a collection file from
                    # elsewhere could point at any host.
                    report = {
                        "root": root["path"],
                        "exists": None,
                        "network": True,
                        "new": 0,
                        "changed": 0,
                        "deleted": 0,
                        "unchanged": 0,
                        "truncated": False,
                    }
                    report.update(
                        root_id=root["root_id"],
                        documents=root["documents"],
                        last_indexed_at=root["last_indexed_at"],
                    )
                    reports.append(report)
                    continue
                try:
                    report = scan_changes(index, root["path"])
                except (OneFindError, OSError):
                    logger.info("change scan failed for a root", exc_info=True)
                    report = {
                        "root": root["path"],
                        "exists": False,
                        "new": 0,
                        "changed": 0,
                        "deleted": 0,
                        "unchanged": 0,
                        "truncated": False,
                    }
                report.update(
                    root_id=root["root_id"],
                    documents=root["documents"],
                    last_indexed_at=root["last_indexed_at"],
                )
                reports.append(report)
        stale = any(
            report["exists"] is False
            or report["new"]
            or report["changed"]
            or report["deleted"]
            for report in reports
        )
        return {"roots": reports, "stale": stale}

    # -- documents ----------------------------------------------------------------------

    @app.post("/api/documents/open")
    def open_document(request: Request, payload: OpenDocumentBody = Body(...)):
        client = request.client.host if request.client else None
        if not app.state.allow_open or not _is_loopback(client):
            raise ApiProblem(
                403,
                "open_disabled",
                "Opening files is turned off for this server. Copy the path instead.",
            )
        with runtime.using_index() as index:
            doc = index.get_document(payload.doc_id)
            if doc is None:
                raise ApiProblem(404, "not_found", "That note is no longer in this collection.")
            path = runtime.resolve_document_file(
                index, doc, confirm_network=bool(payload.confirm_network)
            )
        try:
            app.state.opener(path, bool(payload.reveal))
        except Exception as exc:  # noqa: BLE001 - OS launcher failures vary widely
            logger.warning("could not open a document", exc_info=True)
            raise ApiProblem(
                500,
                "open_failed",
                "The file could not be opened with your default app.",
                retryable=True,
                action="retry",
            ) from exc
        with runtime.lock:
            index = runtime.current_index()
            if index is not None:
                try:
                    index.record_open(payload.doc_id)
                except (OneFindError, sqlite3.Error):
                    logger.info("could not record document activity", exc_info=True)
        return {"opened": True, "doc_id": payload.doc_id, "reveal": bool(payload.reveal)}

    @app.post("/api/documents/remove")
    def remove_documents(payload: RemoveDocumentsBody = Body(...)) -> dict:
        with runtime.using_index() as index:
            return {"removed": index.remove_documents(payload.doc_ids)}

    @app.get("/api/documents/{doc_id:path}")
    def get_document(doc_id: str) -> dict:
        with runtime.using_index() as index:
            doc = index.get_document(doc_id)
            if doc is None:
                raise ApiProblem(404, "not_found", "That note is no longer in this collection.")
            body = doc.get("body") or ""
            doc["truncated"] = len(body) > MAX_DOCUMENT_CHARS
            doc["body"] = body[:MAX_DOCUMENT_CHARS]
            doc["modified_at"] = _iso_from_epoch(doc.get("mtime"))
            doc["duplicates"] = index.duplicates_of(doc_id)
            return doc

    # -- history, saved queries, bookmarks ---------------------------------------------

    @app.get("/api/history")
    def history(limit: int = 10) -> dict:
        with runtime.lock:
            index = runtime.current_index()
            if index is None:
                return {"items": []}
            return {"items": index.recent_searches(max(1, min(int(limit), 200)))}

    @app.post("/api/history")
    def record_history(payload: HistoryBody = Body(...)) -> dict:
        try:
            filters = SearchFilters.from_dict(payload.filters).to_dict() if payload.filters else {}
        except UsageError as exc:
            raise ApiProblem(400, "invalid_filters", str(exc)) from exc
        precision = None if payload.mode == "lexical" else payload.precision
        with runtime.using_index() as index:
            index.record_search(
                payload.query, payload.mode, precision, filters or None, payload.result_count
            )
            return {"items": index.recent_searches(10)}

    @app.post("/api/history/clear")
    def clear_history() -> dict:
        with runtime.using_index() as index:
            return {"cleared": index.clear_history(), "items": []}

    @app.get("/api/saved")
    def saved() -> dict:
        with runtime.lock:
            index = runtime.current_index()
            return {"items": index.saved_queries() if index is not None else []}

    @app.post("/api/saved")
    def save_query(payload: SavedBody = Body(...)) -> dict:
        try:
            filters = SearchFilters.from_dict(payload.filters).to_dict() if payload.filters else None
        except UsageError as exc:
            raise ApiProblem(400, "invalid_filters", str(exc)) from exc
        precision = None if payload.mode == "lexical" else payload.precision
        with runtime.using_index() as index:
            return index.save_query(
                payload.name, payload.query, payload.mode, precision, filters or None
            )

    @app.delete("/api/saved/{query_id}")
    def delete_saved(query_id: str) -> dict:
        with runtime.using_index() as index:
            if not index.delete_saved_query(query_id):
                raise ApiProblem(404, "not_found", "That saved search no longer exists.")
            return {"deleted": True, "id": int(query_id)}

    @app.get("/api/bookmarks")
    def bookmarks() -> dict:
        with runtime.lock:
            index = runtime.current_index()
            return {"items": index.bookmarks() if index is not None else []}

    @app.post("/api/bookmarks")
    def add_bookmark(payload: BookmarkBody = Body(...)) -> dict:
        with runtime.using_index() as index:
            return index.add_bookmark(payload.doc_id, payload.note)

    @app.delete("/api/bookmarks/{doc_id:path}")
    def remove_bookmark(doc_id: str) -> dict:
        with runtime.using_index() as index:
            return {"removed": index.remove_bookmark(doc_id), "doc_id": doc_id}

    @app.get("/api/recent-documents")
    def recent_documents(limit: int = 8) -> dict:
        with runtime.lock:
            index = runtime.current_index()
            if index is None:
                return {"items": []}
            items = index.recent_documents(max(1, min(int(limit), 50)))
        for item in items:
            item["modified_at"] = _iso_from_epoch(item.pop("mtime", None))
        return {"items": items}

    # -- maintenance ------------------------------------------------------------------

    @app.post("/api/maintenance/backup")
    def backup() -> dict:
        with runtime.using_index() as index:
            active = runtime.require_active()
            report = index.backup_to(runtime.backup_target(active))
        return {"path": report["path"], "bytes": report["bytes"], "created_at": utcnow_iso()}

    @app.get("/api/backups")
    def backups() -> dict:
        with runtime.lock:
            name = runtime.active["name"] if runtime.active else None
        return {"items": runtime.library.list_backups(name)}

    @app.post("/api/maintenance/restore")
    def restore(payload: RestoreBody = Body(...)) -> dict:
        source = runtime.check_backup_path(payload.backup_path)
        if payload.mode == "new":
            return _restore_as_new(source, payload.name)
        return _restore_replace(source, payload.confirm_name)

    def _restore_as_new(source: Path, raw_name: str | None) -> dict:
        name = validate_name(raw_name or f"{source.stem} (restored)")
        if any(entry["name"].casefold() == name.casefold() for entry in runtime.library.list()):
            raise NameTakenError(f"a collection named {name!r} already exists")
        directory = runtime.library.collections_dir
        directory.mkdir(parents=True, exist_ok=True)
        slug = slugify(name)
        target = directory / f"{slug}.db"
        counter = 2
        while any(Path(f"{target}{ext}").exists() for ext in ("", "-wal", "-shm")):
            target = directory / f"{slug}-{counter}.db"
            counter += 1
        try:
            _copy_database(source, target)
            entry = runtime.library.register(name, target)
        except BaseException:
            _delete_family(target)
            raise
        with runtime.lock:
            runtime.switch_to(entry)
            runtime.library.set_active(entry["name"])
        return {
            "restored": True,
            "mode": "new",
            "collection": runtime.entry_payload(runtime.library.get(entry["name"])),
        }

    def _restore_replace(source: Path, confirm_name: str | None) -> dict:
        with runtime.lock:
            active = runtime.require_active()
            if not _same_name(confirm_name, active["name"]):
                raise ApiProblem(
                    400,
                    "confirmation_required",
                    f"To replace the collection, type its name “{active['name']}” to confirm.",
                )
            db = Path(active["db"])
            if _path_key(source) == _path_key(db):
                raise ApiProblem(400, "invalid_backup", "That file is the collection itself.")
            runtime.ensure_not_busy(db)
            runtime.stop_autosync(db)
            safety = None
            index = runtime.current_index()
            if index is not None:
                safety = index.backup_to(runtime.backup_target(active))
            staging = db.with_name(f".{db.name}.restore-{os.getpid()}.tmp")
            _delete_family(staging)
            try:
                _copy_database(source, staging)
                Index.open(staging).close()  # validates and upgrades old backups
            except BaseException:
                _delete_family(staging)
                raise
            runtime._close_index()
            try:
                for leftover in (Path(f"{db}-wal"), Path(f"{db}-shm")):
                    leftover.unlink(missing_ok=True)
                os.replace(staging, db)
            except OSError as exc:
                _delete_family(staging)
                runtime._open_active()
                runtime.follow_autosync()
                raise ApiProblem(
                    409,
                    "collection_busy",
                    "The collection file is in use by another program. Close it and try again.",
                    retryable=True,
                    action="retry",
                ) from exc
            _delete_family(staging)
            runtime._open_active()
            runtime.follow_autosync()
            return {
                "restored": True,
                "mode": "replace",
                "safety_backup": (
                    {"path": safety["path"], "bytes": safety["bytes"]} if safety else None
                ),
                "stats": runtime.stats_payload(),
            }

    @app.post("/api/maintenance/check")
    def check() -> dict:
        with runtime.using_index() as index:
            # A running job holds the write lock between batches; checking now
            # would only wait and could not run the keyword-index check.
            runtime.ensure_not_busy()
            return index.integrity_check()

    @app.post("/api/maintenance/repair")
    def repair() -> dict:
        with runtime.using_index() as index:
            runtime.ensure_not_busy()
            try:
                runtime.bind_embedder(index)
            except ApiProblem:
                pass  # repair still rebuilds keyword data without the model
            return index.repair()

    @app.post("/api/maintenance/compact")
    def compact() -> dict:
        with runtime.using_index() as index:
            runtime.ensure_not_busy()
            return index.compact()

    @app.post("/api/maintenance/clear")
    def clear(payload: ClearBody = Body(...)) -> dict:
        with runtime.using_index() as index:
            active = runtime.require_active()
            if not _same_name(payload.confirm_name, active["name"]):
                raise ApiProblem(
                    400,
                    "confirmation_required",
                    f"To clear the collection, type its name “{active['name']}” to confirm.",
                )
            runtime.ensure_not_busy()
            index.clear()  # also drops the auto-sync preference and every folder
            runtime.follow_autosync()
            return {"cleared": True, "stats": runtime.stats_payload()}

    # -- legacy reset ------------------------------------------------------------------

    @app.post("/api/reset")
    def reset(payload: ResetBody = Body(...)):
        with runtime.lock:
            if runtime.reset_token is None:
                return _error(
                    403,
                    "reset_disabled",
                    "Reset is disabled. Start the server with an explicit --reset-token to enable it.",
                )
            if not secrets.compare_digest(
                payload.confirm_token.encode("utf-8"), str(runtime.reset_token).encode("utf-8")
            ):
                return _error(409, "wrong_token", "The reset token is incorrect.")
            active = runtime.require_active()
            runtime.ensure_not_busy()
            db = Path(active["db"])
            runtime.stop_autosync(db)
            runtime._close_index()
            _delete_family(db)
            runtime.index = Index.open(db)
            return {"reset": True}

    return app
