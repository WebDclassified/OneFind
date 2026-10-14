"""Background indexing jobs and a process-wide embedding-model cache.

The web app must stay responsive while a folder is indexed, so indexing runs
in a daemon thread that opens its **own** :class:`~onefind.store.Index`
connection (WAL lets the server keep reading the same file). Progress from
:func:`~onefind.ingest.ingest_path` is folded into a plain job dict that the
UI polls; see docs/11-implementation-contract.md section 5 for the shape.

Loading a sentence-transformers model takes seconds and hundreds of MB, so
:class:`EmbedderCache` keeps exactly one instance per model name and shares
it between jobs and searches (``SentenceEmbedder`` serializes encoding).
"""

from __future__ import annotations

import copy
import errno
import importlib.util
import os
import sqlite3
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .chunking import normalize_chunking
from .envcheck import DEFAULT_MODEL
from .errors import (
    CorpusNotFoundError,
    DataError,
    EnvError,
    OneFindError,
    OperationCancelled,
    UsageError,
)
from .store import Index, utcnow_iso

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .embed import SentenceEmbedder

__all__ = [
    "ACTIVE_STATUSES",
    "EmbedderCache",
    "JobManager",
    "JOB_KINDS",
    "JobNotFoundError",
    "KEEP_CHUNKING",
    "MAX_JOB_HISTORY",
    "STEP_NAMES",
    "model_stack_available",
]

STEP_NAMES = ("scan", "index", "vectors", "ready")
JOB_KINDS = ("index", "rebuild_vectors")
# start_rebuild_vectors(chunking=KEEP_CHUNKING) leaves the passage setting alone.
KEEP_CHUNKING = "keep"
ACTIVE_STATUSES = frozenset({"queued", "running"})
FINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
MAX_JOB_HISTORY = 50

EmbedderFactory = Callable[[str], "SentenceEmbedder"]


class JobNotFoundError(DataError):
    """No job with that id is known to this process (map to HTTP 404)."""


class _ModelUnavailable(Exception):
    """Internal: the embedding model could not be loaded."""


class _AttachFailed(Exception):
    """Internal: the model loaded but could not be bound to this index."""


def model_stack_available() -> bool:
    """True when sentence-transformers is importable (without importing it)."""
    try:
        return importlib.util.find_spec("sentence_transformers") is not None
    except (ImportError, ValueError):
        return False


def _db_key(db_path: str | Path) -> str:
    """Normalized identity of a database file (case-insensitive on Windows)."""
    return os.path.normcase(str(Path(db_path).expanduser().resolve()))


# -- model cache ----------------------------------------------------------------


class EmbedderCache:
    """Process-wide cache holding one embedder per model name.

    ``get()`` loads lazily and is safe under concurrent callers: each model
    name is loaded at most once at a time, and a successful load is shared.
    A failed load is not cached, so a later call can retry (e.g. after the
    model extra was installed). ``factory`` replaces ``SentenceEmbedder`` in
    tests.
    """

    def __init__(self, factory: EmbedderFactory | None = None) -> None:
        self._factory = factory
        self._lock = threading.Lock()
        self._models: dict[str, Any] = {}
        self._loading: dict[str, threading.Lock] = {}

    def _create(self, model_name: str) -> "SentenceEmbedder":
        if self._factory is not None:
            return self._factory(model_name)
        from .embed import SentenceEmbedder

        return SentenceEmbedder(model_name)

    def get(self, model_name: str | None = None) -> "SentenceEmbedder":
        """Return the cached embedder for ``model_name``, loading it once."""
        name = model_name or DEFAULT_MODEL
        with self._lock:
            model = self._models.get(name)
            if model is not None:
                return model
            name_lock = self._loading.setdefault(name, threading.Lock())
        with name_lock:
            with self._lock:
                model = self._models.get(name)
            if model is not None:
                return model  # another thread finished loading while we waited
            model = self._create(name)
            with self._lock:
                self._models[name] = model
            return model

    def loaded(self, model_name: str | None = None) -> bool:
        """True when ``model_name`` is already in memory (never loads it)."""
        with self._lock:
            return (model_name or DEFAULT_MODEL) in self._models

    def peek(self, model_name: str | None = None) -> "SentenceEmbedder | None":
        """The cached embedder or None, without loading."""
        with self._lock:
            return self._models.get(model_name or DEFAULT_MODEL)

    def put(self, model_name: str, embedder: "SentenceEmbedder") -> None:
        """Seed the cache with an embedder loaded elsewhere."""
        with self._lock:
            self._models[model_name] = embedder

    def available(self) -> bool:
        """True when models can be loaded (a factory is set or the stack exists)."""
        return self._factory is not None or model_stack_available()

    def names(self) -> list[str]:
        """Model names currently held in memory."""
        with self._lock:
            return sorted(self._models)

    def clear(self) -> None:
        """Drop every cached model (they are freed once nothing references them)."""
        with self._lock:
            self._models.clear()


