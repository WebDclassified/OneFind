"""SQLite storage layer: connection, schema, transactional document upserts.

Schema follows docs/05-backend-design.md plus the v1.2 additions in
docs/11-implementation-contract.md. The chunks table owns stable row_ids
that the vec tables key off. Since v1.2.1 a long note may be split into
several passages (chunks seq 0..n-1, one vector each; docs/11 section 9);
the setting is per index, new collections have it on, and BEIR evaluation
turns it off so every document keeps exactly one whole-document vector.

Vector storage is created lazily on first embedder attachment because its DDL
depends on the model's dimension:
    vec_float(embedding float[dim] distance_metric=cosine)

Float search uses sqlite-vec KNN. Int8 and binary precision are derived from the
stored float vectors in application code per ADR-7.

Schema v3 adds file metadata columns on ``documents`` and the user tables
(collection roots, search history, saved queries, bookmarks, document
activity, index runs). A v2 database is upgraded in place by ``Index.open``.
"""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable

from .chunking import (
    DEFAULT_CHUNKING,
    chunking_from_meta,
    chunking_to_meta,
    count_words,
    normalize_chunking,
    split_passages,
)
from .errors import DataError, EnvError, OperationCancelled, UsageError

if TYPE_CHECKING:  # pragma: no cover
    from .embed import SentenceEmbedder

SCHEMA_VERSION = 3
BATCH_SIZE = 256
DEFAULT_INT8_SCALE = 1.0 / 127.0
HISTORY_LIMIT = 200
RUNS_LIMIT = 200
SEARCH_MODES = ("lexical", "semantic", "hybrid")
PRECISIONS = ("float", "int8", "binary")
FINISHED_RUN_STATUSES = ("succeeded", "failed", "cancelled")
INTERRUPTED_RUN_ERROR = "interrupted: indexing stopped before finishing (the process ended)"
MAX_NAME_CHARS = 200
MAX_QUERY_CHARS = 4000
MAX_NOTE_CHARS = 4000

# schema_meta keys for passage chunking (docs/11 section 9). ``chunking`` is
# the setting new and changed notes are written with (JSON or "off"; absent =
# off, i.e. one whole-note passage per document as before v1.2.1).
# ``chunking_built`` is the setting every stored note is known to follow;
# while the two differ (or built is "mixed") a vector rebuild is pending.
META_CHUNKING = "chunking"
META_CHUNKING_BUILT = "chunking_built"
CHUNKING_MIXED = "mixed"
# Documents re-chunked per committed batch by Index.rebuild_vectors().
REBUILD_BATCH_DOCS = 16

# schema_meta keys that describe the collection itself and survive clear().
_IDENTITY_META_KEYS = ("schema_version", "collection_name", "created_at", META_CHUNKING)
# Bound parameter lists stay well below SQLite's host-parameter limit.
_SQL_CHUNK = 500

ProgressCallback = Callable[[dict], None]


def utcnow_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


@dataclass
class Document:
    doc_id: str
    body: str
    title: str = ""
    source: str | None = None
    meta: dict | None = None
    content_hash: str | None = None
    mtime: float | None = None
    size_bytes: int | None = None
    ext: str | None = None
    folder: str | None = None
    tags: list[str] | None = None
    root_id: int | None = None


# Columns that schema v3 adds to ``documents``, in declaration order.
_V3_DOCUMENT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("content_hash", "TEXT"),
    ("mtime", "REAL"),
    ("size_bytes", "INTEGER"),
    ("ext", "TEXT"),
    ("folder", "TEXT"),
    ("tags_json", "TEXT"),
    ("root_id", "INTEGER"),
)

# Everything v3 adds besides the columns. Each statement is idempotent so the
# same list serves new databases, the v2 migration, and self-healing.
_V3_STATEMENTS: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_documents_folder ON documents(folder)",
    "CREATE INDEX IF NOT EXISTS idx_documents_mtime ON documents(mtime)",
    "CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(content_hash)",
    "CREATE INDEX IF NOT EXISTS idx_documents_root ON documents(root_id)",
    """CREATE TABLE IF NOT EXISTS collection_roots (
        root_id         INTEGER PRIMARY KEY,
        path            TEXT NOT NULL UNIQUE,
        exclude_json    TEXT,
        added_at        TEXT NOT NULL,
        last_indexed_at TEXT,
        files_only      INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS search_history (
        id           INTEGER PRIMARY KEY,
        query        TEXT NOT NULL,
        mode         TEXT NOT NULL,
        precision    TEXT,
        filters_json TEXT,
        result_count INTEGER NOT NULL,
        searched_at  TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS saved_queries (
        id           INTEGER PRIMARY KEY,
        name         TEXT NOT NULL,
        query        TEXT NOT NULL,
        mode         TEXT NOT NULL,
        precision    TEXT,
        filters_json TEXT,
        created_at   TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS bookmarks (
        doc_id     TEXT PRIMARY KEY REFERENCES documents(doc_id) ON DELETE CASCADE,
        note       TEXT,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS doc_activity (
        doc_id         TEXT PRIMARY KEY REFERENCES documents(doc_id) ON DELETE CASCADE,
        last_opened_at TEXT NOT NULL,
        open_count     INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS index_runs (
        id          INTEGER PRIMARY KEY,
        root_path   TEXT,
        started_at  TEXT NOT NULL,
        finished_at TEXT,
        status      TEXT NOT NULL,
        added       INTEGER DEFAULT 0,
        updated     INTEGER DEFAULT 0,
        unchanged   INTEGER DEFAULT 0,
        removed     INTEGER DEFAULT 0,
        skipped     INTEGER DEFAULT 0,
        error       TEXT
    )""",
)

# Columns added to ``collection_roots`` after the first v3 release (healed in
# place). ``files_only`` marks a root registered by indexing single files, so
# change detection only looks at the files that were actually indexed.
_V3_ROOT_COLUMNS: tuple[tuple[str, str], ...] = (("files_only", "INTEGER"),)

USER_TABLES = (
    "search_history",
    "saved_queries",
    "bookmarks",
    "doc_activity",
    "collection_roots",
    "index_runs",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id       TEXT PRIMARY KEY,
    title        TEXT NOT NULL DEFAULT '',
    body         TEXT NOT NULL,
    source       TEXT,
    meta_json    TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    content_hash TEXT,
    mtime        REAL,
    size_bytes   INTEGER,
    ext          TEXT,
    folder       TEXT,
    tags_json    TEXT,
    root_id      INTEGER
);

