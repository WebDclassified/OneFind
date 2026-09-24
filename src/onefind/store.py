"""SQLite storage layer: connection, schema, transactional document upserts.

Schema follows docs/05-backend-design.md. V1 indexes whole documents
(one chunk per doc) so the BEIR evaluation protocol stays honest; the
chunks table owns stable row_ids that the Phase-2 vec tables key off.

Vector storage is created lazily on first embedder attachment because its DDL
depends on the model's dimension:
    vec_float(embedding float[dim] distance_metric=cosine)

Float search uses sqlite-vec KNN. Int8 and binary precision are derived from the
stored float vectors in application code per ADR-7.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from .errors import DataError, EnvError, UsageError

if TYPE_CHECKING:  # pragma: no cover
    from .embed import SentenceEmbedder

SCHEMA_VERSION = 2
BATCH_SIZE = 256
DEFAULT_INT8_SCALE = 1.0 / 127.0


def utcnow_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


@dataclass
class Document:
    doc_id: str
    body: str
    title: str = ""
    source: str | None = None
    meta: dict | None = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id     TEXT PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT '',
    body       TEXT NOT NULL,
    source     TEXT,
    meta_json  TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
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

INSERT INTO schema_meta(key, value) VALUES ('schema_version', '2')
    ON CONFLICT(key) DO NOTHING;

CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    doc_id UNINDEXED,
    title,
    body,
    tokenize = 'porter unicode61'
);
"""


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


