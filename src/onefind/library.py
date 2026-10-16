"""Named collections registry (docs/11-implementation-contract.md section 4).

A *collection* is one OneFind SQLite file with a friendly name. The registry
lives in ``<home>/collections.json``::

    {"version": 1, "active": "Notes" | null,
     "collections": [{"name": "Notes", "db": "C:/.../notes.db",
                      "created_at": "2026-10-13T09:00:00+00:00",
                      "slug": "notes"}]}

The file is rewritten atomically (temp file in the same directory, then
``os.replace``) and re-read at the start of every operation, so a CLI and a
running server that share one home see each other's changes. A registry that
cannot be parsed is never deleted: it is moved aside to
``collections.json.corrupt-<timestamp>`` and the library starts empty.

``slug`` is an additive field: it is unique within the registry, stays stable
across renames, and names both the default database file and the backups.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any

from .errors import DataError, UsageError

REGISTRY_FILENAME = "collections.json"
REGISTRY_VERSION = 1
NAME_MAX_LENGTH = 64
SLUG_MAX_LENGTH = 48
DEFAULT_SLUG = "collection"

_SLUG_INVALID = re.compile(r"[^a-z0-9]+")
_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"
_BACKUP_NAME = re.compile(
    r"^(?P<slug>[a-z0-9-]+)-(?P<stamp>\d{8}-\d{6})(?:-(?P<seq>\d+))?\.db$"
)
# One lock for every Library instance in the process: a server may build a
# fresh Library per request while a background job holds another.
_LOCK = threading.RLock()
# Depth of the cross-process registry lock held by this thread (re-entrant).
_LOCK_DEPTH = threading.local()
LOCK_TIMEOUT = 30.0


def _lock_file(handle) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


@contextlib.contextmanager
def _registry_lock(home: Path, *, create: bool = False):
    """Serialize registry read-modify-write across threads *and* processes.

    A CLI and a running server share one home; without a file lock their
    load -> modify -> replace cycles interleave and one side's change (e.g. a
    newly created collection) is silently lost. Re-entrant per thread. A
    reader of a home that does not exist yet takes only the in-process lock,
    so reading never creates the folder.
    """
    with _LOCK:
        depth = getattr(_LOCK_DEPTH, "n", 0)
        if not depth and not create and not home.is_dir():
            yield
            return
        if depth:
            _LOCK_DEPTH.n = depth + 1
            try:
                yield
            finally:
                _LOCK_DEPTH.n = depth
            return
        try:
            home.mkdir(parents=True, exist_ok=True)
            handle = open(home / f"{REGISTRY_FILENAME}.lock", "a+b")  # noqa: SIM115
        except OSError as exc:
            raise DataError(f"cannot lock {home / REGISTRY_FILENAME}: {exc}") from exc
        try:
            deadline = time.monotonic() + LOCK_TIMEOUT
            while True:
                try:
                    _lock_file(handle)
                    break
                except OSError as exc:
                    if time.monotonic() > deadline:
                        raise DataError(
                            f"the collections list in {home} is locked by another "
                            "OneFind process; try again"
                        ) from exc
                    time.sleep(0.02)
            _LOCK_DEPTH.n = 1
            try:
                yield
            finally:
                _LOCK_DEPTH.n = 0
                _unlock_file(handle)
        finally:
            handle.close()


class NameTakenError(UsageError):
    """A collection with this name (compared case-insensitively) exists."""


class AlreadyRegisteredError(UsageError):
    """The database file is already registered under another name."""

    def __init__(self, message: str, existing: str) -> None:
        super().__init__(message)
        self.existing = existing


def _now() -> datetime.datetime:
    """Local wall-clock time; isolated so tests can freeze it."""
    return datetime.datetime.now()


def _utcnow_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def default_home() -> Path:
    """Return the per-user OneFind data directory.

    ``$ONEFIND_HOME`` wins when set; otherwise ``%APPDATA%\\OneFind`` on
    Windows, ``~/Library/Application Support/OneFind`` on macOS, and
    ``$XDG_DATA_HOME/onefind`` (or ``~/.local/share/onefind``) elsewhere.
    """
    override = os.environ.get("ONEFIND_HOME", "").strip()
    if override:
        return Path(override).expanduser().absolute()
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "").strip()
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return (base / "OneFind").absolute()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "OneFind"
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    # The XDG spec says relative values must be ignored.
    if xdg and Path(xdg).is_absolute():
        return Path(xdg) / "onefind"
    return Path.home() / ".local" / "share" / "onefind"


def validate_name(name: object) -> str:
    """Return the trimmed collection name or raise UsageError."""
    if not isinstance(name, str):
        raise UsageError("collection name must be text")
    cleaned = name.strip()
    if not cleaned:
        raise UsageError("collection name cannot be empty")
    if len(cleaned) > NAME_MAX_LENGTH:
        raise UsageError(
            f"collection name is too long ({len(cleaned)} characters; "
            f"the limit is {NAME_MAX_LENGTH})"
        )
    if any(unicodedata.category(ch).startswith("C") for ch in cleaned):
        raise UsageError("collection name cannot contain control characters")
    if not any(ch.isalnum() for ch in cleaned):
        raise UsageError("collection name must contain at least one letter or digit")
    return cleaned


def slugify(name: str) -> str:
    """Lowercase ``[a-z0-9-]`` form of ``name``; never empty."""
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_INVALID.sub("-", folded.lower()).strip("-")
    slug = slug[:SLUG_MAX_LENGTH].strip("-")
    return slug or DEFAULT_SLUG


def _key(name: str) -> str:
    return name.strip().casefold()


def _path_key(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def _clean_slug(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    slug = _SLUG_INVALID.sub("-", value.lower()).strip("-")[:SLUG_MAX_LENGTH].strip("-")
    return slug or None


class Library:
    """Registry of named OneFind collections stored under ``home``."""

    def __init__(self, home: str | Path | None = None) -> None:
        base = default_home() if home is None else Path(home).expanduser()
        self.home: Path = base.absolute()
        self.collections_dir: Path = self.home / "collections"
        self.backups_dir: Path = self.home / "backups"
        self.registry_path: Path = self.home / REGISTRY_FILENAME

    # -- queries -------------------------------------------------------------

    def list(self) -> list[dict]:
        """All collections in registry order, each with ``exists``/``active``."""
        with _registry_lock(self.home):
            state = self._load()
            return [self._public(entry, state) for entry in state["collections"]]

    def get(self, name: str) -> dict:
        """One collection by name (case-insensitive); DataError if unknown."""
        with _registry_lock(self.home):
            state = self._load()
            return self._public(self._find(state, name), state)

    def find_by_db(self, db_path: str | Path) -> dict | None:
        """The collection whose database is ``db_path``, or None."""
        target = _path_key(db_path)
        with _registry_lock(self.home):
            state = self._load()
            for entry in state["collections"]:
                if _path_key(entry["db"]) == target:
                    return self._public(entry, state)
        return None

    def active(self) -> dict | None:
        """The active collection, or None when the registry is empty."""
        with _registry_lock(self.home):
            state = self._load()
            entry = self._active_entry(state)
            return None if entry is None else self._public(entry, state)

    # -- mutations -----------------------------------------------------------

    def create(self, name: str, db_path: str | Path | None = None) -> dict:
        """Register a new collection backed by a freshly created empty index.

        Without ``db_path`` the database goes to ``collections_dir/<slug>.db``
        (suffixed ``-2``, ``-3`` … if taken). An explicit ``db_path`` must not
        exist yet; use :meth:`register` for existing files.
        """
        cleaned = validate_name(name)
        with _registry_lock(self.home, create=True):
            state = self._load()
            self._ensure_name_free(state, cleaned)
            slug = self._unique_slug(state, slugify(cleaned))
            if db_path is None:
                target = self._free_default_db(state, slug)
            else:
                target = Path(db_path).expanduser().resolve()
                if target.exists():
                    raise DataError(
                        f"{target} already exists; open it as a collection instead"
                    )
                self._ensure_db_free(state, target)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise DataError(f"cannot create folder {target.parent}: {exc}") from exc
            _create_empty_index(target)
            entry = {
                "name": cleaned,
                "db": str(target),
                "created_at": _utcnow_iso(),
                "slug": slug,
            }
            state["collections"].append(entry)
            if self._active_entry(state) is None:
                state["active"] = cleaned
            try:
                self._save(state)
            except Exception:
                _delete_db_files(target, missing_ok=True, strict=False)
                raise
            return self._public(entry, state)

    def register(self, name: str | None, db_path: str | Path) -> dict:
        """Add an existing OneFind database file under ``name``.

        ``name=None`` uses the file stem. The file must exist and open as a
        OneFind index (DataError otherwise). The stored path is absolute.
        """
        target = Path(db_path).expanduser().resolve()
        cleaned = validate_name(target.stem if name is None else name)
        with _registry_lock(self.home, create=True):
            state = self._load()
            self._ensure_name_free(state, cleaned)
            self._ensure_db_free(state, target)
            _validate_index_file(target)
            entry = {
                "name": cleaned,
                "db": str(target),
                "created_at": _utcnow_iso(),
                "slug": self._unique_slug(state, slugify(cleaned)),
            }
            state["collections"].append(entry)
            if self._active_entry(state) is None:
                state["active"] = cleaned
            self._save(state)
            return self._public(entry, state)

    def remove(self, name: str, *, delete_file: bool = False) -> None:
        """Forget a collection; with ``delete_file`` also delete db/-wal/-shm.

        Files are deleted before the registry changes, so a locked database
        (DataError) leaves the collection listed. If the active collection is
        removed, the first remaining one becomes active.
        """
        with _registry_lock(self.home, create=True):
            state = self._load()
            entry = self._find(state, name)
            if delete_file:
                _delete_db_files(Path(entry["db"]), missing_ok=True, strict=True)
            was_active = self._active_entry(state) is entry
            state["collections"] = [e for e in state["collections"] if e is not entry]
            if was_active:
                remaining = state["collections"]
                state["active"] = remaining[0]["name"] if remaining else None
            self._save(state)

    def rename(self, old: str, new: str) -> dict:
        """Rename a collection; the database file and slug stay unchanged."""
        cleaned = validate_name(new)
        with _registry_lock(self.home, create=True):
            state = self._load()
            entry = self._find(state, old)
            self._ensure_name_free(state, cleaned, ignore=entry)
            was_active = self._active_entry(state) is entry
            entry["name"] = cleaned
            if was_active:
                state["active"] = cleaned
            self._save(state)
            return self._public(entry, state)

    def set_active(self, name: str) -> dict:
        """Make ``name`` the active collection and return its entry."""
        with _registry_lock(self.home, create=True):
            state = self._load()
            entry = self._find(state, name)
            state["active"] = entry["name"]
            self._save(state)
            return self._public(entry, state)

    # -- backups -------------------------------------------------------------

    def backup_path_for(self, name: str) -> Path:
        """A fresh ``backups_dir/<slug>-YYYYmmdd-HHMMSS.db`` path (not created).

        ``backups_dir`` is created. A second backup within the same second
        gets a ``-2``, ``-3`` … suffix before ``.db``.
        """
        with _registry_lock(self.home):
            state = self._load()
            slug = self._slug_of(self._find(state, name))
            try:
                self.backups_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise DataError(f"cannot create backup folder {self.backups_dir}: {exc}") from exc
            stem = f"{slug}-{_now().strftime(_TIMESTAMP_FORMAT)}"
            candidate = self.backups_dir / f"{stem}.db"
            counter = 2
            while _db_family_exists(candidate):
                candidate = self.backups_dir / f"{stem}-{counter}.db"
                counter += 1
            return candidate

    def list_backups(self, name: str | None = None) -> list[dict]:
        """``*.db`` files in ``backups_dir``, newest first.

        Each item is ``{"path", "bytes", "created_at", "collection"}`` where
        ``collection`` is the slug parsed from the filename (None when the
        filename does not follow the backup pattern). With ``name`` only that
        collection's backups are returned; a name that is no longer registered
        is matched by its slug so backups of removed collections stay findable.
        """
        with _registry_lock(self.home):
            wanted: str | None = None
            if name is not None:
                state = self._load()
                try:
                    wanted = self._slug_of(self._find(state, name))
                except DataError:
                    wanted = slugify(validate_name(name))
            if not self.backups_dir.is_dir():
                return []
            items: list[tuple[float, str, dict]] = []
            try:
                candidates = [p for p in self.backups_dir.iterdir() if p.suffix.lower() == ".db"]
            except OSError as exc:
                raise DataError(f"cannot read backup folder {self.backups_dir}: {exc}") from exc
            for path in candidates:
                try:
                    if not path.is_file():
                        continue
                    stat = path.stat()
                except OSError:
                    continue
                collection = _parse_backup_slug(path.name)
                if wanted is not None and collection != wanted:
                    continue
                created = datetime.datetime.fromtimestamp(
                    stat.st_mtime, datetime.timezone.utc
                ).isoformat(timespec="seconds")
                item = {
                    "path": str(path.resolve()),
                    "bytes": stat.st_size,
                    "created_at": created,
                    "collection": collection,
                }
                items.append((stat.st_mtime, _backup_sort_name(path.name), item))
            items.sort(key=lambda row: (row[0], row[1]), reverse=True)
            return [row[2] for row in items]

    # -- internals -----------------------------------------------------------

    def _public(self, entry: dict, state: dict) -> dict:
        active = self._active_entry(state)
        return {
            "name": entry["name"],
            "db": entry["db"],
            "created_at": entry["created_at"],
            "exists": Path(entry["db"]).is_file(),
            "active": active is entry,
            "slug": self._slug_of(entry),
        }

    @staticmethod
    def _slug_of(entry: dict) -> str:
        return entry.get("slug") or slugify(entry["name"])

    @staticmethod
    def _find(state: dict, name: object) -> dict:
        if isinstance(name, str):
            wanted = _key(name)
            for entry in state["collections"]:
                if _key(entry["name"]) == wanted:
                    return entry
        raise DataError(f"unknown collection: {name!r}")

    @staticmethod
    def _active_entry(state: dict) -> dict | None:
        active = state.get("active")
        if isinstance(active, str):
            wanted = _key(active)
            for entry in state["collections"]:
                if _key(entry["name"]) == wanted:
                    return entry
        # A dangling or missing pointer falls back to the first collection so
        # library mode always has something to show when anything exists.
        return state["collections"][0] if state["collections"] else None

    @staticmethod
    def _ensure_name_free(state: dict, name: str, ignore: dict | None = None) -> None:
        wanted = _key(name)
        for entry in state["collections"]:
            if entry is not ignore and _key(entry["name"]) == wanted:
                raise NameTakenError(f"a collection named {entry['name']!r} already exists")

    @staticmethod
    def _ensure_db_free(state: dict, target: Path) -> None:
        wanted = _path_key(target)
        for entry in state["collections"]:
            if _path_key(entry["db"]) == wanted:
                raise AlreadyRegisteredError(
                    f"{target} is already the collection {entry['name']!r}",
                    existing=entry["name"],
                )

    def _unique_slug(self, state: dict, base: str) -> str:
        taken = {self._slug_of(entry) for entry in state["collections"]}
        return _with_suffix(base, lambda slug: slug in taken)

    def _free_default_db(self, state: dict, slug: str) -> Path:
        registered = {_path_key(entry["db"]) for entry in state["collections"]}
        directory = self.collections_dir.resolve()

        def taken(candidate: str) -> bool:
            path = directory / f"{candidate}.db"
            return _db_family_exists(path) or _path_key(path) in registered

        return directory / f"{_with_suffix(slug, taken)}.db"

    def _load(self) -> dict:
        empty: dict[str, Any] = {"version": REGISTRY_VERSION, "active": None, "collections": []}
        try:
            raw = self.registry_path.read_bytes()
        except FileNotFoundError:
            return empty
        except OSError as exc:
            raise DataError(f"cannot read {self.registry_path}: {exc}") from exc

        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError):
            self._quarantine(move=True)
            return empty
        if not isinstance(data, dict) or not isinstance(data.get("collections", []), list):
            self._quarantine(move=True)
            return empty
        version = data.get("version", REGISTRY_VERSION)
        if isinstance(version, int) and not isinstance(version, bool) and version > REGISTRY_VERSION:
            raise DataError(
                f"{self.registry_path} was written by a newer OneFind "
                f"(registry version {version}); upgrade OneFind to use it"
            )
        if version != REGISTRY_VERSION:
            self._quarantine(move=True)
            return empty

        collections: list[dict] = []
        names: set[str] = set()
        dropped = False
        for item in data.get("collections", []):
            entry = _coerce_entry(item)
            if entry is None or _key(entry["name"]) in names:
                dropped = True
                continue
            names.add(_key(entry["name"]))
            collections.append(entry)
        # Stored slugs win (first come, first served); missing or clashing
        # ones are regenerated afterwards so they cannot steal a stored slug.
        slugs: set[str] = set()
        for entry in collections:
            if entry["slug"] is not None and entry["slug"] not in slugs:
                slugs.add(entry["slug"])
            else:
                entry["slug"] = None
        for entry in collections:
            if entry["slug"] is None:
                entry["slug"] = _with_suffix(slugify(entry["name"]), lambda s: s in slugs)
                slugs.add(entry["slug"])
        active = data.get("active")
        state = {
            "version": REGISTRY_VERSION,
            "active": active if isinstance(active, str) else None,
            "collections": collections,
        }
        if dropped:
            # Keep a copy of the original so hand-edited entries we could not
            # understand are never silently lost, then rewrite the cleaned
            # registry so the copy is made once rather than on every read.
            self._quarantine(move=False)
            try:
                self._save(state)
            except DataError:
                pass
        return state

    def _quarantine(self, *, move: bool) -> Path:
        stamp = _now().strftime(_TIMESTAMP_FORMAT)
        base = f"{REGISTRY_FILENAME}.corrupt-{stamp}"
        target = self.home / base
        counter = 2
        while target.exists():
            target = self.home / f"{base}-{counter}"
            counter += 1
        try:
            if move:
                os.replace(self.registry_path, target)
            else:
                shutil.copy2(self.registry_path, target)
        except OSError as exc:
            raise DataError(
                f"{self.registry_path} is damaged and could not be set aside "
                f"as {target.name}: {exc}"
            ) from exc
        return target

    def _save(self, state: dict) -> None:
        payload = {
            "version": REGISTRY_VERSION,
            "active": state.get("active"),
            "collections": [
                {
                    "name": entry["name"],
                    "db": entry["db"],
                    "created_at": entry["created_at"],
                    "slug": self._slug_of(entry),
                }
                for entry in state["collections"]
            ],
        }
        active = self._active_entry(state)
        payload["active"] = None if active is None else active["name"]
        text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            handle, temp_name = tempfile.mkstemp(
                prefix=f".{REGISTRY_FILENAME}.", suffix=".tmp", dir=self.home
            )
        except OSError as exc:
            raise DataError(f"cannot write {self.registry_path}: {exc}") from exc
        temp = Path(temp_name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            _replace_with_retry(temp, self.registry_path)
        except OSError as exc:
            temp.unlink(missing_ok=True)
            raise DataError(f"cannot write {self.registry_path}: {exc}") from exc


# -- module helpers ----------------------------------------------------------


def _coerce_entry(item: object) -> dict | None:
    if not isinstance(item, dict):
        return None
    name, db = item.get("name"), item.get("db")
    if not isinstance(db, str) or not db.strip():
        return None
    try:
        cleaned = validate_name(name)
    except UsageError:
        return None
    created = item.get("created_at")
    return {
        "name": cleaned,
        "db": db,
        "created_at": created if isinstance(created, str) else "",
        "slug": _clean_slug(item.get("slug")),
    }


def _with_suffix(base: str, taken) -> str:
    if not taken(base):
        return base
    counter = 2
    while True:
        suffix = f"-{counter}"
        candidate = base[: SLUG_MAX_LENGTH - len(suffix)].rstrip("-") + suffix
        if not taken(candidate):
            return candidate
        counter += 1


def _db_family_exists(path: Path) -> bool:
    return any(Path(f"{path}{ext}").exists() for ext in ("", "-wal", "-shm"))


def _create_empty_index(target: Path) -> None:
    from .store import Index  # lazy: keeps `import onefind.library` light

    try:
        Index.open(target).close()
    except sqlite3.Error as exc:
        _delete_db_files(target, missing_ok=True, strict=False)
        raise DataError(f"cannot create a OneFind index at {target}: {exc}") from exc


def _validate_index_file(target: Path) -> None:
    from .store import Index

    if not target.exists():
        raise DataError(f"{target} does not exist")
    if not target.is_file():
        raise DataError(f"{target} is not a file")
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise DataError(f"cannot read {target}: {exc}") from exc
    if size == 0:
        # Index.open would initialise (and on failure delete) an empty file;
        # registering must never write to or remove the user's file.
        raise DataError(f"{target} is empty, not a OneFind database")
    try:
        Index.open(target).close()
    except DataError:
        raise
    except (sqlite3.Error, OSError) as exc:
        raise DataError(f"{target} is not a OneFind database: {exc}") from exc


def _delete_db_files(path: Path, *, missing_ok: bool, strict: bool) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            candidate.unlink(missing_ok=missing_ok)
        except OSError as exc:
            if strict:
                raise DataError(
                    f"cannot delete {candidate}: {exc}. Close any program using "
                    "this collection and try again."
                ) from exc


def _replace_with_retry(source: Path, target: Path, attempts: int = 5) -> None:
    # Windows refuses os.replace while another process briefly has the target
    # open for reading; a short retry covers a concurrent CLI/server read.
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


def _parse_backup_slug(filename: str) -> str | None:
    match = _BACKUP_NAME.match(filename)
    if not match:
        return None
    try:
        datetime.datetime.strptime(match.group("stamp"), _TIMESTAMP_FORMAT)
    except ValueError:
        return None
    return match.group("slug")


def _backup_sort_name(filename: str) -> str:
    # Tie-break equal mtimes so "x-...-10.db" sorts after "x-...-9.db".
    match = _BACKUP_NAME.match(filename)
    if not match:
        return filename
    seq = int(match.group("seq") or 1)
    return f"{match.group('slug')}-{match.group('stamp')}-{seq:06d}"