# -- error translation -----------------------------------------------------------


def _find_cause(exc: BaseException, kind: type[BaseException]) -> BaseException | None:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, kind):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None


def _error(code: str, message: str, *, retryable: bool = False, action: str | None = None) -> dict:
    error = {"code": code, "message": message, "retryable": retryable}
    if action is not None:
        error["action"] = action
    return error


def _describe_error(exc: BaseException, root: str) -> dict:
    """Translate an exception from a job into a plain-language error dict."""
    detail = str(exc).strip() or type(exc).__name__
    if isinstance(exc, _ModelUnavailable):
        return _error(
            "model_unavailable",
            "The meaning-search model could not be loaded "
            f"({detail}). You can index without vectors for keyword search only, "
            'or install the model extra with: pip install -e ".[model]"',
            retryable=True,
            action="switch_mode",
        )
    if isinstance(exc, _AttachFailed):
        cause = exc.__cause__
        if isinstance(cause, EnvError):
            return _error(
                "vectors_unavailable",
                "Vector storage (sqlite-vec) is not available in this Python, so "
                "meaning search cannot be built. Index without vectors for keyword "
                "search, or run 'onefind check' for the fix.",
                action="switch_mode",
            )
        return _error(
            "model_mismatch",
            "This collection was built with a different meaning model, and models "
            f"cannot be mixed ({detail}). Reindex with the original model or start "
            "a new collection.",
            action="open_collection",
        )
    permission = _find_cause(exc, PermissionError)
    if permission is None and isinstance(exc, OSError) and exc.errno in {
        errno.EACCES,
        errno.EPERM,
    }:
        permission = exc
    if permission is not None:
        target = getattr(permission, "filename", None) or root
        return _error(
            "permission_denied",
            f"OneFind is not allowed to read {target}. Check the folder's "
            "permissions, or pick a folder you own.",
            retryable=True,
            action="retry",
        )
    if isinstance(exc, CorpusNotFoundError):
        if not Path(root).exists():
            return _error(
                "path_not_found",
                f"The folder {root} does not exist (it may have been moved, renamed, "
                "or unplugged). Choose the folder again.",
                action="index",
            )
        return _error(
            "nothing_to_index",
            f"No notes OneFind can read were found in {root}. Supported files are "
            ".txt, .md, .markdown, .rst, .org and .text (plus .jsonl datasets); "
            "hidden folders and your exclude patterns are skipped.",
            action="index",
        )
    if isinstance(exc, sqlite3.OperationalError) and "locked" in detail.lower():
        return _error(
            "collection_busy",
            "The collection file is busy (another program is writing to it). "
            "Wait a moment and try again.",
            retryable=True,
            action="retry",
        )
    if isinstance(exc, (sqlite3.DatabaseError, DataError)):
        return _error(
            "collection_unreadable",
            f"The collection file could not be updated: {detail}. Run a health "
            "check, or restore a backup.",
            action="repair",
        )
    if isinstance(exc, EnvError):
        return _error("environment", detail, action="retry")
    if isinstance(exc, UsageError):
        return _error("invalid_request", detail)
    if isinstance(exc, OSError):
        return _error(
            "read_failed",
            f"A file could not be read ({detail}). Check that the folder is "
            "still available and try again.",
            retryable=True,
            action="retry",
        )
    if isinstance(exc, OneFindError):
        return _error("indexing_failed", detail, retryable=True, action="retry")
    return _error(
        "internal_error",
        f"Indexing stopped unexpectedly ({type(exc).__name__}: {detail}). "
        "Notes saved before the problem are kept; try again.",
        retryable=True,
        action="retry",
    )