class Index:
    """Owns the SQLite connection; all writes go through add_documents()."""

    def __init__(self, conn: sqlite3.Connection, path: str | Path) -> None:
        self.conn = conn
        self.path = Path(path)
        self.vec_ready = _try_load_vec(conn)
        self.embedder: "SentenceEmbedder | None" = None
        self._vector_cache: tuple["object", list[int]] | None = None
        self._vector_cache_version: int | None = None

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def open(cls, path: str | Path) -> "Index":
        target = Path(path)
        new_database = not target.exists() or target.stat().st_size == 0
        try:
            conn = sqlite3.connect(target, check_same_thread=False)
        except OSError as exc:
            raise DataError(f"cannot open database at {target}: {exc}") from exc
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")

        try:
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
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
                if version != SCHEMA_VERSION:
                    raise DataError(
                        f"unsupported schema version {version} in {target}; "
                        f"this build supports {SCHEMA_VERSION}. Rebuild the index."
                    )
                required = {"documents", "chunks", "schema_meta", "fts"}
                missing = required - tables
                if missing:
                    raise DataError(
                        f"OneFind index is missing required tables: {sorted(missing)}"
                    )
            else:
                conn.executescript(_SCHEMA)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                conn.commit()

            conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            conn.close()
            if new_database:
                target.unlink(missing_ok=True)
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

    # -- embedder attachment ---------------------------------------------------

    def attach_embedder(self, embedder: "SentenceEmbedder") -> None:
        """Bind an embedding model and lazily materialize the vec tables."""
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
            self.embedder = embedder

            # Backfill a lexical-only index automatically. Existing vector rows
            # are retained, while renamed/new chunks receive fresh embeddings.
            missing = self.conn.execute(
                """
                SELECT c.row_id, c.text, d.title
                FROM chunks c
                JOIN documents d ON d.doc_id = c.doc_id
                LEFT JOIN vec_float v ON v.rowid = c.row_id
                WHERE v.rowid IS NULL
                ORDER BY c.row_id
                """
            ).fetchall()
            if missing:
                from .embed import compose_embed_text

                self._sync_vectors(
                    [
                        (
                            int(row["row_id"]),
                            compose_embed_text(str(row["title"] or ""), str(row["text"] or "")),
                        )
                        for row in missing
                    ]
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            self.embedder = None
            self._vector_cache = None
            self._vector_cache_version = None
            raise

    # -- writes ------------------------------------------------------------

    def add_documents(self, docs: Iterable[Document]) -> int:
        """Upsert documents (+fts+chunk[+vector] rows) in committed batches.

        Returns the number of documents processed. A failure part-way
        leaves earlier batches committed and the database openable.
        """
        if self.embedder is None and self.has_vectors():
            raise UsageError(
                "this index already contains embeddings; re-run indexing with "
                "--embed so document and vector data stay synchronized"
            )

        processed = 0
        batch: list[Document] = []

        def _flush(batch: list[Document]) -> None:
            try:
                if self.embedder is not None:
                    from .embed import compose_embed_text

                now = utcnow_iso()
                chunk_rows: list[tuple[int, str]] = []
                for document in batch:
                    meta_json = (
                        json.dumps(document.meta, sort_keys=True)
                        if document.meta
                        else None
                    )
                    self.conn.execute(
                        """
                        INSERT INTO documents
                            (doc_id, title, body, source, meta_json, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(doc_id) DO UPDATE SET
                            title      = excluded.title,
                            body       = excluded.body,
                            source     = excluded.source,
                            meta_json  = excluded.meta_json,
                            updated_at = excluded.updated_at
                        """,
                        (
                            document.doc_id,
                            document.title,
                            document.body,
                            document.source,
                            meta_json,
                            now,
                            now,
                        ),
                    )
                    row = self.conn.execute(
                        """
                        INSERT INTO chunks (doc_id, seq, text) VALUES (?, 0, ?)
                        ON CONFLICT(doc_id, seq) DO UPDATE SET text = excluded.text
                        RETURNING row_id
                        """,
                        (document.doc_id, document.body),
                    ).fetchone()
                    self.conn.execute(
                        "DELETE FROM fts WHERE doc_id = ?", (document.doc_id,)
                    )
                    self.conn.execute(
                        "INSERT INTO fts (doc_id, title, body) VALUES (?, ?, ?)",
                        (document.doc_id, document.title, document.body),
                    )
                    if self.embedder is not None:
                        chunk_rows.append(
                            (
                                int(row[0]),
                                compose_embed_text(document.title, document.body),
                            )
                        )

                if self.embedder is not None and chunk_rows:
                    self._sync_vectors(chunk_rows)

                self.conn.commit()
            except Exception:
                self.conn.rollback()
                self._vector_cache = None
                self._vector_cache_version = None
                raise
            finally:
                self._vector_cache = None
                self._vector_cache_version = None

        for doc in docs:
            batch.append(doc)
            if len(batch) >= BATCH_SIZE:
                _flush(batch)
                processed += len(batch)
                batch = []
        if batch:
            _flush(batch)
            processed += len(batch)
        return processed

    def _sync_vectors(self, chunk_rows: list[tuple[int, str]]) -> None:
        """Encode, validate, and store normalized float32 vectors."""
        import numpy as np

        from .embed import serialize_float32

        texts = [text for _, text in chunk_rows]
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

        if self.get_meta("int8_scale") is None:
            # Sentence embeddings are unit-normalized, so their components are
            # bounded by [-1, 1]. A fixed scale is deterministic across batches
            # and avoids first-batch calibration drift.
            self._write_meta("int8_scale", repr(DEFAULT_INT8_SCALE))

        for (row_id, _text), vector in zip(chunk_rows, vectors):
            self.conn.execute("DELETE FROM vec_float WHERE rowid = ?", (row_id,))
            self.conn.execute(
                "INSERT INTO vec_float(rowid, embedding) VALUES (?, ?)",
                (row_id, serialize_float32(vector)),
            )

    def iter_vector_blocks(self, block_size: int = 2048):
        """Yield bounded blocks of stored vectors without a full-corpus cache."""
        import numpy as np

        if not 1 <= block_size <= 10_000:
            raise UsageError("vector block size must be within 1..10000")
        dim_raw = self.get_meta("model_dim")
        dim = int(dim_raw) if dim_raw else 0
        if not dim:
            return
        start_version = int(self.conn.execute("PRAGMA data_version").fetchone()[0])
        last_row_id = -1
        while True:
            rows = self.conn.execute(
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
        end_version = int(self.conn.execute("PRAGMA data_version").fetchone()[0])
        if start_version != end_version:
            raise DataError("vector index changed during scanning; retry the search")

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
        """doc_id -> float32 ndarray, for reranking a candidate pool."""
        import numpy as np

        unique = list(dict.fromkeys(doc_ids))
        if not unique:
            return {}
        placeholders = ",".join("?" * len(unique))
        rows = self.conn.execute(
            f"""
            SELECT c.doc_id, v.embedding
            FROM vec_float v
            JOIN chunks c ON c.row_id = v.rowid
            WHERE c.doc_id IN ({placeholders})
            """,  # noqa: S608 - placeholders only, no user data in SQL text
            unique,
        ).fetchall()
        return {doc_id: np.frombuffer(blob, dtype="<f4").copy() for doc_id, blob in rows}

    def get_documents(self, doc_ids: list[str]) -> dict[str, dict[str, str | None]]:
        """Fetch display metadata and bodies for a bounded result set."""
        unique = list(dict.fromkeys(doc_ids))
        if not unique:
            return {}
        placeholders = ",".join("?" for _ in unique)
        rows = self.conn.execute(
            f"""
            SELECT doc_id, title, body, source
            FROM documents
            WHERE doc_id IN ({placeholders})
            """,  # noqa: S608 - placeholders only
            unique,
        ).fetchall()
        return {
            str(row["doc_id"]): {
                "title": str(row["title"] or ""),
                "body": str(row["body"] or ""),
                "source": row["source"],
            }
            for row in rows
        }

    def clear(self) -> None:
        """Remove all corpus/vector content while preserving the schema."""
        try:
            if self.has_vectors():
                self.conn.execute("DELETE FROM vec_float")
            self.conn.execute("DELETE FROM fts")
            self.conn.execute("DELETE FROM chunks")
            self.conn.execute("DELETE FROM documents")
            self.conn.execute("DELETE FROM schema_meta WHERE key <> 'schema_version'")
            self._vector_cache = None
            self._vector_cache_version = None
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

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
        """Return count-level consistency checks for documents and vectors."""
        chunks = self.count("chunks")
        report = {
            "documents": self.count("documents"),
            "chunks": chunks,
            "fts": self.count("fts"),
            "vectors": None,
            "missing_vectors": 0,
            "orphan_vectors": 0,
            "ok": self.count("documents") == chunks == self.count("fts"),
        }
        if self.has_vectors():
            vectors = self.count("vectors")
            missing = int(
                self.conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM chunks c
                    LEFT JOIN vec_float v ON v.rowid = c.row_id
                    WHERE v.rowid IS NULL
                    """
                ).fetchone()[0]
            )
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
            report.update(
                vectors=vectors,
                missing_vectors=missing,
                orphan_vectors=orphan,
                ok=report["ok"] and vectors == chunks and missing == 0 and orphan == 0,
            )
        return report

    def stats(self) -> dict:
        info = {
            "path": str(self.path),
            "db_bytes": self.path.stat().st_size if self.path.exists() else 0,
            "documents": self.count("documents"),
            "chunks": self.count("chunks"),
        }
        if self.has_vectors():
            info["vectors"] = self.count("vectors")
            info["model"] = self.get_meta("model_name")
            info["model_dim"] = int(self.get_meta("model_dim") or 0)
            info["int8_calibrated"] = self.get_meta("int8_scale") is not None
        info["health"] = self.health()
        return info
