"""Auto-sync: keep a collection's index up to date when note files change.

No file-system notification library is used (OneFind adds no runtime
dependencies). Instead each watched collection gets one daemon thread that
polls its registered folders with a cheap *stat-only* walk (path -> mtime and
size; nothing is read or hashed) honoring ``DEFAULT_EXCLUDES`` and each
folder's saved exclude patterns. When a folder's snapshot changes, the
watcher waits until it is stable for :data:`DEBOUNCE_POLLS` consecutive
polls (so a note that is still being written is never indexed half-saved),
asks :func:`~onefind.ingest.scan_changes` whether the index really differs
(a touched but unchanged file does not count), and then runs an ordinary
incremental, pruning :class:`~onefind.jobs.JobManager` index job labelled
:data:`AUTO_SYNC_LABEL` for that folder.

Gentle by design:

* default poll interval :data:`DEFAULT_INTERVAL` seconds; the interval grows
  with the time one walk takes (``CPU_FACTOR``), so huge trees are polled
  rarely and CPU stays low;
* folders on another computer (UNC paths, mapped network drives) are never
  polled unless explicitly allowed;
* nothing runs while another (manual) job is active for the collection, and
  :meth:`AutoSync.preempt` lets a manual action cancel an in-flight
  auto-sync job and keep the watcher quiet for a moment;
* a folder that is missing, unreadable, or suddenly has no notes at all is
  left alone (its notes are kept) instead of being pruned;
* errors put the watcher in state ``error`` and it retries with exponential
  backoff; it stops by itself when the collection file disappears.

The per-collection on/off preference lives in the collection's own
``schema_meta`` (key :data:`AUTO_SYNC_META_KEY`, ``"on"``/``"off"``), so it
travels with the portable ``.db`` file. See docs/11-implementation-contract.md
section 10.
"""

from __future__ import annotations

import contextlib
import copy
import itertools
import logging
import os
import stat as stat_module
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import DataError, OneFindError, UsageError
from .ingest import (
    DEFAULT_EXCLUDES,
    SUPPORTED_SUFFIXES,
    is_excluded,
    normalize_excludes,
    scan_changes,
)
from .store import Index, utcnow_iso

__all__ = [
    "AUTO_SYNC_LABEL",
    "AUTO_SYNC_META_KEY",
    "AutoSync",
    "DEFAULT_INTERVAL",
    "STATES",
    "default_interval",
    "diff_snapshots",
    "get_preference",
    "is_network_root",
    "set_preference",
    "snapshot_root",
]

logger = logging.getLogger("onefind.watcher")

AUTO_SYNC_META_KEY = "auto_sync"
AUTO_SYNC_LABEL = "Auto-sync"
STATES = ("idle", "scanning", "syncing", "paused", "error")

DEFAULT_INTERVAL = 5.0
MIN_INTERVAL = 0.02
MAX_INTERVAL = 120.0
MAX_BACKOFF = 300.0
# The next poll waits at least CPU_FACTOR times as long as the last walk took,
# so polling costs at most ~2.5% of one core however large the folders are.
CPU_FACTOR = 40.0
DEBOUNCE_POLLS = 2
# A folder that suddenly has no notes while the index holds at least this many
# of them is treated as "temporarily unavailable" (sync client, unplugged
# disk) instead of pruning every note.
VANISH_GUARD = 5
# How long a manual action keeps the watcher from starting a new job.
DEFAULT_COOLDOWN = 10.0
INTERVAL_ENV = "ONEFIND_AUTOSYNC_INTERVAL"

Snapshot = dict[str, tuple[int, int]]
EventCallback = Callable[[dict], None]

_thread_numbers = itertools.count(1)


# -- preference ---------------------------------------------------------------------


def get_preference(index: Index) -> bool:
    """True when the collection's saved auto-sync preference is on (default off)."""
    value = index.get_meta(AUTO_SYNC_META_KEY)
    return str(value or "").strip().lower() == "on"


def set_preference(index: Index, enabled: bool) -> None:
    """Save the auto-sync preference in the collection file (``schema_meta``)."""
    index.set_meta(AUTO_SYNC_META_KEY, "on" if enabled else "off")