CREATE TABLE IF NOT EXISTS chunks (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    seq    INTEGER NOT NULL,
    text   TEXT NOT NULL,
    UNIQUE (doc_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

INSERT INTO schema_meta(key, value) VALUES ('schema_version', '3')
    ON CONFLICT(key) DO NOTHING;

CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    doc_id UNINDEXED,
    title,
    body,
    tokenize = 'porter unicode61'
);
""" + "".join(f"{statement};\n" for statement in _V3_STATEMENTS)


def _try_load_vec(conn: sqlite3.Connection) -> bool:
    """Load sqlite-vec; vector features degrade gracefully when absent."""
    try:
        import sqlite_vec

        conn.enable_load_extension(True)  # noqa: PLC2801 - required API
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)  # noqa: PLC2801
        return True
    except Exception:  # noqa: BLE001 - reported when vector features are used
        return False


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def _document_columns(conn: sqlite3.Connection) -> set[str]:
    return {str(row[1]) for row in conn.execute("PRAGMA table_info(documents)")}


def _root_columns(conn: sqlite3.Connection) -> set[str]:
    return {str(row[1]) for row in conn.execute("PRAGMA table_info(collection_roots)")}


def _apply_v3_structures(conn: sqlite3.Connection) -> None:
    existing = _document_columns(conn)
    for name, column_type in _V3_DOCUMENT_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE documents ADD COLUMN {name} {column_type}")
    for statement in _V3_STATEMENTS:
        conn.execute(statement)
    roots = _root_columns(conn)
    for name, column_type in _V3_ROOT_COLUMNS:
        if name not in roots:
            conn.execute(f"ALTER TABLE collection_roots ADD COLUMN {name} {column_type}")


def _heal_root_columns(conn: sqlite3.Connection) -> None:
    """Add optional ``collection_roots`` columns; a read-only file keeps working."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        roots = _root_columns(conn)
        for name, column_type in _V3_ROOT_COLUMNS:
            if name not in roots:
                conn.execute(f"ALTER TABLE collection_roots ADD COLUMN {name} {column_type}")
        conn.commit()
    except sqlite3.Error:
        if conn.in_transaction:
            conn.rollback()
        # Optional column: callers check for it before using it.


def _is_busy(exc: BaseException) -> bool:
    """True for SQLite lock contention (not damage)."""
    return isinstance(exc, sqlite3.OperationalError) and any(
        word in str(exc).lower() for word in ("locked", "busy")
    )


def _still_empty(target: Path) -> bool:
    """True when ``target`` is 0 bytes or holds no tables (safe to remove)."""
    try:
        if target.stat().st_size == 0:
            return True
    except OSError:
        return False
    try:
        probe = sqlite3.connect(f"{target.resolve().as_uri()}?mode=ro", uri=True, timeout=0.5)
    except (sqlite3.Error, ValueError, OSError):
        return False
    try:
        return probe.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0
    except sqlite3.Error:
        return False
    finally:
        probe.close()


def _ensure_wal(conn: sqlite3.Connection) -> None:
    """Switch to WAL only when needed; a read-only file keeps its journal mode."""
    try:
        current = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if str(current).lower() != "wal":
            conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError as exc:
        if "readonly" not in str(exc).lower().replace("-", ""):
            raise
        # A read-only file (e.g. a backup marked read-only) is still readable
        # in its current journal mode.


def _migrate_to_v3(conn: sqlite3.Connection) -> None:
    """Upgrade a v2 database (or heal a partial v3 one) in one transaction.

    Safe to re-run: columns are only added when absent and every other
    statement is ``IF NOT EXISTS``.
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
        _apply_v3_structures(conn)
        first_created = conn.execute("SELECT MIN(created_at) FROM documents").fetchone()[0]
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES ('created_at', ?) "
            "ON CONFLICT(key) DO NOTHING",
            (first_created or utcnow_iso(),),
        )
        conn.execute(
            "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION),),
        )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _loads_json(raw: Any, default: Any = None) -> Any:
    if raw in (None, ""):
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _dumps_filters(filters: Any) -> str | None:
    if filters is None:
        return None
    if hasattr(filters, "to_dict"):  # SearchFilters from onefind.search
        filters = filters.to_dict()
    if not isinstance(filters, dict):
        raise UsageError("filters must be a JSON object")
    if not filters:
        return None
    try:
        return json.dumps(filters, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise UsageError(f"filters are not JSON serializable: {exc}") from exc


def normalize_tags(tags: Iterable[Any] | None) -> list[str]:
    """Lowercase, trim, and de-duplicate tags while keeping their order."""
    seen: dict[str, None] = {}
    for tag in tags or ():
        cleaned = str(tag).strip().lower()
        if cleaned:
            seen.setdefault(cleaned, None)
    return list(seen)


def _chunked(values: list[Any], size: int = _SQL_CHUNK) -> Iterable[list[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _placeholders(values: list[Any]) -> str:
    return ",".join("?" for _ in values)


def _clamp_limit(limit: int, maximum: int) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError) as exc:
        raise UsageError("limit must be an integer") from exc
    return max(1, min(value, maximum))


def _db_footprint(path: Path) -> int:
    """Main file plus write-ahead log, i.e. what the collection occupies."""
    total = 0
    for candidate in (path, Path(f"{path}-wal")):
        try:
            total += candidate.stat().st_size
        except OSError:
            pass
    return total


def _same_path(left: Path, right: Path) -> bool:
    try:
        if left.exists() and right.exists():
            return os.path.samefile(left, right)
    except OSError:
        pass
    return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


class Index:
    """Owns the SQLite connection; all writes go through add_documents()."""

    def __init__(self, conn: sqlite3.Connection, path: str | Path) -> None:
        self.conn = conn
        self.path = Path(path)
        self.vec_ready = _try_load_vec(conn)
        self.embedder: "SentenceEmbedder | None" = None
        self._vector_cache: tuple["object", list[int]] | None = None
        self._vector_cache_version: int | None = None
        self._census_cache: tuple[tuple, dict] | None = None

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def open(cls, path: str | Path, *, readonly: bool = False) -> "Index":
        """Open (or create) a OneFind database, upgrading v2 files to v3.

        ``readonly=True`` opens an existing file without ever writing to it
        (no creation, no schema upgrade, no journal-mode switch); a file that
        would need an upgrade raises :class:`DataError` instead.
        """
        target = Path(path)
        if readonly:
            return cls._open_readonly(target)
        try:
            new_database = not target.exists() or target.stat().st_size == 0
        except OSError:
            new_database = False
        created_here = False
        try:
            conn = sqlite3.connect(target, check_same_thread=False)
        except OSError as exc:
            raise DataError(f"cannot open database at {target}: {exc}") from exc
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")

        try:
            tables = _table_names(conn)
            if tables and "schema_meta" not in tables:
                raise DataError(f"{target} is not a OneFind database")

            if tables:
                row = conn.execute(
                    "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                ).fetchone()
                try:
                    version = int(row[0]) if row else 0
                except (TypeError, ValueError) as exc:
                    raise DataError(f"invalid schema version in {target}") from exc
                if version not in (2, SCHEMA_VERSION):
                    raise DataError(
                        f"unsupported schema version {version} in {target}; "
                        f"this build supports {SCHEMA_VERSION} (version 2 indexes "
                        f"are upgraded automatically). Rebuild the index."
                    )
                required = {"documents", "chunks", "schema_meta", "fts"}
                missing = required - tables
                if missing:
                    raise DataError(
                        f"OneFind index is missing required tables: {sorted(missing)}"
                    )
                needs_upgrade = (
                    version != SCHEMA_VERSION
                    or not set(USER_TABLES) <= tables
                    or not {name for name, _ in _V3_DOCUMENT_COLUMNS}
                    <= _document_columns(conn)
                )
                if not needs_upgrade and not {
                    name for name, _ in _V3_ROOT_COLUMNS
                } <= _root_columns(conn):
                    _heal_root_columns(conn)
                if needs_upgrade:
                    try:
                        _migrate_to_v3(conn)
                    except sqlite3.Error as exc:
                        if "readonly" in str(exc).lower().replace("-", ""):
                            raise DataError(
                                f"{target} is read-only and was made by an older "
                                f"OneFind (schema v{version}); restore it with "
                                f"'onefind restore {target}' instead"
                            ) from exc
                        raise DataError(
                            f"could not upgrade {target} from schema v{version} to "
                            f"v{SCHEMA_VERSION}: {exc}"
                        ) from exc
            else:
                created_here = True
                # One transaction: another connection never sees a partial
                # schema. executescript commits any pending transaction first,
                # so BEGIN/COMMIT live inside the script. Every statement is
                # idempotent, so a racing creator simply waits and no-ops.
                # New collections split long notes into passages by default
                # (evaluate.py turns this off so BEIR runs stay whole-document).
                default_chunking = chunking_to_meta(dict(DEFAULT_CHUNKING))
                conn.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + _SCHEMA
                    + "INSERT INTO schema_meta(key, value) VALUES ('created_at', '"
                    + utcnow_iso()
                    + "') ON CONFLICT(key) DO NOTHING;\n"
                    + f"INSERT INTO schema_meta(key, value) VALUES ('{META_CHUNKING}', '"
                    + default_chunking
                    + "') ON CONFLICT(key) DO NOTHING;\n"
                    + f"INSERT INTO schema_meta(key, value) VALUES ('{META_CHUNKING_BUILT}', '"
                    + default_chunking
                    + "') ON CONFLICT(key) DO NOTHING;\n"
                    + f"PRAGMA user_version = {SCHEMA_VERSION};\n"
                    + "COMMIT;"
                )

            _ensure_wal(conn)
        except Exception as exc:
            if conn.in_transaction:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
            conn.close()
            # Only remove a file this call started creating, never one another
            # connection is building (or one that already holds tables).
            if new_database and created_here and not _is_busy(exc) and _still_empty(target):
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
            raise
        return cls(conn, target)

    @classmethod
    def _open_readonly(cls, target: Path) -> "Index":
        if not target.is_file():
            raise DataError(f"cannot open database at {target}: file not found")
        try:
            conn = sqlite3.connect(
                f"{target.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False
            )
        except (sqlite3.Error, ValueError) as exc:
            raise DataError(f"cannot open database at {target}: {exc}") from exc
        conn.row_factory = sqlite3.Row
        try:
            tables = _table_names(conn)
            if "schema_meta" not in tables:
                raise DataError(f"{target} is not a OneFind database")
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            try:
                version = int(row[0]) if row else 0
            except (TypeError, ValueError) as exc:
                raise DataError(f"invalid schema version in {target}") from exc
            missing = {"documents", "chunks", "schema_meta", "fts"} - tables
            if missing:
                raise DataError(f"OneFind index is missing required tables: {sorted(missing)}")
            if (
                version != SCHEMA_VERSION
                or not set(USER_TABLES) <= tables
                or not {name for name, _ in _V3_DOCUMENT_COLUMNS} <= _document_columns(conn)
            ):
                raise DataError(
                    f"{target} was made by an older OneFind (schema v{version}) and "
                    f"cannot be opened read-only; restore it with 'onefind restore {target}'"
                )
        except Exception:
            conn.close()
            raise
        return cls(conn, target)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Index":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- schema_meta helpers -------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM schema_meta WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def _write_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO schema_meta(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    def set_meta(self, key: str, value: str) -> None:
        self._write_meta(key, value)
        self.conn.commit()

    def _vec_table_exists(self) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'vec_float'"
            ).fetchone()
            is not None
        )

    def _invalidate_vector_cache(self) -> None:
        self._vector_cache = None
        self._vector_cache_version = None
        self._census_cache = None

    # -- embedder attachment ---------------------------------------------------

    def attach_embedder(
        self,
        embedder: "SentenceEmbedder",
        *,
        progress: ProgressCallback | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> None:
        """Bind an embedding model and lazily materialize the vec tables.

        Chunks that have no vector yet are embedded right away, one committed
        batch at a time with the model running outside any write transaction;
        ``progress(event)`` then receives ``{"phase": "embed", "done": int,
        "total": int}`` after each batch, and ``cancel()`` returning True
        raises :class:`OperationCancelled` between batches. The model binding
        is committed first, so a cancelled backfill leaves a supported,
        partly vectorized collection.
        """
        if not self.vec_ready:
            raise EnvError(
                "sqlite-vec unavailable in this interpreter; "
                "vector search disabled - run 'OneFind check' for the fix"
            )
        existing_model = self.get_meta("model_name")
        if existing_model and existing_model != embedder.name:
            raise DataError(
                f"index already bound to model '{existing_model}'; "
                f"refusing to mix models (got '{embedder.name}')"
            )
        revision = str(getattr(embedder, "revision", "unpinned"))
        existing_revision = self.get_meta("model_revision")
        if existing_revision and existing_revision != revision:
            raise DataError(
                f"index model revision is {existing_revision}; refusing to mix "
                f"revision {revision}"
            )
        existing_dim = self.get_meta("model_dim")
        if existing_dim and int(existing_dim) != embedder.dimension:
            raise DataError(
                f"model_dim mismatch: index has {existing_dim}, model reports "
                f"{embedder.dimension}"
            )

        dim = embedder.dimension
        try:
            if not existing_model and not existing_dim and self._vec_table_exists():
                # Vectors left without a model binding (e.g. by an older clear())
                # are meaningless and may have another dimension: start over.
                self.conn.execute("DROP TABLE vec_float")
            self.conn.execute(
                f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS vec_float USING vec0(
                    embedding float[{dim}] distance_metric=cosine
                );
                """
            )
            self._write_meta("model_name", embedder.name)
            self._write_meta("model_revision", revision)
            self._write_meta("model_dim", str(dim))
            max_tokens = getattr(embedder, "max_seq_length", None)
            if isinstance(max_tokens, int) and max_tokens > 0:
                self._write_meta("model_max_tokens", str(max_tokens))
            # Commit the binding right away: the backfill below runs the model
            # outside any write transaction and commits batch by batch.
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            self.embedder = None
            self._invalidate_vector_cache()
            raise
        self.embedder = embedder
        try:
            # Backfill a lexical-only index automatically. Existing vector rows
            # are retained, while renamed/new chunks receive fresh embeddings.
            # Committed batches stay committed when this is cancelled or fails;
            # rows still missing a vector are picked up by the next attach.
            self._embed_missing_vectors(progress=progress, cancel=cancel, commit_batches=True)
        except BaseException:
            if self.conn.in_transaction:
                self.conn.rollback()
            self.embedder = None
            self._invalidate_vector_cache()
            raise

    def _missing_vector_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT c.row_id, c.doc_id, c.text, d.title
            FROM chunks c
            JOIN documents d ON d.doc_id = c.doc_id
            WHERE c.row_id NOT IN (SELECT rowid FROM vec_float)
            ORDER BY c.row_id
            """
        ).fetchall()

    def _embed_missing_vectors(
        self,
        *,
        progress: ProgressCallback | None = None,
        cancel: Callable[[], bool] | None = None,
        commit_batches: bool = False,
    ) -> int:
        """Embed every chunk without a vector; returns how many were embedded.

        With ``commit_batches`` every batch is encoded with no transaction
        open and then stored and committed on its own, so other connections
        can write while the model runs and ``cancel()`` (checked before each
        batch) keeps finished batches. Without it the caller owns the
        transaction (repair).
        """
        missing = self._missing_vector_rows()
        if not missing:
            return 0
        from .embed import compose_embed_text

        total = len(missing)
        done = 0
        for part in _chunked(list(missing), BATCH_SIZE):
            if cancel is not None and cancel():
                raise OperationCancelled("building vectors was cancelled; finished batches were kept")
            row_ids = [int(row["row_id"]) for row in part]
            texts = [
                compose_embed_text(str(row["title"] or ""), str(row["text"] or ""))
                for row in part
            ]
            vectors = self._encode_vectors(texts)
            if commit_batches:
                try:
                    # A chunk removed by another connection meanwhile must not
                    # get an orphan vector.
                    alive: set[int] = set()
                    for ids in _chunked(row_ids):
                        alive.update(
                            int(row[0])
                            for row in self.conn.execute(
                                f"SELECT row_id FROM chunks WHERE row_id IN ({_placeholders(ids)})",  # noqa: S608
                                ids,
                            )
                        )
                    keep = [pos for pos, row_id in enumerate(row_ids) if row_id in alive]
                    self._store_vectors([row_ids[pos] for pos in keep], vectors[keep])
                    self.conn.commit()
                except BaseException:
                    self.conn.rollback()
                    raise
                finally:
                    self._invalidate_vector_cache()
            else:
                self._store_vectors(row_ids, vectors)
            done += len(part)
            if progress is not None:
                progress({"phase": "embed", "done": done, "total": total})
        return total

    # -- writes ------------------------------------------------------------

    def add_documents(
        self,
        docs: Iterable[Document],
        *,
        progress: ProgressCallback | None = None,
    ) -> int:
        """Upsert documents (+fts+chunk[+vector] rows) in committed batches.

        Returns the number of documents processed. A failure part-way
        leaves earlier batches committed and the database openable.

        ``progress(event)`` is called after each committed batch with
        ``{"phase": "write" | "embed", "done": int, "total": int | None}``;
        the phase is ``"embed"`` when the batch also computed vectors.
        """
        if self.embedder is None and self.has_vectors():
            raise UsageError(
                "this index already contains embeddings; re-run indexing with "
                "--embed so document and vector data stay synchronized"
            )

        total = len(docs) if hasattr(docs, "__len__") else None  # type: ignore[arg-type]
        processed = 0
        batch: list[Document] = []
        # One setting for the whole call: every note it writes follows it.
        chunking = self.chunking_config()

        def _flush(batch: list[Document]) -> None:
            plans = [split_passages(document.body, chunking) for document in batch]
            # Encode first, with no transaction open: the model can take
            # seconds per batch and must not hold the collection's write lock.
            vectors = None
            if self.embedder is not None:
                vectors = self._passage_vectors(
                    [
                        (document.doc_id, document.title, passages)
                        for document, passages in zip(batch, plans)
                    ]
                )
            vec_table = self._vec_table_exists()
            try:
                now = utcnow_iso()
                for position, document in enumerate(batch):
                    meta_json = (
                        json.dumps(document.meta, sort_keys=True)
                        if document.meta
                        else None
                    )
                    tags = normalize_tags(document.tags)
                    self.conn.execute(
                        """
                        INSERT INTO documents
                            (doc_id, title, body, source, meta_json, created_at,
                             updated_at, content_hash, mtime, size_bytes, ext,
                             folder, tags_json, root_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(doc_id) DO UPDATE SET
                            title        = excluded.title,
                            body         = excluded.body,
                            source       = excluded.source,
                            meta_json    = excluded.meta_json,
                            updated_at   = excluded.updated_at,
                            content_hash = excluded.content_hash,
                            mtime        = excluded.mtime,
                            size_bytes   = excluded.size_bytes,
                            ext          = excluded.ext,
                            folder       = excluded.folder,
                            tags_json    = excluded.tags_json,
                            root_id      = excluded.root_id
                        """,
                        (
                            document.doc_id,
                            document.title,
                            document.body,
                            document.source,
                            meta_json,
                            now,
                            now,
                            document.content_hash,
                            document.mtime,
                            document.size_bytes,
                            document.ext,
                            document.folder,
                            json.dumps(tags) if tags else None,
                            document.root_id,
                        ),
                    )
                    row_ids = self._write_passages(
                        document.doc_id,
                        plans[position],
                        drop_vectors=vec_table and self.vec_ready and vectors is None,
                    )
                    self.conn.execute(
                        "DELETE FROM fts WHERE doc_id = ?", (document.doc_id,)
                    )
                    self.conn.execute(
                        "INSERT INTO fts (doc_id, title, body) VALUES (?, ?, ?)",
                        (document.doc_id, document.title, document.body),
                    )
                    if vectors is not None:
                        self._store_vectors(row_ids, vectors[position])

                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise
            finally:
                self._invalidate_vector_cache()

        def _report() -> None:
            if progress is not None:
                progress(
                    {
                        "phase": "embed" if self.embedder is not None else "write",
                        "done": processed,
                        "total": total,
                    }
                )

        for doc in docs:
            batch.append(doc)
            if len(batch) >= BATCH_SIZE:
                _flush(batch)
                processed += len(batch)
                batch = []
                _report()
        if batch:
            _flush(batch)
            processed += len(batch)
            _report()
        return processed

    def update_metadata(self, rows: Iterable[dict]) -> int:
        """Refresh file metadata of existing documents without re-indexing text.

        Each row is ``{"doc_id", "mtime", "size_bytes", "root_id", "folder",
        "ext"}``; used by incremental ingest when a file was touched but its
        content hash is unchanged. Returns the number of rows updated.
        """
        updated = 0
        try:
            for row in rows:
                cursor = self.conn.execute(
                    """
                    UPDATE documents
                    SET mtime = ?, size_bytes = ?, root_id = ?, folder = ?, ext = ?
                    WHERE doc_id = ?
                    """,
                    (
                        row.get("mtime"),
                        row.get("size_bytes"),
                        row.get("root_id"),
                        row.get("folder"),
                        row.get("ext"),
                        row["doc_id"],
                    ),
                )
                updated += cursor.rowcount
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return updated

    def remove_documents(self, doc_ids: Iterable[str]) -> int:
        """Remove documents from the index only; source files are untouched.

        Deletes the document, chunk, FTS, vector, bookmark, and activity rows
        in one transaction and returns how many documents were removed.
        """
        unique = list(dict.fromkeys(str(doc_id) for doc_id in doc_ids))
        if not unique:
            return 0
        vec_table = self._vec_table_exists()
        if vec_table and not self.vec_ready:
            raise EnvError(
                "sqlite-vec unavailable in this interpreter, so stored vectors "
                "cannot be removed - run 'OneFind check' for the fix"
            )
        try:
            removed = self._delete_documents(unique)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            self._invalidate_vector_cache()
        return removed

    def _delete_documents(self, doc_ids: list[str]) -> int:
        """Delete documents and every derived row (caller owns the transaction)."""
        unique = list(dict.fromkeys(str(doc_id) for doc_id in doc_ids))
        if not unique:
            return 0
        vec_table = self._vec_table_exists()
        removed = 0
        for part in _chunked(unique):
            marks = _placeholders(part)
            if vec_table:
                row_ids = [
                    int(row[0])
                    for row in self.conn.execute(
                        f"SELECT row_id FROM chunks WHERE doc_id IN ({marks})",  # noqa: S608
                        part,
                    )
                ]
                for row_id in row_ids:
                    self.conn.execute("DELETE FROM vec_float WHERE rowid = ?", (row_id,))
            self.conn.execute(f"DELETE FROM fts WHERE doc_id IN ({marks})", part)  # noqa: S608
            self.conn.execute(f"DELETE FROM chunks WHERE doc_id IN ({marks})", part)  # noqa: S608
            self.conn.execute(f"DELETE FROM bookmarks WHERE doc_id IN ({marks})", part)  # noqa: S608
            self.conn.execute(
                f"DELETE FROM doc_activity WHERE doc_id IN ({marks})", part  # noqa: S608
            )
            cursor = self.conn.execute(
                f"DELETE FROM documents WHERE doc_id IN ({marks})", part  # noqa: S608
            )
            removed += cursor.rowcount
        return removed

    def _write_passages(self, doc_id: str, passages: list[str], *, drop_vectors: bool) -> list[int]:
        """Make ``doc_id``'s chunks exactly ``passages`` (seq 0..n-1); returns row_ids.

        Existing rows keep their row_id (seq 0 of a short note never moves);
        rows past the new end are deleted together with their vectors.
        ``drop_vectors`` also deletes the vectors of the kept rows (used when
        no new vectors will be stored for them). Caller owns the transaction.
        """
        count = len(passages)
        stale = [
            int(row[0])
            for row in self.conn.execute(
                "SELECT row_id FROM chunks WHERE doc_id = ? AND seq >= ?", (doc_id, count)
            )
        ]
        if stale and self._vec_table_exists() and self.vec_ready:
            for row_id in stale:
                self.conn.execute("DELETE FROM vec_float WHERE rowid = ?", (row_id,))
        if stale:
            self.conn.execute("DELETE FROM chunks WHERE doc_id = ? AND seq >= ?", (doc_id, count))
        row_ids: list[int] = []
        for seq, text in enumerate(passages):
            row = self.conn.execute(
                """
                INSERT INTO chunks (doc_id, seq, text) VALUES (?, ?, ?)
                ON CONFLICT(doc_id, seq) DO UPDATE SET text = excluded.text
                RETURNING row_id
                """,
                (doc_id, seq, text),
            ).fetchone()
            row_ids.append(int(row[0]))
        if drop_vectors:
            for row_id in row_ids:
                self.conn.execute("DELETE FROM vec_float WHERE rowid = ?", (row_id,))
        return row_ids

    def _reusable_vectors(self, doc_ids: list[str]) -> dict[str, Any]:
        """embed text -> stored vector for the current passages of ``doc_ids``.

        A vector is a pure function of its embed text (title + passage) under
        the bound model, so an edited long note re-embeds only the passages
        that changed. Read-only; safe to call with no transaction open.
        """
        if not doc_ids or not self._vec_table_exists() or not self.vec_ready:
            return {}
        import numpy as np

        from .embed import compose_embed_text

        dim = int(self.get_meta("model_dim") or 0)
        found: dict[str, Any] = {}
        for part in _chunked(list(dict.fromkeys(doc_ids))):
            rows = self.conn.execute(
                f"""
                SELECT c.row_id, c.text, d.title
                FROM chunks c JOIN documents d ON d.doc_id = c.doc_id
                WHERE c.doc_id IN ({_placeholders(part)})
                """,  # noqa: S608 - placeholders only
                part,
            ).fetchall()
            for row in rows:
                blob = self.conn.execute(
                    "SELECT embedding FROM vec_float WHERE rowid = ?", (int(row["row_id"]),)
                ).fetchone()
                if blob is None or not dim or len(blob[0]) != dim * 4:
                    continue
                text = compose_embed_text(str(row["title"] or ""), str(row["text"] or ""))
                found.setdefault(text, np.frombuffer(blob[0], dtype="<f4").copy())
        return found

    def _passage_vectors(self, items: list[tuple[str, str, list[str]]]) -> list[list[Any]]:
        """Vectors for ``[(doc_id, title, passages)]``, aligned with the passages.

        Each passage is embedded as ``title + "\\n" + passage``; a one-passage
        note therefore gets exactly the whole-note text it always had. Vectors
        of unchanged passages of multi-passage notes are reused. Runs the
        model with no transaction open.
        """
        from .embed import compose_embed_text

        reuse = self._reusable_vectors(
            [doc_id for doc_id, _title, passages in items if len(passages) > 1]
        )
        result: list[list[Any]] = [[None] * len(passages) for _id, _title, passages in items]
        texts: list[str] = []
        slots: list[tuple[int, int]] = []
        for position, (_doc_id, title, passages) in enumerate(items):
            for seq, passage in enumerate(passages):
                text = compose_embed_text(title, passage)
                cached = reuse.get(text) if len(passages) > 1 else None
                if cached is not None:
                    result[position][seq] = cached
                else:
                    texts.append(text)
                    slots.append((position, seq))
        if texts:
            encoded = self._encode_vectors(texts)
            for (position, seq), vector in zip(slots, encoded):
                result[position][seq] = vector
        return result

    def _encode_vectors(self, texts: list[str]):
        """Run the model and validate its output; touches no database state."""
        import numpy as np

        vectors = self.embedder.encode_documents(texts)
        expected_shape = (len(texts), self.embedder.dimension)
        if not isinstance(vectors, np.ndarray) or vectors.shape != expected_shape:
            actual = getattr(vectors, "shape", type(vectors).__name__)
            raise DataError(
                f"embedding model returned invalid shape {actual}; expected {expected_shape}"
            )
        if not np.isfinite(vectors).all():
            raise DataError("embedding model returned NaN or infinite values")
        if np.any(np.linalg.norm(vectors.astype(np.float64), axis=1) == 0):
            raise DataError("embedding model returned a zero-length vector")
        return vectors

    def _store_vectors(self, row_ids: list[int], vectors) -> None:
        """Write validated vectors for chunk rows (caller owns the transaction)."""
        from .embed import serialize_float32

        if not row_ids:
            return
        if self.get_meta("int8_scale") is None:
            # Sentence embeddings are unit-normalized, so their components are
            # bounded by [-1, 1]. A fixed scale is deterministic across batches
            # and avoids first-batch calibration drift.
            self._write_meta("int8_scale", repr(DEFAULT_INT8_SCALE))
        for row_id, vector in zip(row_ids, vectors):
            self.conn.execute("DELETE FROM vec_float WHERE rowid = ?", (row_id,))
            self.conn.execute(
                "INSERT INTO vec_float(rowid, embedding) VALUES (?, ?)",
                (row_id, serialize_float32(vector)),
            )

    def _sync_vectors(self, chunk_rows: list[tuple[int, str]]) -> None:
        """Encode, validate, and store normalized float32 vectors."""
        vectors = self._encode_vectors([text for _, text in chunk_rows])
        self._store_vectors([row_id for row_id, _ in chunk_rows], vectors)

    def iter_vector_blocks(self, block_size: int = 2048):
        """Yield bounded blocks of stored vectors without a full-corpus cache.

        The whole scan runs in one read transaction (a single WAL snapshot),
        so commits by other connections are simply not visible instead of
        failing it; a transaction the caller already had open is left alone.
        """
        import numpy as np

        if not 1 <= block_size <= 10_000:
            raise UsageError("vector block size must be within 1..10000")
        dim_raw = self.get_meta("model_dim")
        dim = int(dim_raw) if dim_raw else 0
        if not dim:
            return
        conn = self.conn
        owned = not conn.in_transaction
        if owned:
            conn.execute("BEGIN")
        try:
            start_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
            last_row_id = -1
            while True:
                rows = conn.execute(
                    """
                    SELECT rowid, embedding
                    FROM vec_float
                    WHERE rowid > ?
                    ORDER BY rowid
                    LIMIT ?
                    """,
                    (last_row_id, block_size),
                ).fetchall()
                if not rows:
                    break
                row_ids = [int(row[0]) for row in rows]
                blob = b"".join(row[1] for row in rows)
                matrix = np.frombuffer(blob, dtype="<f4").reshape(len(rows), dim).copy()
                yield matrix, row_ids
                last_row_id = row_ids[-1]
                if len(rows) < block_size:
                    break
            # Safety net: inside one snapshot only a write through this
            # connection mid-scan can move the version.
            end_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
            if start_version != end_version:
                raise DataError("vector index changed during scanning; retry the search")
        finally:
            if owned and conn.in_transaction:
                conn.commit()

    def get_all_vectors(self) -> tuple["object", list[int]]:
        """All stored embeddings as (<ndarray float32 [n, dim]>, row_ids).

        Loaded once per Index instance (invalidated by writes); callers
        must treat the returned array as READ-ONLY.
        """
        import numpy as np

        data_version = int(self.conn.execute("PRAGMA data_version").fetchone()[0])
        if (
            self._vector_cache is not None
            and self._vector_cache_version == data_version
        ):
            return self._vector_cache

        dim_raw = self.get_meta("model_dim")
        dim = int(dim_raw) if dim_raw else 0
        rows = self.conn.execute(
            "SELECT rowid, embedding FROM vec_float ORDER BY rowid"
        ).fetchall()
        row_ids = [int(r[0]) for r in rows]
        if not rows or not dim:
            result = (np.empty((0, dim or 0), dtype=np.float32), row_ids)
        else:
            blob = b"".join(r[1] for r in rows)
            matrix = np.frombuffer(blob, dtype="<f4").reshape(len(rows), dim)
            result = (matrix.copy(), row_ids)
        self._vector_cache = result
        self._vector_cache_version = data_version
        return result

    def vectors_for_docs(self, doc_ids: list[str]) -> dict:
        """doc_id -> float32 ndarray, for reranking a candidate pool.

        A one-passage note maps to its vector (shape ``[dim]``, as before
        passage chunking). A note split into passages maps to a matrix of
        shape ``[n_passages, dim]`` whose row ``i`` is passage ``seq == i``;
        callers score it by its best row. A note with any passage vector
        missing is left out, so rows always line up with ``seq``.
        """
        import numpy as np

        unique = list(dict.fromkeys(doc_ids))
        if not unique:
            return {}
        grouped: dict[str, list[tuple[int, bytes]]] = {}
        expected: dict[str, int] = {}
        for part in _chunked(unique):
            marks = _placeholders(part)
            for doc_id, count in self.conn.execute(
                f"SELECT doc_id, COUNT(*) FROM chunks WHERE doc_id IN ({marks}) GROUP BY doc_id",  # noqa: S608
                part,
            ):
                expected[str(doc_id)] = int(count)
            # CROSS JOIN keeps chunks as the outer loop (vec0 rowid point lookups).
            rows = self.conn.execute(
                f"""
                SELECT c.doc_id, c.seq, v.embedding
                FROM chunks c
                CROSS JOIN vec_float v
                WHERE v.rowid = c.row_id AND c.doc_id IN ({marks})
                ORDER BY c.doc_id, c.seq
                """,  # noqa: S608 - placeholders only, no user data in SQL text
                part,
            ).fetchall()
            for doc_id, seq, blob in rows:
                grouped.setdefault(str(doc_id), []).append((int(seq), blob))
        result: dict[str, Any] = {}
        for doc_id, items in grouped.items():
            if len(items) != expected.get(doc_id) or [seq for seq, _ in items] != list(
                range(len(items))
            ):
                continue
            vectors = [np.frombuffer(blob, dtype="<f4").copy() for _seq, blob in items]
            result[doc_id] = vectors[0] if len(vectors) == 1 else np.stack(vectors)
        return result

    def passage_info(self, doc_ids: list[str]) -> dict[str, int]:
        """doc_id -> number of stored passages (chunks) for each known note."""
        unique = list(dict.fromkeys(str(doc_id) for doc_id in doc_ids))
        counts: dict[str, int] = {}
        for part in _chunked(unique):
            for doc_id, count in self.conn.execute(
                f"SELECT doc_id, COUNT(*) FROM chunks WHERE doc_id IN ({_placeholders(part)}) "  # noqa: S608
                "GROUP BY doc_id",
                part,
            ):
                counts[str(doc_id)] = int(count)
        return counts

    def get_documents(self, doc_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch display metadata and bodies for a bounded result set."""
        unique = list(dict.fromkeys(doc_ids))
        if not unique:
            return {}
        result: dict[str, dict[str, Any]] = {}
        for part in _chunked(unique):
            rows = self.conn.execute(
                f"""
                SELECT doc_id, title, body, source, folder, ext, mtime, tags_json
                FROM documents
                WHERE doc_id IN ({_placeholders(part)})
                """,  # noqa: S608 - placeholders only
                part,
            ).fetchall()
            for row in rows:
                result[str(row["doc_id"])] = {
                    "title": str(row["title"] or ""),
                    "body": str(row["body"] or ""),
                    "source": row["source"],
                    "folder": row["folder"],
                    "ext": row["ext"],
                    "mtime": row["mtime"],
                    "tags": _loads_json(row["tags_json"], []) or [],
                }
        return result

    def clear(self) -> None:
        """Full reset: remove corpus, vectors, and user data; keep the schema.

        The collection identity (schema version, name, creation time) is kept.
        The vector table is dropped and any attached model is detached, so a
        model with another dimension can be attached afterwards.
        """
        try:
            if self._vec_table_exists():
                if not self.vec_ready:
                    raise EnvError(
                        "sqlite-vec unavailable in this interpreter, so stored "
                        "vectors cannot be cleared - run 'OneFind check' for the fix"
                    )
                # Drop rather than empty: the vec0 table has a fixed dimension,
                # and the next attach_embedder may bring a different model.
                self.conn.execute("DROP TABLE vec_float")
            for table in USER_TABLES:
                self.conn.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed names
            self.conn.execute("DELETE FROM fts")
            self.conn.execute("DELETE FROM chunks")
            self.conn.execute("DELETE FROM documents")
            self.conn.execute(
                f"DELETE FROM schema_meta WHERE key NOT IN "
                f"({_placeholders(list(_IDENTITY_META_KEYS))})",  # noqa: S608
                _IDENTITY_META_KEYS,
            )
            # The passage setting is kept; an empty collection follows it.
            current = self.get_meta(META_CHUNKING)
            if current is not None:
                self._write_meta(META_CHUNKING_BUILT, current)
            self._invalidate_vector_cache()
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        # The model binding is gone; the next index run attaches a model again.
        self.embedder = None

    # -- passage chunking (docs/11 section 9) ----------------------------------

    def chunking_config(self) -> dict | None:
        """The passage setting new and changed notes are written with.

        ``{"version": 1, "max_words": int, "overlap_words": int}`` or None
        (off: one whole-note passage per document, as before v1.2.1). New
        collections default to :data:`onefind.chunking.DEFAULT_CHUNKING`;
        collections created before passage chunking existed keep None.
        """
        return chunking_from_meta(self.get_meta(META_CHUNKING))

    def _chunking_metas(self) -> tuple[dict | None, str, str]:
        """(setting, its meta text, meta text of the setting stored notes follow)."""
        config = self.chunking_config()
        raw_built = self.get_meta(META_CHUNKING_BUILT)
        if raw_built == CHUNKING_MIXED:
            built = CHUNKING_MIXED
        else:
            built = chunking_to_meta(chunking_from_meta(raw_built))
        return config, chunking_to_meta(config), built

    def set_chunking(self, config: Any) -> dict | None:
        """Change the passage setting; returns the normalized setting.

        ``config``: None/``"off"`` (whole notes), ``"default"``/True, or a
        dict such as ``{"max_words": 200, "overlap_words": 40}`` (validated
        by :func:`onefind.chunking.normalize_chunking`; raises UsageError).
        Notes already stored keep their passages and vectors until
        :meth:`rebuild_vectors` runs; :meth:`vectors_need_rebuild` reports
        that (it stays False when no stored note is affected, e.g. an empty
        collection or one with only short notes).
        """
        new = normalize_chunking(config)
        new_meta = chunking_to_meta(new)
        if self.conn.in_transaction:
            self.conn.commit()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            _config, current, built = self._chunking_metas()
            stored = self.get_meta(META_CHUNKING)
            if new_meta == current:
                if stored is None:
                    self._write_meta(META_CHUNKING, new_meta)
                self.conn.commit()
                return new
            has_documents = (
                self.conn.execute("SELECT 1 FROM documents LIMIT 1").fetchone() is not None
            )
            if not has_documents:
                next_built = new_meta
            elif built == current:
                next_built = built
            else:
                next_built = CHUNKING_MIXED
            self._write_meta(META_CHUNKING, new_meta)
            if next_built != new_meta and not self._rechunk_candidates([new]):
                # Every stored note already looks the same under the new
                # setting (e.g. only short notes): nothing to rebuild.
                next_built = new_meta
            self._write_meta(META_CHUNKING_BUILT, next_built)
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        finally:
            self._invalidate_vector_cache()
        return new

    def _accepted_chunkings(self) -> list[dict | None]:
        """Passage settings a stored note may legitimately follow right now.

        The current setting, plus the one stored notes follow while a rebuild
        is pending; when that is unknown ("mixed") whole notes and the
        default setting are accepted too.
        """
        config, _current, built = self._chunking_metas()
        options: list[dict | None] = [config]
        if built == CHUNKING_MIXED:
            options += [None, dict(DEFAULT_CHUNKING)]
        else:
            options.append(chunking_from_meta(built))
        unique: dict[str, dict | None] = {}
        for option in options:
            unique.setdefault(chunking_to_meta(option), option)
        return list(unique.values())

    def _rechunk_candidates(self, configs: list[dict | None]) -> list[str]:
        """doc_ids (sorted) whose stored passages match none of ``configs``.

        A note matches a setting when its chunks are exactly
        ``split_passages(body, setting)`` in seq order. Short notes and
        whole-note passages are checked in SQL/word counts without
        splitting. Runs in one read snapshot.
        """
        conn = self.conn
        owned = not conn.in_transaction
        if owned:
            conn.execute("BEGIN")
        try:
            result: list[str] = []
            cursor = conn.execute(
                """
                SELECT d.doc_id, d.body,
                       (SELECT COUNT(*) FROM chunks c WHERE c.doc_id = d.doc_id) AS n,
                       EXISTS(SELECT 1 FROM chunks c
                              WHERE c.doc_id = d.doc_id AND c.seq = 0 AND c.text = d.body)
                           AS whole
                FROM documents d
                ORDER BY d.doc_id
                """
            )
            for row in cursor:
                doc_id = str(row["doc_id"])
                body = str(row["body"] or "")
                count = int(row["n"])
                whole = count == 1 and bool(row["whole"])
                words: int | None = None
                stored: list[str] | None = None
                matched = False
                for config in configs:
                    if config is None:
                        matched = whole
                    else:
                        if words is None:
                            words = count_words(body)
                        if words <= config["max_words"]:
                            matched = whole
                        elif count >= 2:
                            expected = split_passages(body, config)
                            if len(expected) == count:
                                if stored is None:
                                    stored = [
                                        str(item[0])
                                        for item in conn.execute(
                                            "SELECT text FROM chunks WHERE doc_id = ? ORDER BY seq",
                                            (doc_id,),
                                        )
                                    ]
                                matched = stored == expected
                    if matched:
                        break
                if not matched:
                    result.append(doc_id)
            return result
        finally:
            if owned and conn.in_transaction:
                conn.commit()

    def _passage_census(self) -> dict:
        """Cached passage facts for stats(): setting, long notes, pending notes.

        Cached per database version (other connections' commits) and this
        connection's own change count, so polling stats stays cheap.
        """
        version = int(self.conn.execute("PRAGMA data_version").fetchone()[0])
        key = (version, self.conn.total_changes)
        if self._census_cache is not None and self._census_cache[0] == key:
            return self._census_cache[1]
        config, current, built = self._chunking_metas()
        limit = int((config or DEFAULT_CHUNKING)["max_words"])
        if config is not None and built == current:
            long_notes = int(
                self.conn.execute(
                    "SELECT COUNT(*) FROM (SELECT doc_id FROM chunks GROUP BY doc_id "
                    "HAVING COUNT(*) > 1)"
                ).fetchone()[0]
            )
        else:
            # Whitespace runs approximate the word count in SQL (no body is
            # loaded into Python): enough for a "would be split" hint.
            long_notes = int(
                self.conn.execute(
                    """
                    SELECT COUNT(*) FROM documents
                    WHERE length(body) > ?
                      AND length(body) - length(replace(replace(replace(replace(
                          body, ' ', ''), char(10), ''), char(9), ''), char(13), '')) >= ?
                    """,
                    (limit, limit),
                ).fetchone()[0]
            )
        pending = self._rechunk_candidates([config]) if built != current else []
        census = {
            "chunking": config,
            "current": current,
            "built": built,
            "long_notes": long_notes,
            "pending": pending,
        }
        self._census_cache = (key, census)
        return census

    def vectors_need_rebuild(self) -> dict:
        """Whether stored passages/vectors still follow an older passage setting.

        Returns ``{"needed": bool, "documents": int, "reason": str,
        "chunking": setting | None, "long_notes": int, "recommended": bool}``.
        ``documents`` is how many notes :meth:`rebuild_vectors` will
        re-split (and re-embed when the collection has vectors).
        ``recommended`` is True when passage search is off but some notes are
        longer than one passage (only a hint; evaluation indexes never get
        it). ``reason`` is one plain sentence ("" when there is nothing to say).
        """
        census = self._passage_census()
        config = census["chunking"]
        pending = census["pending"]
        count = len(pending)
        long_notes = int(census["long_notes"])
        result = {
            "needed": bool(pending),
            "documents": count,
            "reason": "",
            "chunking": config,
            "long_notes": long_notes,
            "recommended": False,
        }
        notes = _plural(count, "note")
        if pending:
            if config is None:
                result["reason"] = (
                    f"Passage search was turned off; {notes} still "
                    f"{'has' if count == 1 else 'have'} passage vectors and need one "
                    "vector per note again."
                )
            elif census["built"] == chunking_to_meta(None):
                result["reason"] = (
                    f"{notes} {'is' if count == 1 else 'are'} longer than one passage but "
                    f"still {'has' if count == 1 else 'have'} a single whole-note vector, so "
                    "meaning search only sees the beginning. Rebuild vectors to split "
                    "them into passages."
                )
            else:
                result["reason"] = (
                    f"The passage settings changed; {notes} need new passages."
                )
        elif config is None and long_notes and self.get_meta("eval_manifest") is None:
            result["recommended"] = True
            result["reason"] = (
                f"{_plural(long_notes, 'note')} {'is' if long_notes == 1 else 'are'} longer "
                f"than {DEFAULT_CHUNKING['max_words']} words; meaning search only reads the "
                "beginning of each. Turn on passage search and rebuild vectors to cover "
                "them in full."
            )
        return result

    def rebuild_vectors(
        self,
        *,
        progress: ProgressCallback | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> dict:
        """Re-split notes under the current passage setting and re-embed them.

        Only notes whose stored passages differ from the current setting are
        rewritten; each committed batch replaces a note's passages and
        vectors together, so an interrupted rebuild (``cancel()`` returning
        True raises :class:`OperationCancelled` between batches, or a crash)
        leaves every note either fully old or fully new, and running it
        again continues where it stopped. Requires the embedding model to be
        attached when the collection has vectors (UsageError otherwise).

        ``progress(event)`` gets ``{"phase": "scan" | "embed" | "write" |
        "done", "done": int, "total": int, "message": str}`` (``embed`` when
        vectors are computed; ``done`` counts notes). Returns ``{"documents": checked, "rebuilt": n,
        "passages": written, "vectors_embedded": n, "skipped": n,
        "chunking": setting, "complete": True}``.
        """
        if self.has_vectors():
            if not self.vec_ready:
                raise EnvError(
                    "sqlite-vec unavailable in this interpreter, so vectors cannot be "
                    "rebuilt - run 'OneFind check' for the fix"
                )
            if self.embedder is None:
                raise UsageError(
                    "rebuilding vectors needs the embedding model; attach it first "
                    "(the collection's model is "
                    f"'{self.get_meta('model_name')}')"
                )
        if self.conn.in_transaction:
            self.conn.commit()
        config = self.chunking_config()
        config_meta = chunking_to_meta(config)

        def _emit(phase: str, done: int, total: int, message: str) -> None:
            if progress is not None:
                progress({"phase": phase, "done": done, "total": total, "message": message})

        documents = self.count("documents")
        candidates = self._rechunk_candidates([config])
        total = len(candidates)
        _emit("scan", total, total, f"{_plural(total, 'note')} need new passages")
        rebuilt = written = embedded = skipped = 0
        phase = "embed" if self.embedder is not None else "write"
        try:
            for part in _chunked(candidates, REBUILD_BATCH_DOCS):
                if cancel is not None and cancel():
                    raise OperationCancelled(
                        "rebuilding vectors was cancelled; notes finished so far keep "
                        "their new passages"
                    )
                rows = self.conn.execute(
                    f"SELECT doc_id, title, body FROM documents "
                    f"WHERE doc_id IN ({_placeholders(part)}) ORDER BY doc_id",  # noqa: S608
                    part,
                ).fetchall()
                plans = [
                    (
                        str(row["doc_id"]),
                        str(row["title"] or ""),
                        str(row["body"] or ""),
                        split_passages(str(row["body"] or ""), config),
                    )
                    for row in rows
                ]
                vectors = None
                if self.embedder is not None:
                    vectors = self._passage_vectors(
                        [(doc_id, title, passages) for doc_id, title, _body, passages in plans]
                    )
                vec_table = self._vec_table_exists()
                try:
                    self.conn.execute("BEGIN IMMEDIATE")
                    for position, (doc_id, title, body, passages) in enumerate(plans):
                        current = self.conn.execute(
                            "SELECT title, body FROM documents WHERE doc_id = ?", (doc_id,)
                        ).fetchone()
                        if current is None or (
                            str(current["title"] or ""), str(current["body"] or "")
                        ) != (title, body):
                            # Removed or rewritten by another writer meanwhile;
                            # that write already used the current setting.
                            skipped += 1
                            continue
                        row_ids = self._write_passages(
                            doc_id,
                            passages,
                            drop_vectors=vec_table and self.vec_ready and vectors is None,
                        )
                        if vectors is not None:
                            self._store_vectors(row_ids, vectors[position])
                            embedded += len(row_ids)
                        rebuilt += 1
                        written += len(row_ids)
                    self.conn.commit()
                except BaseException:
                    self.conn.rollback()
                    raise
                finally:
                    self._invalidate_vector_cache()
                _emit(
                    phase,
                    min(total, rebuilt + skipped),
                    total,
                    f"Rebuilt passages for {_plural(rebuilt, 'note')}",
                )
            if self.embedder is not None and self._vec_table_exists():
                # Passages that had no vector at all (e.g. a cancelled backfill).
                embedded += self._embed_missing_vectors(cancel=cancel, commit_batches=True)
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                _now, current_meta, _built = self._chunking_metas()
                # Mark the collection rebuilt only if the setting did not change
                # meanwhile and no note was (re)written under an older one.
                if current_meta == config_meta and not self._rechunk_candidates([config]):
                    self._write_meta(META_CHUNKING_BUILT, config_meta)
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise
        finally:
            self._invalidate_vector_cache()
        _emit("done", total, total, f"Rebuilt passages for {_plural(rebuilt, 'note')}")
        return {
            "documents": documents,
            "rebuilt": rebuilt,
            "passages": written,
            "vectors_embedded": embedded,
            "skipped": skipped,
            "chunking": config,
            "complete": True,
        }

    # -- reads ---------------------------------------------------------------

    def count(self, what: str) -> int:
        table = {
            "documents": "documents",
            "chunks": "chunks",
            "fts": "fts",
            "vectors": "vec_float",
        }[what]
        return int(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608

    def search(self, query: str, mode: str = "hybrid", **options) -> list:
        """Convenience wrapper around :func:`onefind.search.search`."""
        from .search import search

        return search(self, query, mode=mode, **options)

    def has_vectors(self) -> bool:
        return self.get_meta("model_name") is not None

    def health(self) -> dict:
        """Return count-level consistency checks for documents and vectors.

        The original keys count **notes** (documents), so callers that compare
        ``documents == chunks == fts`` keep working when long notes are split
        into several passages: ``chunks`` is the number of notes that have
        search passages, ``vectors`` the number of notes with at least one
        vector and ``missing_vectors`` the notes with a passage that has no
        vector. Row-level counts are in ``passages``, ``passage_vectors`` and
        ``missing_passage_vectors``; ``orphan_vectors`` stays a row count.
        ``ok`` requires: every note has >= 1 passage, no passage without a
        note, one FTS row per note, and (with vectors) a vector for every
        passage and none without a passage.
        """
        documents = self.count("documents")
        passages, noted = self.conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT doc_id) FROM chunks"
        ).fetchone()
        passages, noted = int(passages or 0), int(noted or 0)
        orphan_passages = int(
            self.conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE doc_id NOT IN (SELECT doc_id FROM documents)"
            ).fetchone()[0]
        )
        without_passages = int(
            self.conn.execute(
                "SELECT COUNT(*) FROM documents d "
                "WHERE NOT EXISTS (SELECT 1 FROM chunks c WHERE c.doc_id = d.doc_id)"
            ).fetchone()[0]
        )
        fts = self.count("fts")
        report = {
            "documents": documents,
            "chunks": noted,
            "fts": fts,
            "vectors": None,
            "missing_vectors": 0,
            "orphan_vectors": 0,
            "ok": documents == noted == fts and not orphan_passages and not without_passages,
            "passages": passages,
            "documents_without_passages": without_passages,
            "orphan_passages": orphan_passages,
            "chunking": self.chunking_config(),
        }
        if self.has_vectors() and not self.vec_ready:
            # The vectors may be fine; this interpreter just cannot read them.
            # Keyword search keeps working, so this is not index damage.
            report["vectors_unavailable"] = True
        elif self.has_vectors():
            vector_rows = self.count("vectors")
            with_vectors, missing_docs, missing_rows = self.conn.execute(
                """
                SELECT COALESCE(SUM(missing < total), 0),
                       COALESCE(SUM(missing > 0), 0),
                       COALESCE(SUM(missing), 0)
                FROM (
                    -- NOT IN materializes the vector row ids once; a LEFT JOIN
                    -- would probe the vec0 table row by row (~100x slower).
                    SELECT c.doc_id, COUNT(*) AS total,
                           SUM(c.row_id NOT IN (SELECT rowid FROM vec_float)) AS missing
                    FROM chunks c
                    GROUP BY c.doc_id
                )
                """
            ).fetchone()
            orphan = int(
                self.conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM vec_float v
                    LEFT JOIN chunks c ON c.row_id = v.rowid
                    WHERE c.row_id IS NULL
                    """
                ).fetchone()[0]
            )
            with_vectors, missing_docs = int(with_vectors), int(missing_docs)
            report.update(
                vectors=with_vectors,
                missing_vectors=missing_docs,
                orphan_vectors=orphan,
                passage_vectors=vector_rows,
                missing_passage_vectors=int(missing_rows),
                ok=report["ok"]
                and with_vectors == noted
                and missing_docs == 0
                and orphan == 0,
            )
            long_documents = self._long_document_count()
            if long_documents is not None:
                report["long_documents"] = long_documents
        return report

    def _long_document_count(self) -> int | None:
        """Approximate count of notes the model cannot read in full.

        The model only reads the first ``model_max_tokens`` tokens of each
        embedded text (title + passage), so a note with a longer passage is
        partly matched by keywords only. With passage chunking on, long notes
        are split and this is normally 0. Uses ~4 characters per token.
        """
        raw = self.get_meta("model_max_tokens")
        try:
            tokens = int(raw) if raw else 0
        except ValueError:
            tokens = 0
        if tokens <= 0:
            return None
        return int(
            self.conn.execute(
                """
                SELECT COUNT(DISTINCT c.doc_id)
                FROM chunks c JOIN documents d ON d.doc_id = c.doc_id
                WHERE length(COALESCE(d.title, '')) + length(c.text) > ?
                """,
                (tokens * 4,),
            ).fetchone()[0]
        )

    def last_indexed_at(self) -> str | None:
        """Most recent successful indexing time (ISO, UTC) or None."""
        row = self.conn.execute(
            """
            SELECT MAX(value) FROM (
                SELECT MAX(last_indexed_at) AS value FROM collection_roots
                UNION ALL
                SELECT MAX(finished_at) FROM index_runs WHERE status = 'succeeded'
            )
            """
        ).fetchone()
        return row[0] if row and row[0] else None

    def stats(self) -> dict:
        info = {
            "path": str(self.path),
            "db_bytes": self.path.stat().st_size if self.path.exists() else 0,
            "documents": self.count("documents"),
            "chunks": self.count("chunks"),
        }
        if self.has_vectors():
            if self.vec_ready:
                info["vectors"] = self.count("vectors")
            else:
                info["vectors"] = None
                info["vectors_unavailable"] = True
            info["model"] = self.get_meta("model_name")
            info["model_dim"] = int(self.get_meta("model_dim") or 0)
            info["int8_calibrated"] = self.get_meta("int8_scale") is not None
        info["health"] = self.health()
        info["last_indexed_at"] = self.last_indexed_at()
        info["roots"] = int(
            self.conn.execute("SELECT COUNT(*) FROM collection_roots").fetchone()[0]
        )
        info["collection_name"] = self.get_meta("collection_name")
        # Passage chunking (v1.2.1, additive): rows, setting, long notes and
        # whether stored passages/vectors still follow an older setting.
        census = self._passage_census()
        info["passages"] = info["chunks"]
        info["chunking"] = census["chunking"]
        info["long_notes"] = census["long_notes"]
        info["vectors_need_rebuild"] = self.vectors_need_rebuild()
        return info

    # -- documents & metadata -----------------------------------------------

    def get_document(self, doc_id: str) -> dict | None:
        """Full stored record for one document, or None when unknown."""
        row = self.conn.execute(
            """
            SELECT d.doc_id, d.title, d.body, d.source, d.folder, d.ext, d.mtime,
                   d.size_bytes, d.tags_json, d.content_hash, d.root_id,
                   d.created_at, d.updated_at,
                   EXISTS(SELECT 1 FROM bookmarks b WHERE b.doc_id = d.doc_id)
                       AS bookmarked
            FROM documents d
            WHERE d.doc_id = ?
            """,
            (str(doc_id),),
        ).fetchone()
        if row is None:
            return None
        return {
            "doc_id": str(row["doc_id"]),
            "title": str(row["title"] or ""),
            "body": str(row["body"] or ""),
            "source": row["source"],
            "folder": row["folder"],
            "ext": row["ext"],
            "mtime": row["mtime"],
            "size_bytes": row["size_bytes"],
            "tags": _loads_json(row["tags_json"], []) or [],
            "content_hash": row["content_hash"],
            "root_id": row["root_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "bookmarked": bool(row["bookmarked"]),
        }

    def _require_document(self, doc_id: str) -> None:
        if (
            self.conn.execute(
                "SELECT 1 FROM documents WHERE doc_id = ?", (str(doc_id),)
            ).fetchone()
            is None
        ):
            raise DataError(f"document not found in this collection: {doc_id!r}")

    def fingerprints(self, root_id: int | None = None) -> dict[str, str | None]:
        """doc_id -> content_hash, optionally restricted to one collection root."""
        if root_id is None:
            rows = self.conn.execute("SELECT doc_id, content_hash FROM documents")
        else:
            rows = self.conn.execute(
                "SELECT doc_id, content_hash FROM documents WHERE root_id = ?",
                (int(root_id),),
            )
        return {str(row[0]): row[1] for row in rows}

    def document_manifest(self, root_id: int | None = None) -> dict[str, dict]:
        """doc_id -> stored change-detection fields (hash, mtime, size, root...).

        Used by incremental ingest and ``scan_changes``; it never loads bodies.
        """
        sql = (
            "SELECT doc_id, content_hash, mtime, size_bytes, root_id, title, "
            "source, meta_json FROM documents"
        )
        params: tuple = ()
        if root_id is not None:
            sql += " WHERE root_id = ?"
            params = (int(root_id),)
        return {
            str(row["doc_id"]): {
                "content_hash": row["content_hash"],
                "mtime": row["mtime"],
                "size_bytes": row["size_bytes"],
                "root_id": row["root_id"],
                "title": row["title"],
                "source": row["source"],
                "meta_json": row["meta_json"],
            }
            for row in self.conn.execute(sql, params)
        }

    def duplicates_of(self, doc_id: str) -> list[str]:
        """Other doc_ids whose content hash equals this document's."""
        rows = self.conn.execute(
            """
            SELECT other.doc_id
            FROM documents me
            JOIN documents other
              ON other.content_hash = me.content_hash AND other.doc_id <> me.doc_id
            WHERE me.doc_id = ? AND me.content_hash IS NOT NULL
            ORDER BY other.doc_id
            """,
            (str(doc_id),),
        ).fetchall()
        return [str(row[0]) for row in rows]

    def duplicate_groups(
        self, limit: int = 50, *, root_id: int | None = None
    ) -> list[list[str]]:
        """Groups of doc_ids with identical content, largest groups first.

        ``root_id`` keeps only groups that include a document from that root.
        """
        limit = _clamp_limit(limit, 10_000)
        having = "COUNT(*) > 1"
        params: list[Any] = []
        if root_id is not None:
            having += " AND SUM(CASE WHEN root_id = ? THEN 1 ELSE 0 END) > 0"
            params.append(int(root_id))
        params.append(limit)
        rows = self.conn.execute(
            f"""
            SELECT json_group_array(doc_id) AS ids
            FROM documents
            WHERE content_hash IS NOT NULL
            GROUP BY content_hash
            HAVING {having}
            ORDER BY COUNT(*) DESC, MIN(doc_id) ASC
            LIMIT ?
            """,  # noqa: S608 - fixed SQL fragments only
            params,
        ).fetchall()
        return [sorted(str(item) for item in json.loads(row["ids"])) for row in rows]

    def facets(self) -> dict:
        """Counts for the filter panel: folders, file types, tags, date range."""
        folders = [
            {"folder": str(row[0]), "count": int(row[1])}
            for row in self.conn.execute(
                """
                SELECT folder, COUNT(*) FROM documents
                WHERE folder IS NOT NULL
                GROUP BY folder ORDER BY COUNT(*) DESC, folder ASC
                """
            )
        ]
        types = [
            {"ext": str(row[0]), "count": int(row[1])}
            for row in self.conn.execute(
                """
                SELECT ext, COUNT(*) FROM documents
                WHERE ext IS NOT NULL AND ext <> ''
                GROUP BY ext ORDER BY COUNT(*) DESC, ext ASC
                """
            )
        ]
        tags = [
            {"tag": str(row[0]), "count": int(row[1])}
            for row in self.conn.execute(
                """
                SELECT tag.value, COUNT(DISTINCT d.doc_id)
                FROM documents d, json_each(d.tags_json) AS tag
                WHERE d.tags_json IS NOT NULL AND json_valid(d.tags_json)
                  AND tag.type = 'text'
                GROUP BY tag.value ORDER BY COUNT(DISTINCT d.doc_id) DESC, tag.value ASC
                """
            )
        ]
        low, high = self.conn.execute(
            "SELECT MIN(mtime), MAX(mtime) FROM documents"
        ).fetchone()
        return {
            "folders": folders,
            "types": types,
            "tags": tags,
            "modified": {
                "min": float(low) if low is not None else None,
                "max": float(high) if high is not None else None,
            },
        }

    # -- collection roots ----------------------------------------------------

    def _roots_files_only_column(self) -> bool:
        cached = getattr(self, "_files_only_column", None)
        if cached is None:
            cached = "files_only" in _root_columns(self.conn)
            self._files_only_column = cached
        return cached

    def ensure_root(
        self,
        path: str | Path,
        exclude: list[str] | None = None,
        *,
        files_only: bool | None = None,
    ) -> int:
        """Register a source folder (idempotent) and return its root_id.

        ``exclude=None`` keeps any stored exclude patterns; a list replaces them.
        ``files_only=True`` marks a *new* root as registered by indexing single
        files (change detection then only looks at those files);
        ``files_only=False`` turns an existing root into a full folder root;
        ``None`` leaves the flag alone.
        """
        key = str(path).strip()
        if not key:
            raise UsageError("collection root path is empty")
        exclude_json = None
        if exclude is not None:
            if isinstance(exclude, str) or not all(isinstance(p, str) for p in exclude):
                raise UsageError("exclude must be a list of glob strings")
            exclude_json = json.dumps(list(exclude))
        flag_column = self._roots_files_only_column()
        try:
            row = self.conn.execute(
                "SELECT root_id FROM collection_roots WHERE path = ?", (key,)
            ).fetchone()
            if row is not None:
                root_id = int(row[0])
                if exclude is not None:
                    self.conn.execute(
                        "UPDATE collection_roots SET exclude_json = ? WHERE root_id = ?",
                        (exclude_json, root_id),
                    )
                if files_only is False and flag_column:
                    self.conn.execute(
                        "UPDATE collection_roots SET files_only = NULL WHERE root_id = ?",
                        (root_id,),
                    )
            else:
                if flag_column:
                    cursor = self.conn.execute(
                        """
                        INSERT INTO collection_roots (path, exclude_json, added_at, files_only)
                        VALUES (?, ?, ?, ?)
                        """,
                        (key, exclude_json, utcnow_iso(), 1 if files_only else None),
                    )
                else:
                    cursor = self.conn.execute(
                        """
                        INSERT INTO collection_roots (path, exclude_json, added_at)
                        VALUES (?, ?, ?)
                        """,
                        (key, exclude_json, utcnow_iso()),
                    )
                root_id = int(cursor.lastrowid)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return root_id

    def _root_dict(self, row: sqlite3.Row) -> dict:
        keys = row.keys()
        return {
            "root_id": int(row["root_id"]),
            "path": str(row["path"]),
            "exclude": _loads_json(row["exclude_json"], []) or [],
            "added_at": row["added_at"],
            "last_indexed_at": row["last_indexed_at"],
            "documents": int(row["documents"]),
            "files_only": bool(row["files_only"]) if "files_only" in keys else False,
        }

    def _roots_sql(self, where: str = "") -> str:
        flag = ", r.files_only" if self._roots_files_only_column() else ""
        return f"""
            SELECT r.root_id, r.path, r.exclude_json, r.added_at, r.last_indexed_at{flag},
                   (SELECT COUNT(*) FROM documents d WHERE d.root_id = r.root_id)
                       AS documents
            FROM collection_roots r
            {where}
            ORDER BY r.root_id
            """  # noqa: S608 - fixed SQL fragments only

    def roots(self) -> list[dict]:
        """Registered source folders with their document counts."""
        rows = self.conn.execute(self._roots_sql()).fetchall()
        return [self._root_dict(row) for row in rows]

    def get_root(self, path: str | Path) -> dict | None:
        """The registered root with exactly this path, or None."""
        row = self.conn.execute(
            self._roots_sql("WHERE r.path = ?"), (str(path).strip(),)
        ).fetchone()
        return self._root_dict(row) if row is not None else None

    def get_root_by_id(self, root_id: int) -> dict | None:
        """The registered root with this id, or None."""
        try:
            identifier = int(root_id)
        except (TypeError, ValueError):
            return None
        row = self.conn.execute(
            self._roots_sql("WHERE r.root_id = ?"), (identifier,)
        ).fetchone()
        return self._root_dict(row) if row is not None else None

    def remove_root(self, root_id: int) -> bool:
        """Forget a root registration; its documents stay but lose root_id."""
        try:
            self.conn.execute(
                "UPDATE documents SET root_id = NULL WHERE root_id = ?", (int(root_id),)
            )
            cursor = self.conn.execute(
                "DELETE FROM collection_roots WHERE root_id = ?", (int(root_id),)
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return cursor.rowcount > 0

    def forget_root(self, root_id: int, *, delete_documents: bool = True) -> dict:
        """Unregister a folder and (by default) remove its notes from the index.

        Used when a folder was moved or deleted: its documents, chunks, keyword
        and vector rows, bookmarks and activity are removed in one
        transaction; files on disk are never touched. With
        ``delete_documents=False`` the notes stay searchable without a root.
        Returns ``{"removed": n, "forgotten": bool, "root": path | None}``.
        """
        root = self.get_root_by_id(root_id)
        if root is None:
            return {"removed": 0, "forgotten": False, "root": None}
        identifier = int(root["root_id"])
        doc_ids = list(self.fingerprints(identifier)) if delete_documents else []
        if doc_ids and self._vec_table_exists() and not self.vec_ready:
            raise EnvError(
                "sqlite-vec unavailable in this interpreter, so stored vectors "
                "cannot be removed - run 'OneFind check' for the fix"
            )
        if self.conn.in_transaction:
            self.conn.commit()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            removed = self._delete_documents(doc_ids)
            self.conn.execute(
                "UPDATE documents SET root_id = NULL WHERE root_id = ?", (identifier,)
            )
            cursor = self.conn.execute(
                "DELETE FROM collection_roots WHERE root_id = ?", (identifier,)
            )
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        finally:
            self._invalidate_vector_cache()
        return {"removed": removed, "forgotten": cursor.rowcount > 0, "root": root["path"]}

    def relocate_root(self, root_id: int, new_path: str | Path) -> dict:
        """Point a registered folder at its new location (it was moved).

        Sources are stored relative to the root, so every note keeps its
        doc_id, bookmarks and vectors; re-index the folder afterwards to pick
        up edits. Raises UsageError when another root already has that path.
        """
        root = self.get_root_by_id(root_id)
        if root is None:
            raise DataError(f"collection folder not found: {root_id!r}")
        key = str(new_path).strip()
        if not key:
            raise UsageError("collection root path is empty")
        other = self.get_root(key)
        if other is not None and other["root_id"] != root["root_id"]:
            raise UsageError(f"{key} is already a folder of this collection")
        try:
            self.conn.execute(
                "UPDATE collection_roots SET path = ? WHERE root_id = ?",
                (key, int(root["root_id"])),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        updated = self.get_root_by_id(root["root_id"])
        assert updated is not None
        return updated

    def adopt_root(self, child_root_id: int, parent_root_id: int, prefix: str) -> int:
        """Merge a registered sub-folder root into its parent root.

        The child's documents keep their doc_ids (so bookmarks, activity and
        vectors survive) but now belong to the parent, with ``source`` and
        ``folder`` rewritten relative to it. Returns the number of documents
        moved. ``prefix`` is the child's path relative to the parent (posix).
        """
        clean = str(prefix).strip("/")
        if not clean:
            raise UsageError("prefix of the adopted folder is empty")
        try:
            cursor = self.conn.execute(
                """
                UPDATE documents
                SET root_id = ?,
                    source  = ? || '/' || source,
                    folder  = CASE WHEN folder IS NULL OR folder = '' THEN ?
                                   ELSE ? || '/' || folder END
                WHERE root_id = ? AND source IS NOT NULL
                """,
                (int(parent_root_id), clean, clean, clean, int(child_root_id)),
            )
            moved = max(0, cursor.rowcount)
            self.conn.execute(
                "UPDATE documents SET root_id = NULL WHERE root_id = ?", (int(child_root_id),)
            )
            self.conn.execute(
                "DELETE FROM collection_roots WHERE root_id = ?", (int(child_root_id),)
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return int(moved)

    def move_user_rows(self, pairs: Iterable[tuple[str, str]]) -> int:
        """Carry bookmarks and open-activity from old doc_ids to new ones.

        Used when a note was renamed or moved inside its folder, so pruning
        the old doc_id does not silently delete what the user wrote. A row
        already present on the new id wins. Returns the number of rows moved.
        """
        moved = 0
        try:
            for old, new in pairs:
                if old == new:
                    continue
                for table in ("bookmarks", "doc_activity"):
                    cursor = self.conn.execute(
                        f"UPDATE OR IGNORE {table} SET doc_id = ? WHERE doc_id = ?",  # noqa: S608
                        (str(new), str(old)),
                    )
                    moved += max(0, cursor.rowcount)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return moved

    def mark_root_indexed(self, root_id: int) -> None:
        self.conn.execute(
            "UPDATE collection_roots SET last_indexed_at = ? WHERE root_id = ?",
            (utcnow_iso(), int(root_id)),
        )
        self.conn.commit()

    # -- search history ------------------------------------------------------

    @staticmethod
    def _history_dict(row: sqlite3.Row) -> dict:
        return {
            "id": int(row["id"]),
            "query": str(row["query"]),
            "mode": str(row["mode"]),
            "precision": row["precision"],
            "filters": _loads_json(row["filters_json"]),
            "result_count": int(row["result_count"]),
            "searched_at": row["searched_at"],
        }

    def record_search(
        self,
        query: str,
        mode: str,
        precision: str | None,
        filters: dict | None,
        result_count: int,
    ) -> None:
        """Append to search history (blank queries ignored, capped at 200 rows)."""
        text = str(query or "").strip()
        if not text:
            return
        try:
            self.conn.execute(
                """
                INSERT INTO search_history
                    (query, mode, precision, filters_json, result_count, searched_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    text[:MAX_QUERY_CHARS],
                    str(mode),
                    precision,
                    _dumps_filters(filters),
                    max(0, int(result_count)),
                    utcnow_iso(),
                ),
            )
            self.conn.execute(
                """
                DELETE FROM search_history
                WHERE id NOT IN (
                    SELECT id FROM search_history ORDER BY id DESC LIMIT ?
                )
                """,
                (HISTORY_LIMIT,),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def recent_searches(self, limit: int = 10) -> list[dict]:
        """Newest searches first, one entry per (query, mode) pair."""
        rows = self.conn.execute(
            """
            SELECT h.*
            FROM search_history h
            JOIN (
                SELECT MAX(id) AS id FROM search_history GROUP BY query, mode
            ) latest ON latest.id = h.id
            ORDER BY h.id DESC
            LIMIT ?
            """,
            (_clamp_limit(limit, HISTORY_LIMIT),),
        ).fetchall()
        return [self._history_dict(row) for row in rows]

    def clear_history(self) -> int:
        cursor = self.conn.execute("DELETE FROM search_history")
        self.conn.commit()
        return int(cursor.rowcount)

    # -- saved queries ---------------------------------------------------------

    @staticmethod
    def _saved_dict(row: sqlite3.Row) -> dict:
        return {
            "id": int(row["id"]),
            "name": str(row["name"]),
            "query": str(row["query"]),
            "mode": str(row["mode"]),
            "precision": row["precision"],
            "filters": _loads_json(row["filters_json"]),
            "created_at": row["created_at"],
        }

    def save_query(
        self,
        name: str,
        query: str,
        mode: str,
        precision: str | None = None,
        filters: dict | None = None,
    ) -> dict:
        """Save a named query; saving an existing name (any case) replaces it."""
        clean_name = str(name or "").strip()
        clean_query = str(query or "").strip()
        if not clean_name:
            raise UsageError("saved query name is empty")
        if len(clean_name) > MAX_NAME_CHARS:
            raise UsageError(f"saved query name is longer than {MAX_NAME_CHARS} characters")
        if not clean_query:
            raise UsageError("saved query text is empty")
        if len(clean_query) > MAX_QUERY_CHARS:
            raise UsageError(f"saved query is longer than {MAX_QUERY_CHARS} characters")
        if mode not in SEARCH_MODES:
            raise UsageError(f"mode must be one of {', '.join(SEARCH_MODES)}")
        if precision is not None and precision not in PRECISIONS:
            raise UsageError(f"precision must be one of {', '.join(PRECISIONS)}")
        filters_json = _dumps_filters(filters)
        try:
            existing = self.conn.execute(
                "SELECT id FROM saved_queries WHERE lower(name) = lower(?)",
                (clean_name,),
            ).fetchone()
            if existing is not None:
                query_id = int(existing[0])
                self.conn.execute(
                    """
                    UPDATE saved_queries
                    SET name = ?, query = ?, mode = ?, precision = ?, filters_json = ?,
                        created_at = ?
                    WHERE id = ?
                    """,
                    # Replacing re-saves it: relative date filters ("last 7 days")
                    # are worked out from this moment, not the first save.
                    (clean_name, clean_query, mode, precision, filters_json, utcnow_iso(),
                     query_id),
                )
            else:
                cursor = self.conn.execute(
                    """
                    INSERT INTO saved_queries
                        (name, query, mode, precision, filters_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (clean_name, clean_query, mode, precision, filters_json, utcnow_iso()),
                )
                query_id = int(cursor.lastrowid)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        row = self.conn.execute(
            "SELECT * FROM saved_queries WHERE id = ?", (query_id,)
        ).fetchone()
        return self._saved_dict(row)

    def saved_queries(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM saved_queries ORDER BY id DESC"
        ).fetchall()
        return [self._saved_dict(row) for row in rows]

    def delete_saved_query(self, query_id: int) -> bool:
        try:
            identifier = int(query_id)
        except (TypeError, ValueError):
            return False
        cursor = self.conn.execute("DELETE FROM saved_queries WHERE id = ?", (identifier,))
        self.conn.commit()
        return cursor.rowcount > 0

    # -- bookmarks & activity --------------------------------------------------

    def add_bookmark(self, doc_id: str, note: str | None = None) -> dict:
        """Bookmark a document (idempotent; a new note replaces the old one)."""
        self._require_document(doc_id)
        clean_note = None if note is None else str(note).strip()[:MAX_NOTE_CHARS] or None
        self.conn.execute(
            """
            INSERT INTO bookmarks (doc_id, note, created_at) VALUES (?, ?, ?)
            ON CONFLICT(doc_id) DO UPDATE SET note = excluded.note
            """,
            (str(doc_id), clean_note, utcnow_iso()),
        )
        self.conn.commit()
        return next(item for item in self.bookmarks() if item["doc_id"] == str(doc_id))

    def remove_bookmark(self, doc_id: str) -> bool:
        cursor = self.conn.execute("DELETE FROM bookmarks WHERE doc_id = ?", (str(doc_id),))
        self.conn.commit()
        return cursor.rowcount > 0

    def bookmarks(self) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT b.doc_id, b.note, b.created_at, d.title, d.source, d.folder
            FROM bookmarks b
            JOIN documents d ON d.doc_id = b.doc_id
            ORDER BY b.created_at DESC, b.rowid DESC
            """
        ).fetchall()
        return [
            {
                "doc_id": str(row["doc_id"]),
                "note": row["note"],
                "created_at": row["created_at"],
                "title": str(row["title"] or ""),
                "source": row["source"],
                "folder": row["folder"],
            }
            for row in rows
        ]

    def is_bookmarked(self, doc_id: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM bookmarks WHERE doc_id = ?", (str(doc_id),)
            ).fetchone()
            is not None
        )

    def record_open(self, doc_id: str) -> None:
        """Note that the user opened a document (feeds recent_documents)."""
        self._require_document(doc_id)
        self.conn.execute(
            """
            INSERT INTO doc_activity (doc_id, last_opened_at, open_count) VALUES (?, ?, 1)
            ON CONFLICT(doc_id) DO UPDATE SET
                last_opened_at = excluded.last_opened_at,
                open_count = doc_activity.open_count + 1
            """,
            (str(doc_id), utcnow_iso()),
        )
        self.conn.commit()

    def recent_documents(self, limit: int = 8) -> list[dict]:
        """Recently opened documents first, then the most recently modified."""
        rows = self.conn.execute(
            """
            SELECT d.doc_id, d.title, d.source, d.folder, d.updated_at, d.mtime,
                   a.last_opened_at, COALESCE(a.open_count, 0) AS open_count
            FROM documents d
            LEFT JOIN doc_activity a ON a.doc_id = d.doc_id
            ORDER BY a.last_opened_at IS NULL, a.last_opened_at DESC,
                     COALESCE(d.mtime, 0) DESC, d.updated_at DESC, d.doc_id ASC
            LIMIT ?
            """,
            (_clamp_limit(limit, 100),),
        ).fetchall()
        return [
            {
                "doc_id": str(row["doc_id"]),
                "title": str(row["title"] or ""),
                "source": row["source"],
                "folder": row["folder"],
                "last_opened_at": row["last_opened_at"],
                "open_count": int(row["open_count"]),
                "updated_at": row["updated_at"],
                "mtime": row["mtime"],
            }
            for row in rows
        ]

    # -- index runs ----------------------------------------------------------

    @staticmethod
    def _run_dict(row: sqlite3.Row) -> dict:
        return {
            "id": int(row["id"]),
            "root_path": row["root_path"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "status": str(row["status"]),
            "added": int(row["added"] or 0),
            "updated": int(row["updated"] or 0),
            "unchanged": int(row["unchanged"] or 0),
            "removed": int(row["removed"] or 0),
            "skipped": int(row["skipped"] or 0),
            "error": row["error"],
        }

    def start_run(self, root_path: str | Path | None) -> int:
        """Record the start of an indexing run and return its id.

        Runs still marked ``running`` were left behind by a process that ended
        before finishing (writers are serialized per collection), so they are
        closed as failed/interrupted first.
        """
        try:
            self.conn.execute(
                """
                UPDATE index_runs
                SET status = 'failed',
                    finished_at = COALESCE(finished_at, ?),
                    error = COALESCE(error, ?)
                WHERE status = 'running'
                """,
                (utcnow_iso(), INTERRUPTED_RUN_ERROR),
            )
            cursor = self.conn.execute(
                "INSERT INTO index_runs (root_path, started_at, status) VALUES (?, ?, 'running')",
                (None if root_path is None else str(root_path), utcnow_iso()),
            )
            run_id = int(cursor.lastrowid)
            self.conn.execute(
                """
                DELETE FROM index_runs
                WHERE status <> 'running'
                  AND id NOT IN (SELECT id FROM index_runs ORDER BY id DESC LIMIT ?)
                """,
                (RUNS_LIMIT,),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return run_id

    def finish_run(
        self,
        run_id: int,
        status: str,
        counts: dict | None = None,
        error: str | None = None,
    ) -> None:
        """Close a run with its final status and added/updated/... counts."""
        if status not in FINISHED_RUN_STATUSES:
            raise UsageError(f"run status must be one of {', '.join(FINISHED_RUN_STATUSES)}")
        counts = counts or {}

        def _count(key: str) -> int:
            value = counts.get(key, 0)
            if isinstance(value, (list, tuple, set, dict)):
                return len(value)
            try:
                return max(0, int(value or 0))
            except (TypeError, ValueError):
                return 0

        try:
            self.conn.execute(
                """
                UPDATE index_runs
                SET finished_at = ?, status = ?, added = ?, updated = ?, unchanged = ?,
                    removed = ?, skipped = ?, error = ?
                WHERE id = ?
                """,
                (
                    utcnow_iso(),
                    status,
                    _count("added"),
                    _count("updated"),
                    _count("unchanged"),
                    _count("removed"),
                    _count("skipped"),
                    None if error is None else str(error)[:2000],
                    int(run_id),
                ),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def last_run(self) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM index_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return self._run_dict(row) if row is not None else None

    # -- maintenance -----------------------------------------------------------

    def backup_to(self, dest: str | Path, *, overwrite: bool = False) -> dict:
        """Write a consistent single-file copy using SQLite's backup API.

        Safe while the collection is in WAL mode or being read. Refuses to
        overwrite an existing file unless ``overwrite=True`` and refuses to
        write onto the collection file itself. Returns ``{path, bytes}``.
        """
        if not str(dest).strip():
            raise UsageError("backup destination is empty")
        target = Path(dest)
        if _same_path(target, self.path):
            raise UsageError(
                "backup destination is the collection file itself; choose another path"
            )
        if target.exists():
            if target.is_dir():
                raise UsageError(f"backup destination is a folder: {target}")
            if not overwrite:
                raise UsageError(
                    f"backup destination already exists: {target} "
                    "(choose a new name or allow overwriting)"
                )
        temp = target.with_name(f".{target.name}.{os.getpid()}.partial")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temp.unlink(missing_ok=True)
            dest_conn = sqlite3.connect(temp)
            try:
                self.conn.backup(dest_conn)
                # The copy should be one self-contained file, not a WAL pair.
                dest_conn.execute("PRAGMA journal_mode=DELETE")
                dest_conn.commit()
            finally:
                dest_conn.close()
            os.replace(temp, target)
        except sqlite3.Error as exc:
            raise DataError(f"backup failed: {exc}") from exc
        except OSError as exc:
            raise DataError(f"cannot write backup to {target}: {exc}") from exc
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
        return {"path": str(target), "bytes": target.stat().st_size}

    def _vector_problem(self) -> str | None:
        if self._vec_table_exists() and not self.vec_ready:
            return (
                "This collection has meaning-search vectors, but the sqlite-vec "
                "extension could not be loaded, so they cannot be checked or used."
            )
        return None

    def integrity_check(self) -> dict:
        """Check database, keyword index, and vectors; explain in plain words."""
        problems: list[str] = []
        suggestions: list[str] = []

        def _suggest(text: str) -> None:
            if text not in suggestions:
                suggestions.append(text)

        repair_hint = "Run Repair to rebuild the keyword index and clean up leftovers."
        # A long-lived connection may hold a stale FTS5 structure cache after
        # another connection wrote; any read of the table refreshes it, so
        # PRAGMA integrity_check does not report that cache as damage.
        try:
            self.conn.execute("SELECT count(*) FROM fts").fetchone()
        except sqlite3.DatabaseError as exc:
            if _is_busy(exc):
                raise  # contention, not damage: callers report "busy, try again"
            # real damage is reported by the checks below
        try:
            messages = [
                str(row[0]) for row in self.conn.execute("PRAGMA integrity_check(20)")
            ]
            sqlite_status = "ok" if messages == ["ok"] else "; ".join(messages)
        except sqlite3.DatabaseError as exc:
            if _is_busy(exc):
                raise
            sqlite_status = str(exc)
        if sqlite_status != "ok":
            problems.append("The collection file reports internal damage.")
            _suggest(
                "Restore the collection from a backup, or re-index your folders "
                "into a new collection."
            )

        try:
            self.conn.execute("INSERT INTO fts(fts) VALUES ('integrity-check')")
            self.conn.commit()
            fts_status = "ok"
        except sqlite3.DatabaseError as exc:
            self.conn.rollback()
            if _is_busy(exc):
                raise  # another connection holds the write lock (e.g. a job)
            fts_status = str(exc)
        if fts_status != "ok":
            problems.append("The keyword index is damaged.")
            _suggest(repair_hint)

        def _scalar(sql: str) -> int:
            try:
                return int(self.conn.execute(sql).fetchone()[0] or 0)
            except sqlite3.DatabaseError:
                return 0

        missing_fts = _scalar(
            "SELECT COUNT(*) FROM documents WHERE doc_id NOT IN (SELECT doc_id FROM fts)"
        )
        extra_fts = _scalar(
            "SELECT COUNT(*) FROM fts WHERE doc_id NOT IN (SELECT doc_id FROM documents)"
        ) + _scalar("SELECT COUNT(*) - COUNT(DISTINCT doc_id) FROM fts")
        # Notes with no passages, or passages that match neither the current
        # passage setting nor the one stored notes follow (a pending rebuild
        # is not damage).
        try:
            out_of_date = len(self._rechunk_candidates(self._accepted_chunkings()))
        except sqlite3.DatabaseError as exc:
            if _is_busy(exc):
                raise
            out_of_date = 0
        orphan_passages = _scalar(
            "SELECT COUNT(*) FROM chunks WHERE doc_id NOT IN (SELECT doc_id FROM documents)"
        )
        if missing_fts:
            problems.append(
                f"{_plural(missing_fts, 'document')} cannot be found by keyword search "
                "because they are missing from the keyword index."
            )
            _suggest(repair_hint)
        if extra_fts:
            problems.append(
                f"{_plural(extra_fts, 'keyword-index entry', 'keyword-index entries')} "
                "point to documents that are no longer in the collection."
            )
            _suggest(repair_hint)
        if out_of_date:
            problems.append(
                f"{_plural(out_of_date, 'document')} have out-of-date search data."
            )
            _suggest(repair_hint)
        if orphan_passages:
            problems.append(
                f"{_plural(orphan_passages, 'search passage')} belong to documents "
                "that were removed."
            )
            _suggest(repair_hint)

        health: dict | None
        vector_problem = self._vector_problem()
        if vector_problem:
            problems.append(vector_problem)
            _suggest("Run 'onefind check' to fix the vector extension; keyword search still works.")
            health = None
        else:
            health = self.health()
            if health["missing_vectors"]:
                problems.append(
                    f"{_plural(health['missing_vectors'], 'document')} have no "
                    "meaning vector, so semantic and hybrid search will skip them."
                )
                _suggest(
                    "Run Repair with the embedding model available, or re-index "
                    "with embeddings turned on."
                )
            if health["orphan_vectors"]:
                problems.append(
                    f"{_plural(health['orphan_vectors'], 'stored vector')} belong to "
                    "documents that were removed."
                )
                _suggest(repair_hint)

        return {
            "ok": not problems,
            "sqlite": sqlite_status,
            "fts": fts_status,
            "health": health,
            "problems": problems,
            "suggestions": suggestions,
        }

    def repair(self) -> dict:
        """Rebuild derived data from ``documents`` (the source of truth).

        Rewrites chunk text and the FTS table, drops orphan rows and vectors,
        and re-embeds missing vectors when an embedder is attached.
        """
        vec_table = self._vec_table_exists()
        vector_problem = self._vector_problem()
        report = {
            "fts_rebuilt": 0,
            "chunks_fixed": 0,
            "orphan_rows_removed": 0,
            "orphan_vectors_removed": 0,
            "vectors_embedded": 0,
            "vectors_missing": None,
        }
        vec_usable = vec_table and self.vec_ready
        if self.conn.in_transaction:
            self.conn.commit()
        try:
            self.conn.execute("BEGIN IMMEDIATE")

            def _drop_vectors(row_ids: list[int]) -> None:
                if vec_usable:
                    for row_id in row_ids:
                        self.conn.execute("DELETE FROM vec_float WHERE rowid = ?", (row_id,))

            orphan_chunks = [
                int(row[0])
                for row in self.conn.execute(
                    "SELECT row_id FROM chunks WHERE doc_id NOT IN (SELECT doc_id FROM documents)"
                )
            ]
            _drop_vectors(orphan_chunks)
            for part in _chunked(orphan_chunks):
                self.conn.execute(
                    f"DELETE FROM chunks WHERE row_id IN ({_placeholders(part)})", part  # noqa: S608
                )
            # Notes whose passages are missing or match no known passage
            # setting are re-split under the current one; their vectors are
            # stale too and are re-embedded below.
            mismatched = self._rechunk_candidates(self._accepted_chunkings())
            chunking = self.chunking_config()
            for part in _chunked(mismatched):
                for row in self.conn.execute(
                    f"SELECT doc_id, body FROM documents WHERE doc_id IN ({_placeholders(part)})",  # noqa: S608
                    part,
                ).fetchall():
                    self._write_passages(
                        str(row["doc_id"]),
                        split_passages(str(row["body"] or ""), chunking),
                        drop_vectors=vec_usable,
                    )
            report["chunks_fixed"] = len(orphan_chunks) + len(mismatched)

            self.conn.execute("DELETE FROM fts")
            report["fts_rebuilt"] = self.conn.execute(
                "INSERT INTO fts (doc_id, title, body) "
                "SELECT doc_id, title, body FROM documents ORDER BY doc_id"
            ).rowcount

            orphan_rows = 0
            for table in ("bookmarks", "doc_activity"):
                orphan_rows += self.conn.execute(
                    f"DELETE FROM {table} WHERE doc_id NOT IN (SELECT doc_id FROM documents)"  # noqa: S608
                ).rowcount
            orphan_rows += self.conn.execute(
                """
                UPDATE documents SET root_id = NULL
                WHERE root_id IS NOT NULL
                  AND root_id NOT IN (SELECT root_id FROM collection_roots)
                """
            ).rowcount
            report["orphan_rows_removed"] = orphan_rows

            if vec_usable:
                orphan_vectors = [
                    int(row[0])
                    for row in self.conn.execute(
                        """
                        SELECT v.rowid FROM vec_float v
                        LEFT JOIN chunks c ON c.row_id = v.rowid
                        WHERE c.row_id IS NULL
                        """
                    )
                ]
                _drop_vectors(orphan_vectors)
                report["orphan_vectors_removed"] = len(orphan_vectors)
                if self.embedder is not None:
                    report["vectors_embedded"] = self._embed_missing_vectors()
                # Notes (not passages) still without a complete set of vectors.
                report["vectors_missing"] = len(
                    {str(row["doc_id"]) for row in self._missing_vector_rows()}
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            self._invalidate_vector_cache()

        report["health"] = None if vector_problem else self.health()
        report["ok"] = bool(report["health"] and report["health"]["ok"])
        if vector_problem:
            report["problem"] = vector_problem
        return report

    def compact(self) -> dict:
        """Checkpoint the WAL and VACUUM; returns on-disk bytes before/after."""
        before = _db_footprint(self.path)
        if self.conn.in_transaction:
            self.conn.commit()
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.conn.execute("VACUUM")
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError as exc:
            raise DataError(
                f"could not compact the collection (it may be busy in another "
                f"program; try again): {exc}"
            ) from exc
        finally:
            self._invalidate_vector_cache()
        return {"before": before, "after": _db_footprint(self.path)}