# -- jobs ----------------------------------------------------------------------------


class _Job:
    """Internal bookkeeping for one job; ``data`` is the public dict."""

    def __init__(self, data: dict, db_key: str) -> None:
        self.data = data
        self.db_key = db_key
        self.cancel_event = threading.Event()
        self.finished = threading.Event()
        self.thread: threading.Thread | None = None


class JobManager:
    """Runs indexing jobs in daemon threads and tracks their progress.

    One job may run per database file at a time. Finished jobs are kept in
    memory (most recent :data:`MAX_JOB_HISTORY`). ``get()`` and ``list()``
    return deep copies, so callers can never observe a half-written update.
    """

    def __init__(self, embedders: EmbedderCache | None = None) -> None:
        self.embedders = embedders if embedders is not None else EmbedderCache()
        self._lock = threading.Lock()
        self._jobs: OrderedDict[str, _Job] = OrderedDict()

    # -- public API ------------------------------------------------------------

    def start_index(
        self,
        db_path: str | Path,
        root: str | Path,
        *,
        exclude: list[str] | None = None,
        embed: bool | None = None,
        model_name: str | None = None,
        prune: bool = True,
        incremental: bool = True,
        label: str | None = None,
        refresh_files: bool = False,
    ) -> dict:
        """Start indexing ``root`` into ``db_path`` in the background.

        ``embed=None`` builds vectors when the index already has them or the
        model stack is installed; ``embed=False`` on an index that already has
        vectors still embeds (writing lexical-only rows would desynchronize
        it) and says so in the job message. ``refresh_files`` re-reads only
        the indexed files of a folder registered by indexing single files (see
        :func:`~onefind.ingest.ingest_path`). Raises :class:`UsageError` when
        a job is already running for the same database.
        """
        if db_path is None or not str(db_path).strip():
            raise UsageError("a collection database path is required")
        if root is None or not str(root).strip():
            raise UsageError("choose a folder or file to index")
        if embed is not None and not isinstance(embed, bool):
            raise UsageError("embed must be true, false, or null")
        db_key = _db_key(db_path)
        root_text = str(Path(str(root)).expanduser())
        job_id = uuid.uuid4().hex[:12]
        data = {
            "id": job_id,
            "kind": "index",
            "label": label,
            "db": str(Path(db_path).expanduser()),
            "root": root_text,
            "status": "queued",
            "phase": "scan",
            "done": 0,
            "total": None,
            "message": "Waiting to start",
            "started_at": utcnow_iso(),
            "finished_at": None,
            "result": None,
            "error": None,
            "embed": embed,
            "model": None,
            "options": {
                "exclude": list(exclude) if exclude is not None else None,
                "prune": bool(prune),
                "incremental": bool(incremental),
            },
            "counts": {"found": 0, "read": 0, "saved": 0, "embedded": 0},
            "health": None,
            "steps": [{"name": name, "status": "pending"} for name in STEP_NAMES],
        }
        job = _Job(data, db_key)
        with self._lock:
            for other in self._jobs.values():
                if other.db_key == db_key and other.data["status"] in ACTIVE_STATUSES:
                    raise UsageError("an indexing job is already running")
            self._jobs[job_id] = job
            self._trim_history()
            snapshot = copy.deepcopy(data)
        thread = threading.Thread(
            target=self._run,
            args=(job, Path(db_path).expanduser(), root_text, exclude, embed, model_name),
            kwargs={
                "prune": prune,
                "incremental": incremental,
                "refresh_files": bool(refresh_files),
            },
            name=f"onefind-index-{job_id}",
            daemon=True,
        )
        job.thread = thread
        try:
            thread.start()
        except RuntimeError as exc:  # pragma: no cover - interpreter shutting down
            self._finish(job, "failed", error=_describe_error(exc, root_text))
            raise
        return snapshot

    def start_rebuild_vectors(
        self,
        db_path: str | Path,
        *,
        model_name: str | None = None,
        chunking: Any = KEEP_CHUNKING,
        label: str | None = None,
    ) -> dict:
        """Re-split notes into passages and re-embed them in the background.

        Runs :meth:`Index.rebuild_vectors` (see docs/11 section 9) as a job
        with ``kind="rebuild_vectors"`` and the same dict shape and steps as
        an indexing job: ``scan`` (finding notes that need new passages),
        ``index`` (writing passages), ``vectors`` (embedding them; skipped for
        a keyword-only collection) and ``ready``. ``chunking`` first changes
        the collection's passage setting (``"default"``/True to turn passage
        search on, None/``"off"`` to turn it off, or a settings dict); the
        default keeps the current setting. The collection's own model is
        loaded through the shared cache (``model_name`` only matters for a
        collection without one). Cancelling stops between batches; notes
        finished so far keep their new passages and a later run continues.
        Raises :class:`UsageError` when a job is already running for the
        same database or the setting is invalid.
        """
        if db_path is None or not str(db_path).strip():
            raise UsageError("a collection database path is required")
        keep = isinstance(chunking, str) and chunking.strip().lower() == KEEP_CHUNKING
        option = None if keep else normalize_chunking(chunking)
        db_key = _db_key(db_path)
        job_id = uuid.uuid4().hex[:12]
        data = {
            "id": job_id,
            "kind": "rebuild_vectors",
            "label": label,
            "db": str(Path(db_path).expanduser()),
            "root": None,
            "status": "queued",
            "phase": "scan",
            "done": 0,
            "total": None,
            "message": "Waiting to start",
            "started_at": utcnow_iso(),
            "finished_at": None,
            "result": None,
            "error": None,
            "embed": None,
            "model": None,
            "options": {
                "chunking": KEEP_CHUNKING if keep else option,
            },
            "counts": {"found": 0, "read": 0, "saved": 0, "embedded": 0},
            "health": None,
            "steps": [{"name": name, "status": "pending"} for name in STEP_NAMES],
        }
        job = _Job(data, db_key)
        with self._lock:
            for other in self._jobs.values():
                if other.db_key == db_key and other.data["status"] in ACTIVE_STATUSES:
                    raise UsageError("an indexing job is already running")
            self._jobs[job_id] = job
            self._trim_history()
            snapshot = copy.deepcopy(data)
        thread = threading.Thread(
            target=self._run_rebuild,
            args=(job, Path(db_path).expanduser(), model_name, not keep, option),
            name=f"onefind-rebuild-{job_id}",
            daemon=True,
        )
        job.thread = thread
        try:
            thread.start()
        except RuntimeError as exc:  # pragma: no cover - interpreter shutting down
            self._finish(job, "failed", error=_describe_error(exc, str(db_path)))
            raise
        return snapshot

    def get(self, job_id: str) -> dict:
        """A deep copy of the job dict; :class:`JobNotFoundError` if unknown."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise JobNotFoundError(f"no indexing job with id {job_id!r}")
            return copy.deepcopy(job.data)

    def list(self, active_only: bool = False) -> list[dict]:
        """Jobs newest first (deep copies); only queued/running when asked."""
        with self._lock:
            jobs = [
                copy.deepcopy(job.data)
                for job in reversed(self._jobs.values())
                if not active_only or job.data["status"] in ACTIVE_STATUSES
            ]
        return jobs

    def active_for(self, db_path: str | Path) -> dict | None:
        """The queued/running job for ``db_path``, or None."""
        db_key = _db_key(db_path)
        with self._lock:
            for job in reversed(self._jobs.values()):
                if job.db_key == db_key and job.data["status"] in ACTIVE_STATUSES:
                    return copy.deepcopy(job.data)
        return None

    def cancel(self, job_id: str) -> dict:
        """Ask a job to stop; finished jobs are returned unchanged.

        Cancellation is cooperative: the job stops between files/batches, so
        its status turns ``cancelled`` shortly after this call returns.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise JobNotFoundError(f"no indexing job with id {job_id!r}")
            if job.data["status"] in ACTIVE_STATUSES and not job.cancel_event.is_set():
                job.cancel_event.set()
                job.data["message"] = "Cancelling - finishing the current batch"
            return copy.deepcopy(job.data)

    def wait(self, job_id: str, timeout: float | None = None) -> dict:
        """Block until the job finishes (or ``timeout``); returns its dict."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise JobNotFoundError(f"no indexing job with id {job_id!r}")
        job.finished.wait(timeout)
        return self.get(job_id)

    def shutdown(self, timeout: float | None = 5.0) -> None:
        """Cancel every active job and wait up to ``timeout`` seconds each."""
        with self._lock:
            jobs = [job for job in self._jobs.values() if job.data["status"] in ACTIVE_STATUSES]
            for job in jobs:
                job.cancel_event.set()
        for job in jobs:
            job.finished.wait(timeout)

    # -- internals ------------------------------------------------------------------

    def _trim_history(self) -> None:
        """Drop the oldest finished jobs beyond the cap (caller holds the lock)."""
        excess = len(self._jobs) - MAX_JOB_HISTORY
        if excess <= 0:
            return
        for job_id in [
            job_id
            for job_id, job in self._jobs.items()
            if job.data["status"] not in ACTIVE_STATUSES
        ][:excess]:
            del self._jobs[job_id]

    def _update(self, job: _Job, **fields: Any) -> None:
        with self._lock:
            job.data.update(fields)

    def _set_steps(self, job: _Job, **statuses: str) -> None:
        with self._lock:
            for step in job.data["steps"]:
                if step["name"] in statuses:
                    step["status"] = statuses[step["name"]]

    def _finish(
        self,
        job: _Job,
        status: str,
        *,
        message: str | None = None,
        result: dict | None = None,
        error: dict | None = None,
        health: dict | None = None,
    ) -> None:
        with self._lock:
            data = job.data
            data["status"] = status
            data["finished_at"] = utcnow_iso()
            if result is not None:
                data["result"] = result
            if health is not None:
                data["health"] = health
            data["error"] = error
            if status == "succeeded":
                data["phase"] = "done"
                for step in data["steps"]:
                    if step["status"] != "skipped":
                        step["status"] = "done"
            else:
                # failed: the interrupted step turns red; cancelled: it simply
                # did not complete, so it goes back to pending.
                interrupted = "failed" if status == "failed" else "pending"
                for step in data["steps"]:
                    if step["status"] == "active":
                        step["status"] = interrupted
            if message is not None:
                data["message"] = message
            elif error is not None:
                data["message"] = error["message"]
        job.finished.set()

    def _on_progress(self, job: _Job, event: dict) -> None:
        phase = str(event.get("phase") or "")
        done = int(event.get("done") or 0)
        total = event.get("total")
        message = str(event.get("message") or "")
        with self._lock:
            data = job.data
            counts = data["counts"]
            steps = {step["name"]: step for step in data["steps"]}
            if phase == "scan":
                counts["found"] = max(counts["found"], done)
                steps["scan"]["status"] = "active"
            elif phase in {"read", "write", "embed", "prune"}:
                if steps["scan"]["status"] in {"pending", "active"}:
                    steps["scan"]["status"] = "done"
                steps["index"]["status"] = "active"
                if phase == "read":
                    counts["read"] = max(counts["read"], done)
                    if total is not None:
                        counts["found"] = max(counts["found"], int(total))
                elif phase == "write":
                    counts["saved"] = max(counts["saved"], done)
                elif phase == "embed":
                    counts["saved"] = max(counts["saved"], done)
                    counts["embedded"] = max(counts["embedded"], done)
                    if steps["vectors"]["status"] == "pending":
                        steps["vectors"]["status"] = "active"
            elif phase == "done":
                for name in ("scan", "index"):
                    steps[name]["status"] = "done"
                if steps["vectors"]["status"] in {"pending", "active"}:
                    steps["vectors"]["status"] = "done"
                steps["ready"]["status"] = "active"
            if job.cancel_event.is_set():
                message = "Cancelling - finishing the current batch"
            data.update(
                phase=phase or data["phase"],
                done=done,
                total=int(total) if total is not None else None,
                message=message or data["message"],
            )

    def _on_backfill(self, job: _Job, event: dict) -> None:
        """Progress of embedding notes that were indexed without vectors."""
        done = int(event.get("done") or 0)
        total = event.get("total")
        with self._lock:
            data = job.data
            data["counts"]["embedded"] = max(data["counts"]["embedded"], done)
            for step in data["steps"]:
                if step["name"] == "vectors" and step["status"] == "pending":
                    step["status"] = "active"
            suffix = f" ({done} of {total})" if total is not None else f" ({done})"
            data.update(
                phase="embed",
                done=done,
                total=int(total) if total is not None else None,
                message="Building vectors for notes already in this collection" + suffix,
            )

    def _on_rebuild(self, job: _Job, event: dict) -> None:
        """Progress of :meth:`Index.rebuild_vectors` (counts are notes)."""
        phase = str(event.get("phase") or "")
        done = int(event.get("done") or 0)
        total = event.get("total")
        message = str(event.get("message") or "")
        with self._lock:
            data = job.data
            counts = data["counts"]
            steps = {step["name"]: step for step in data["steps"]}
            if phase == "scan":
                counts["found"] = max(counts["found"], int(total or 0))
                steps["scan"]["status"] = "done"
                steps["index"]["status"] = "active"
                if steps["vectors"]["status"] == "pending":
                    steps["vectors"]["status"] = "active"
                message = message or "Looking for notes that need new passages"
            elif phase in {"write", "embed"}:
                counts["read"] = max(counts["read"], done)
                counts["saved"] = max(counts["saved"], done)
                if phase == "embed":
                    counts["embedded"] = max(counts["embedded"], done)
            elif phase == "done":
                for name in ("scan", "index"):
                    steps[name]["status"] = "done"
                if steps["vectors"]["status"] in {"pending", "active"}:
                    steps["vectors"]["status"] = "done"
                steps["ready"]["status"] = "active"
            if job.cancel_event.is_set():
                message = "Cancelling - finishing the current batch"
            data.update(
                phase=phase if phase in {"scan", "write", "embed", "done"} else data["phase"],
                done=done,
                total=int(total) if total is not None else None,
                message=message or data["message"],
            )

    def _run_rebuild(
        self,
        job: _Job,
        db_path: Path,
        model_name: str | None,
        change_chunking: bool,
        chunking: dict | None,
    ) -> None:
        index: Index | None = None
        outcome: tuple[str, dict] | None = None
        try:
            self._update(job, status="running", message="Starting")
            self._set_steps(job, scan="active")
            if job.cancel_event.is_set():
                raise OperationCancelled("rebuilding vectors was cancelled before it started")
            if not db_path.is_file():
                raise DataError(f"collection file not found: {db_path}")
            index = Index.open(db_path)
            if change_chunking:
                index.set_chunking(chunking)
            use_vectors = index.has_vectors()
            self._update(job, embed=use_vectors)
            if use_vectors:
                self._attach(job, index, model_name)
            else:
                self._set_steps(job, vectors="skipped")
            self._update(job, phase="scan", message="Looking for notes that need new passages")
            result = index.rebuild_vectors(
                progress=lambda event: self._on_rebuild(job, event),
                cancel=job.cancel_event.is_set,
            )
            health = index.health()
            rebuilt = int(result.get("rebuilt") or 0)
            summary = (
                f"Vectors rebuilt: {rebuilt} {'note' if rebuilt == 1 else 'notes'} now "
                "searched passage by passage"
                if rebuilt
                else "Vectors are up to date; no note needed new passages"
            )
            outcome = ("succeeded", {"message": summary, "result": result, "health": health})
        except OperationCancelled:
            outcome = (
                "cancelled",
                {
                    "message": "Rebuilding vectors was cancelled. Notes finished so far keep "
                    "their new passages; run it again to finish."
                },
            )
        except BaseException as exc:  # noqa: BLE001 - a job must always settle
            outcome = ("failed", {"error": _describe_error(exc, str(db_path))})
        finally:
            if index is not None:
                try:
                    index.close()
                except Exception:  # noqa: BLE001,S110 - best-effort cleanup
                    pass
            if outcome is None:  # pragma: no cover - defensive
                outcome = (
                    "failed",
                    {
                        "error": _error(
                            "internal_error", "Rebuilding stopped unexpectedly.", retryable=True
                        )
                    },
                )
            self._finish(job, outcome[0], **outcome[1])

    def _resolve_embed(self, index: Index, embed: bool | None) -> tuple[bool, str | None]:
        """Decide whether this run builds vectors; returns (embed, note)."""
        has_vectors = index.has_vectors()
        if embed is None:
            return has_vectors or (index.vec_ready and self.embedders.available()), None
        if not embed and has_vectors:
            return True, (
                "This collection already has meaning-search vectors, so new and "
                "changed notes were embedded too (keeps search consistent)."
            )
        return embed, None

    def _attach(self, job: _Job, index: Index, model_name: str | None) -> None:
        name = model_name or index.get_meta("model_name") or DEFAULT_MODEL
        cached = self.embedders.loaded(name)
        self._update(
            job,
            phase="model",
            done=0,
            total=None,
            model=name,
            message=(
                "Preparing the meaning-search model"
                if cached
                else "Loading the meaning-search model (the first time can take a minute)"
            ),
        )
        try:
            embedder = self.embedders.get(name)
        except Exception as exc:  # noqa: BLE001 - any load failure means "unavailable"
            raise _ModelUnavailable(str(exc).strip() or type(exc).__name__) from exc
        if job.cancel_event.is_set():
            raise OperationCancelled("indexing was cancelled before it started")
        if not index.has_vectors() and index.count("documents"):
            self._update(job, message="Building vectors for notes already in this collection")
        try:
            index.attach_embedder(
                embedder,
                progress=lambda event: self._on_backfill(job, event),
                cancel=job.cancel_event.is_set,
            )
        except (DataError, EnvError) as exc:
            raise _AttachFailed(str(exc)) from exc

    def _run(
        self,
        job: _Job,
        db_path: Path,
        root: str,
        exclude: list[str] | None,
        embed: bool | None,
        model_name: str | None,
        *,
        prune: bool,
        incremental: bool,
        refresh_files: bool = False,
    ) -> None:
        from .ingest import ingest_path

        index: Index | None = None
        outcome: tuple[str, dict] | None = None
        try:
            self._update(job, status="running", message="Starting")
            self._set_steps(job, scan="active")
            if job.cancel_event.is_set():
                raise OperationCancelled("indexing was cancelled before it started")
            index = Index.open(db_path)
            use_vectors, note = self._resolve_embed(index, embed)
            self._update(job, embed=use_vectors)
            if use_vectors:
                self._attach(job, index, model_name)
            else:
                self._set_steps(job, vectors="skipped")
            self._update(job, phase="scan", message="Looking for notes")

            result = ingest_path(
                index,
                root,
                exclude=exclude,
                prune=prune,
                incremental=incremental,
                progress=lambda event: self._on_progress(job, event),
                cancel=job.cancel_event.is_set,
                refresh_files=refresh_files,
            )
            health = index.health()
            with self._lock:
                summary = job.data["message"]
            if not summary.startswith("Indexing finished"):
                summary = "Indexing finished"
            if note:
                summary = f"{summary}. {note}"
            outcome = ("succeeded", {"message": summary, "result": result, "health": health})
        except OperationCancelled:
            outcome = (
                "cancelled",
                {"message": "Indexing was cancelled. Notes saved before cancelling are kept."},
            )
        except BaseException as exc:  # noqa: BLE001 - a job must always settle
            outcome = ("failed", {"error": _describe_error(exc, root)})
        finally:
            # Close the job's connection *before* the job turns final, so a
            # caller that waits for the job can delete or replace the file.
            if index is not None:
                try:
                    index.close()
                except Exception:  # noqa: BLE001,S110 - best-effort cleanup
                    pass
            if outcome is None:  # pragma: no cover - defensive
                outcome = (
                    "failed",
                    {
                        "error": _error(
                            "internal_error", "Indexing stopped unexpectedly.", retryable=True
                        )
                    },
                )
            self._finish(job, outcome[0], **outcome[1])