def default_interval() -> float:
    """``$ONEFIND_AUTOSYNC_INTERVAL`` (seconds) when set and valid, else 5 s."""
    raw = os.environ.get(INTERVAL_ENV, "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            logger.warning("ignoring invalid %s=%r", INTERVAL_ENV, raw)
        else:
            if value > 0:
                return max(MIN_INTERVAL, value)
    return DEFAULT_INTERVAL


def _check_interval(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise UsageError("the auto-sync interval must be a number of seconds") from exc
    if not number > 0:
        raise UsageError("the auto-sync interval must be greater than 0 seconds")
    return max(MIN_INTERVAL, number)


# -- network detection ---------------------------------------------------------------


def _drive_is_remote(path: str) -> bool:
    """True for a Windows drive letter mapped to a network share (no I/O)."""
    if os.name != "nt" or len(path) < 2 or path[1] != ":":
        return False
    try:
        import ctypes

        drive_type = ctypes.windll.kernel32.GetDriveTypeW(f"{path[0]}:\\")  # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError):  # pragma: no cover - not Windows
        return False
    return drive_type == 4  # DRIVE_REMOTE


def is_network_root(path: str | Path | None) -> bool:
    r"""True for UNC/device paths (``\\host\share``) and mapped network drives.

    Polling such a folder makes the computer contact another machine every
    few seconds, so auto-sync skips it unless explicitly allowed.
    """
    if path is None:
        return False
    text = str(path).strip().replace("/", "\\")
    if text.startswith("\\\\"):
        return True
    return _drive_is_remote(text)


# -- stat-only snapshots ---------------------------------------------------------------


def snapshot_root(
    root: str | Path,
    exclude: Iterable[str] | str | None = None,
    *,
    sources: Iterable[str] | None = None,
) -> tuple[Snapshot, bool]:
    """Stat every supported, non-excluded file under ``root`` (nothing is read).

    Returns ``(snapshot, readable)`` where ``snapshot`` maps the relative
    posix path to ``(mtime_ns, size)`` and ``readable`` is False when the
    folder itself is missing or cannot be listed. Excluded folders are never
    descended into (same rules as :func:`onefind.ingest.list_files`).
    ``sources`` limits the snapshot to those relative paths (folders that
    were registered by indexing single files).
    """
    base = Path(root)
    snapshot: Snapshot = {}
    if sources is not None:
        if not base.is_dir():
            return snapshot, False
        for relative in sources:
            try:
                info = os.stat(base.joinpath(*PurePosixPath(relative).parts))
            except (OSError, ValueError):
                continue
            if stat_module.S_ISREG(info.st_mode):
                snapshot[relative] = (info.st_mtime_ns, info.st_size)
        return snapshot, True

    patterns = [*DEFAULT_EXCLUDES, *normalize_excludes(exclude)]
    pending: list[tuple[str, str]] = [("", str(base))]
    first = True
    while pending:
        rel_dir, folder = pending.pop()
        try:
            with os.scandir(folder) as iterator:
                entries = list(iterator)
        except OSError:
            if first:
                return {}, False
            continue  # an unreadable sub-folder: scan_changes/ingest handle it safely
        first = False
        for entry in entries:
            name = entry.name
            relative = f"{rel_dir}/{name}" if rel_dir else name
            try:
                if entry.is_dir(follow_symlinks=False):
                    if not is_excluded(relative, patterns):
                        pending.append((relative, entry.path))
                    continue
                if os.path.splitext(name)[1].lower() not in SUPPORTED_SUFFIXES:
                    continue
                if is_excluded(relative, patterns) or not entry.is_file():
                    continue
                info = entry.stat()
            except OSError:
                continue
            snapshot[relative] = (info.st_mtime_ns, info.st_size)
    return snapshot, True


def diff_snapshots(old: Snapshot | None, new: Snapshot) -> dict[str, int]:
    """Count new / changed / deleted paths between two stat snapshots."""
    if old is None:
        return {"new": 0, "changed": 0, "deleted": 0}
    added = sum(1 for path in new if path not in old)
    deleted = sum(1 for path in old if path not in new)
    changed = sum(1 for path, value in new.items() if path in old and old[path] != value)
    return {"new": added, "changed": changed, "deleted": deleted}


def _db_key(db_path: str | Path) -> str:
    return os.path.normcase(str(Path(db_path).expanduser().absolute()))


def _zero_pending() -> dict[str, int]:
    return {"new": 0, "changed": 0, "deleted": 0}


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _folder_name(path: str) -> str:
    return Path(path).name or path


def _blank_status(interval: float) -> dict:
    return {
        "enabled": False,
        "state": "idle",
        "last_scan_at": None,
        "last_sync_at": None,
        "pending": _zero_pending(),
        "message": "",
        "interval_s": round(interval, 2),
        "syncs": 0,
        "last_changes": None,
        "progress": None,
        "skipped_roots": 0,
    }


# -- one watched collection -------------------------------------------------------------


class _Watcher:
    """Polling loop for one collection database (internal)."""

    def __init__(
        self,
        owner: "AutoSync",
        db_path: Path,
        roots: list[dict] | None,
        *,
        interval: float,
        allow_network: bool,
    ) -> None:
        self.owner = owner
        self.db = db_path
        self.key = _db_key(db_path)
        self.fixed_roots = roots
        self.base_interval = interval
        self.interval = interval
        self.allow_network = allow_network
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.lock = threading.Lock()  # guards status
        self.gate = threading.Lock()  # held while deciding to start a job
        self.cooldown_until = 0.0
        self.job_id: str | None = None
        self.failures = 0
        self.manual_seen = False
        self.previous: dict[str, Snapshot] = {}
        self.stable: dict[str, int] = {}
        self.synced: dict[str, Snapshot | None] = {}
        self.status = _blank_status(interval)
        self.status["enabled"] = True
        self.thread = threading.Thread(
            target=self._run,
            name=f"onefind-autosync-{next(_thread_numbers)}",
            daemon=True,
        )

    # -- status & events -----------------------------------------------------------

    def _set(self, **fields: Any) -> None:
        with self.lock:
            self.status.update(fields)

    def _emit(self, kind: str, **fields: Any) -> None:
        callback = self.owner.on_event
        if callback is None:
            return
        try:
            callback({"type": kind, "db": str(self.db), **fields})
        except Exception:  # noqa: BLE001 - a broken listener must not stop syncing
            logger.debug("auto-sync event listener failed", exc_info=True)

    def _transition(self, state: str, message: str = "", *, event: str | None = None) -> None:
        """Set state + message; emit ``event`` only when either really changed."""
        with self.lock:
            changed = self.status["state"] != state or self.status["message"] != message
            self.status["state"] = state
            self.status["message"] = message
        if changed and event:
            self._emit(event, state=state, message=message)

    def snapshot_status(self) -> dict:
        with self.lock:
            status = copy.deepcopy(self.status)
            job_id = self.job_id
        if job_id is not None:
            try:
                job = self.owner.jobs.get(job_id)
            except OneFindError:
                job = None
            if job is not None:
                total = job.get("total")
                done = job.get("done") or 0
                if job.get("phase") in {"read", "write", "embed"} and total:
                    status["progress"] = max(0, min(100, int(100 * int(done) / int(total))))
        return status

    # -- loop ------------------------------------------------------------------------

    def _run(self) -> None:
        self._emit("started", interval_s=self.interval)
        try:
            while not self.stop_event.is_set():
                try:
                    delay = self._cycle()
                except Exception as exc:  # noqa: BLE001 - the watcher must survive anything
                    logger.warning("auto-sync poll failed", exc_info=True)
                    self.job_id = None
                    delay = self._fail(f"Auto-sync ran into a problem ({type(exc).__name__}).")
                if self.stop_event.is_set():
                    break
                self.wake_event.wait(max(MIN_INTERVAL, delay))
                self.wake_event.clear()
        finally:
            self._cancel_job()
            with self.lock:
                self.status["enabled"] = False
                if self.status["state"] in {"scanning", "syncing"}:
                    self.status["state"] = "idle"
                self.status["pending"] = _zero_pending()
                self.status["progress"] = None
            self._emit("stopped", message=self.status["message"])

    def _cancel_job(self) -> None:
        job_id = self.job_id
        if job_id is None:
            return
        jobs = self.owner.jobs
        with contextlib.suppress(OneFindError):
            jobs.cancel(job_id)
            jobs.wait(job_id, timeout=self.owner.stop_timeout)
        self.job_id = None

    def _backoff(self) -> float:
        return min(MAX_BACKOFF, max(self.base_interval, self.interval) * (2 ** min(self.failures, 10)))

    def _fail(self, message: str) -> float:
        self.failures += 1
        delay = self._backoff()
        with self.lock:
            self.status["progress"] = None
        self._transition(
            "error",
            f"{message} Auto-sync will try again in {delay:.0f} s." if delay >= 1 else message,
            event="error",
        )
        return delay

    def _open_index(self) -> Index:
        try:
            return Index.open(self.db, readonly=True)
        except DataError:
            # e.g. an older schema that must be upgraded first; a normal open does it.
            return Index.open(self.db)

    def _load_roots(self, index: Index) -> list[dict]:
        roots: list[dict] = []
        registered = {root["path"]: root for root in index.roots()}
        if self.fixed_roots is not None:
            wanted = self.fixed_roots
        else:
            wanted = list(registered.values())
        for item in wanted:
            path = str(item.get("path") or "").strip()
            if not path:
                continue
            stored = registered.get(path) or {}
            root = {
                "root_id": item.get("root_id", stored.get("root_id")),
                "path": path,
                "exclude": item.get("exclude") if self.fixed_roots is not None else None,
                "patterns": list(
                    item.get("exclude")
                    if item.get("exclude") is not None
                    else stored.get("exclude") or []
                ),
                "documents": int(stored.get("documents") or 0),
                "files_only": bool(stored.get("files_only")),
                "allow_network": bool(item.get("allow_network", False)),
                "sources": None,
            }
            if root["files_only"] and root["root_id"] is not None:
                manifest = index.document_manifest(root["root_id"])
                root["sources"] = sorted(
                    str(meta.get("source") or doc_id) for doc_id, meta in manifest.items()
                )
            roots.append(root)
        return roots

    def _cycle(self) -> float:
        """One poll; returns the delay before the next one."""
        if not self.db.is_file():
            self._transition(
                "idle",
                "The collection file is gone, so auto-sync stopped.",
                event="paused",
            )
            self.stop_event.set()
            return 0.0
        now = time.monotonic()
        if now < self.cooldown_until:
            self.manual_seen = True  # a manual action may change the index meanwhile
            self._transition("paused", "Paused while OneFind works on this collection.", event="paused")
            return min(self.interval, self.cooldown_until - now + MIN_INTERVAL)
        active = self.owner.jobs.active_for(self.db)
        if active is not None:
            self.manual_seen = True
            self._transition("paused", "Waiting for indexing to finish.", event="paused")
            return min(self.interval, 1.0)
        if self.manual_seen:
            # The index changed under us: compare again against it.
            self.manual_seen = False
            self.synced = {path: None for path in self.synced}

        try:
            with self._open_index() as index:
                roots = self._load_roots(index)
                has_vectors = index.has_vectors()
        except Exception as exc:  # noqa: BLE001 - keep watching, report plainly
            logger.info("auto-sync could not read %s", self.db, exc_info=True)
            return self._fail(f"The collection could not be read ({exc}).")

        started = time.perf_counter()
        notes: list[str] = []
        skipped_network = 0
        ready: list[tuple[dict, Snapshot]] = []
        pending = _zero_pending()
        current_paths = set()
        for root in roots:
            path = root["path"]
            current_paths.add(path)
            if is_network_root(path) and not (self.allow_network or root["allow_network"]):
                skipped_network += 1
                continue
            snapshot, readable = snapshot_root(path, root["patterns"], sources=root["sources"])
            if not readable:
                notes.append(
                    f"“{_folder_name(path)}” is not available right now; its notes are kept."
                )
                self.previous.pop(path, None)
                self.stable.pop(path, None)
                continue
            if not snapshot and root["documents"] >= VANISH_GUARD and root["sources"] is None:
                notes.append(
                    f"Every note in “{_folder_name(path)}” seems to be gone, so auto-sync left "
                    "the index alone. Update the index from Manage if this was intended."
                )
                continue
            if self.previous.get(path) != snapshot:
                self.previous[path] = snapshot
                self.stable[path] = 0
            else:
                self.stable[path] = self.stable.get(path, 0) + 1
            synced = self.synced.get(path)
            if synced is not None and synced == snapshot:
                continue
            if self.stable[path] >= DEBOUNCE_POLLS - 1:
                ready.append((root, snapshot))
            else:
                for key, value in diff_snapshots(synced, snapshot).items():
                    pending[key] += value
        for gone in set(self.previous) - current_paths:
            self.previous.pop(gone, None)
            self.stable.pop(gone, None)
            self.synced.pop(gone, None)
        elapsed = time.perf_counter() - started
        self.interval = min(
            max(self.base_interval, elapsed * CPU_FACTOR),
            max(self.base_interval, self.owner.max_interval),
        )
        with self.lock:
            self.status.update(
                last_scan_at=utcnow_iso(),
                interval_s=round(self.interval, 2),
                skipped_roots=skipped_network,
                pending=dict(pending),
            )
        if skipped_network:
            notes.append(
                f"Skipping {_plural(skipped_network, 'network folder')}: auto-sync never "
                "contacts other computers on its own."
            )
        if self.interval > self.base_interval * 1.5 and self.interval >= 10:
            notes.append(f"Large folders: checking every {self.interval:.0f} s to keep the computer quiet.")

        for root, snapshot in ready:
            if self.stop_event.is_set():
                return 0.0
            outcome = self._sync_root(root, snapshot, has_vectors)
            if outcome is not None:
                return outcome  # paused or failed: try again later
        self.failures = 0
        with self.lock:
            self.status["pending"] = dict(pending)
            self.status["progress"] = None
        if any(pending.values()):
            # Some folder is still changing: index it once it has settled.
            self._transition("scanning", "Changes found; waiting for files to finish saving.")
            return self.interval
        self._transition("idle", " ".join(notes))
        return self.interval

    def _sync_root(self, root: dict, snapshot: Snapshot, has_vectors: bool) -> float | None:
        """Scan one settled folder and run a job when the index differs."""
        path = root["path"]
        self._transition("scanning", f"Checking “{_folder_name(path)}” for changes.")
        try:
            with self._open_index() as index:
                report = scan_changes(index, path, exclude=root["exclude"])
        except Exception as exc:  # noqa: BLE001
            logger.info("auto-sync scan failed for %s", path, exc_info=True)
            return self._fail(f"Could not check “{_folder_name(path)}” for changes ({exc}).")
        if report.get("exists") is False:
            self.synced[path] = snapshot
            return None
        counts = {key: int(report.get(key) or 0) for key in ("new", "changed", "deleted")}
        if not any(counts.values()):
            self.synced[path] = snapshot
            return None

        with self.lock:
            self.status["pending"] = dict(counts)
        self._emit("changes", root=path, **counts)
        with self.gate:
            if self.stop_event.is_set():
                return 0.0
            if time.monotonic() < self.cooldown_until:
                self._transition("paused", "Paused while OneFind works on this collection.", event="paused")
                return self.interval
            options: dict[str, Any] = {
                "exclude": root["exclude"],
                "embed": bool(has_vectors),
                "prune": True,
                "incremental": True,
                "label": AUTO_SYNC_LABEL,
            }
            if root["files_only"]:
                options["refresh_files"] = True
            try:
                job = self.owner.jobs.start_index(self.db, path, **options)
            except UsageError as exc:
                if "already running" in str(exc):
                    self.manual_seen = True
                    self._transition("paused", "Waiting for indexing to finish.", event="paused")
                    return min(self.interval, 1.0)
                return self._fail(str(exc))
            self.job_id = job["id"]
        total = sum(counts.values())
        self._transition("syncing", f"Syncing {_plural(total, 'change')} in “{_folder_name(path)}”.")
        self._emit("sync_started", root=path, job_id=job["id"], **counts)
        began = time.perf_counter()
        final = self._wait_job(job["id"])
        self.job_id = None
        if final is None:  # stopping
            return 0.0
        status = final.get("status")
        if status == "succeeded":
            result = final.get("result") or {}
            changes = {key: int(result.get(key) or 0) for key in ("added", "updated", "removed")}
            self.synced[path] = snapshot
            self.failures = 0
            with self.lock:
                self.status["last_sync_at"] = utcnow_iso()
                self.status["last_changes"] = dict(changes)
                self.status["progress"] = None
                if any(changes.values()):
                    self.status["syncs"] += 1
            self._emit(
                "synced",
                root=path,
                seconds=round(time.perf_counter() - began, 2),
                **changes,
            )
            return None
        if status == "cancelled":
            # A manual action took over (preempt) or we are stopping.
            self._transition("paused", "Paused while OneFind works on this collection.", event="paused")
            return self.interval
        error = final.get("error") or {}
        return self._fail(
            str(error.get("message") or final.get("message") or "Auto-sync could not update the index.")
        )

    def _wait_job(self, job_id: str) -> dict | None:
        jobs = self.owner.jobs
        while True:
            try:
                job = jobs.wait(job_id, timeout=0.25)
            except OneFindError:
                return {"status": "failed", "error": {"message": "The sync job was lost."}}
            if job.get("status") in {"succeeded", "failed", "cancelled"}:
                return job
            if self.stop_event.is_set():
                self._cancel_job()
                return None


# -- public manager ----------------------------------------------------------------------


class AutoSync:
    """Runs at most one auto-sync watcher thread per collection database.

    ``job_manager`` is the :class:`~onefind.jobs.JobManager` whose index jobs
    do the actual work (so the web app's job tracking, cancellation and
    one-job-per-collection rule apply unchanged). ``interval`` is the base
    poll interval in seconds (default :func:`default_interval`).
    ``on_event(event)`` receives small dicts (``type`` is one of
    ``started``, ``changes``, ``sync_started``, ``synced``, ``paused``,
    ``error``, ``stopped``) from the watcher threads; it must be quick.
    All methods are thread-safe.
    """

    def __init__(
        self,
        job_manager: Any,
        *,
        interval: float | None = None,
        max_interval: float = MAX_INTERVAL,
        cooldown: float = DEFAULT_COOLDOWN,
        allow_network: bool = False,
        on_event: EventCallback | None = None,
        stop_timeout: float = 10.0,
    ) -> None:
        self.jobs = job_manager
        self.interval = _check_interval(interval) or default_interval()
        self.max_interval = float(max_interval)
        self.cooldown = float(cooldown)
        self.allow_network = bool(allow_network)
        self.on_event = on_event
        self.stop_timeout = float(stop_timeout)
        self._lock = threading.Lock()
        self._watchers: dict[str, _Watcher] = {}
        self._closed = False

    # -- lifecycle -----------------------------------------------------------------

    def start(
        self,
        db_path: str | Path,
        roots: list[dict] | None = None,
        *,
        interval: float | None = None,
        allow_network: bool | None = None,
    ) -> dict:
        """Watch ``db_path`` (idempotent while a watcher is running).

        ``roots`` is a list of ``{"root_id", "path", "exclude"}`` dicts; when
        omitted, the collection's registered folders are re-read on every
        poll (so folders added later are picked up). Returns the status.
        """
        if db_path is None or not str(db_path).strip():
            raise UsageError("a collection database path is required")
        target = Path(db_path).expanduser().absolute()
        if not target.is_file():
            raise DataError(f"no collection file at {target}")
        chosen = _check_interval(interval) or self.interval
        fixed = None
        if roots is not None:
            fixed = []
            for item in roots:
                if not isinstance(item, dict) or not str(item.get("path") or "").strip():
                    raise UsageError("each auto-sync root needs a 'path'")
                fixed.append(dict(item))
        key = _db_key(target)
        stale: _Watcher | None = None
        with self._lock:
            if self._closed:
                raise UsageError("auto-sync has been shut down")
            current = self._watchers.get(key)
            if current is not None and current.thread.is_alive() and not current.stop_event.is_set():
                if fixed is not None:
                    current.fixed_roots = fixed
                current.wake_event.set()
                return current.snapshot_status()
            stale = current
            watcher = _Watcher(
                self,
                target,
                fixed,
                interval=chosen,
                allow_network=self.allow_network if allow_network is None else bool(allow_network),
            )
            self._watchers[key] = watcher
            watcher.thread.start()
        if stale is not None:
            stale.thread.join(self.stop_timeout)
        return watcher.snapshot_status()

    def stop(self, db_path: str | Path, timeout: float | None = None) -> dict:
        """Stop watching ``db_path`` (cancels an in-flight auto-sync job)."""
        key = _db_key(db_path)
        with self._lock:
            watcher = self._watchers.pop(key, None)
        if watcher is None:
            return _blank_status(self.interval)
        watcher.stop_event.set()
        watcher.wake_event.set()
        if watcher.thread is not threading.current_thread():
            watcher.thread.join(self.stop_timeout if timeout is None else timeout)
        status = watcher.snapshot_status()
        status["enabled"] = False
        return status

    def shutdown(self, timeout: float | None = None) -> None:
        """Stop every watcher; no new ones can start afterwards."""
        with self._lock:
            self._closed = True
            watchers = list(self._watchers.values())
            self._watchers.clear()
        for watcher in watchers:
            watcher.stop_event.set()
            watcher.wake_event.set()
        for watcher in watchers:
            watcher.thread.join(self.stop_timeout if timeout is None else timeout)

    def reopen(self) -> None:
        """Allow :meth:`start` again after :meth:`shutdown` (tests, app restarts)."""
        with self._lock:
            self._closed = False

    # -- queries ---------------------------------------------------------------------

    def blank_status(self) -> dict:
        """The status of a collection that is not being watched."""
        return _blank_status(self.interval)

    def running(self, db_path: str | Path) -> bool:
        """True while a watcher thread for ``db_path`` is alive."""
        with self._lock:
            watcher = self._watchers.get(_db_key(db_path))
        return bool(watcher and watcher.thread.is_alive() and not watcher.stop_event.is_set())

    def watched(self) -> list[str]:
        """Database paths with a live watcher."""
        with self._lock:
            return [
                str(watcher.db)
                for watcher in self._watchers.values()
                if watcher.thread.is_alive() and not watcher.stop_event.is_set()
            ]

    def status(self, db_path: str | Path) -> dict:
        """``{"enabled", "state", "last_scan_at", "last_sync_at", "pending",
        "message", "interval_s", ...}`` for ``db_path`` (never contains paths
        of the database)."""
        with self._lock:
            watcher = self._watchers.get(_db_key(db_path))
        if watcher is None:
            return _blank_status(self.interval)
        status = watcher.snapshot_status()
        if not watcher.thread.is_alive():
            status["enabled"] = False
        return status

    def poke(self, db_path: str | Path) -> None:
        """Poll ``db_path`` now instead of waiting for the next interval."""
        with self._lock:
            watcher = self._watchers.get(_db_key(db_path))
        if watcher is not None:
            watcher.wake_event.set()

    def preempt(
        self,
        db_path: str | Path,
        *,
        timeout: float = 15.0,
        cooldown: float | None = None,
    ) -> bool:
        """Make way for a manual action on ``db_path``.

        Keeps the watcher from starting a job for ``cooldown`` seconds, then
        cancels an auto-sync job that is running for the collection and waits
        up to ``timeout`` for it to stop (finished batches stay committed).
        Returns True when no auto-sync job is running afterwards.
        """
        key = _db_key(db_path)
        with self._lock:
            watcher = self._watchers.get(key)
        if watcher is not None:
            span = max(self.cooldown if cooldown is None else float(cooldown), 2 * watcher.interval)
            # Taking the gate means a job the watcher was about to start is
            # either visible below or will not start at all.
            with watcher.gate:
                watcher.cooldown_until = max(watcher.cooldown_until, time.monotonic() + span)
        active = self.jobs.active_for(db_path)
        if active is None or active.get("label") != AUTO_SYNC_LABEL:
            return True
        with contextlib.suppress(OneFindError):
            self.jobs.cancel(active["id"])
            self.jobs.wait(active["id"], timeout=timeout)
        again = self.jobs.active_for(db_path)
        return again is None or again.get("label") != AUTO_SYNC_LABEL
