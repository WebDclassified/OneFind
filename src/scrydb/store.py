"""SQLite storage layer: connection, schema, transactional document upserts.

Schema follows docs/05-backend-design.md. V1 indexes whole documents
(one chunk per doc) so the BEIR evaluation protocol stays honest; the
chunks table owns stable row_ids that the Phase-2 vec tables key off.

Vector tables are created lazily on first embedder attachment because
their DDL depends on the model's dimension:
    vec_float(embedding float[dim] distance_metric=cosine)
    vec_int8 (embedding int8[dim])
    vec_bit  (embedding bit[dim])
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from .errors import DataError, EnvError

if TYPE_CHECKING:  # pragma: no cover
    from .embed import SentenceEmbedder

SCHEMA_VERSION = 1
BATCH_SIZE = 256


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

INSERT INTO schema_meta(key, value) VALUES ('schema_version', '1')
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

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def open(cls, path: str | Path) -> "Index":
        target = Path(path)
        try:
            conn = sqlite3.connect(target)
        except OSError as exc:
            raise DataError(f"cannot open database at {target}: {exc}") from exc
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        conn.commit()
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

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO schema_meta(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self.conn.commit()

    # -- embedder attachment ---------------------------------------------------

    def attach_embedder(self, embedder: "SentenceEmbedder") -> None:
        """Bind an embedding model and lazily materialize the vec tables."""
        if not self.vec_ready:
            raise EnvError(
                "sqlite-vec unavailable in this interpreter; "
                "vector search disabled - run 'scrydb check' for the fix"
            )
        existing_model = self.get_meta("model_name")
        if existing_model and existing_model != embedder.name:
            raise DataError(
                f"index already bound to model '{existing_model}'; "
                f"refusing to mix models (got '{embedder.name}')"
            )
        existing_dim = self.get_meta("model_dim")
        if existing_dim and int(existing_dim) != embedder.dimension:
            raise DataError(
                f"model_dim mismatch: index has {existing_dim}, model reports "
                f"{embedder.dimension}"
            )

        dim = embedder.dimension
        # NOTE (ADR-7): sqlite-vec v0.1.9 vec0 rejects ALL int8/bit inputs
        # (verified empirically - see benchmarks reports), so quantized
        # precisions are computed application-side over vec_float storage.
        self.conn.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_float USING vec0(
                embedding float[{dim}] distance_metric=cosine
            );
            """
        )
        self.conn.commit()
        self.set_meta("model_name", embedder.name)
        self.set_meta("model_dim", str(dim))
        self.embedder = embedder

    # -- writes ------------------------------------------------------------

    def add_documents(self, docs: Iterable[Document]) -> int:
        """Upsert documents (+fts+chunk[+vector] rows) in committed batches.

        Returns the number of documents processed. A failure part-way
        leaves earlier batches committed and the database openable.
        """
        processed = 0
        batch: list[Document] = []

        def _flush(batch: list[Document]) -> None:
            from .embed import compose_embed_text

            now = utcnow_iso()
            chunk_rows: list[tuple[int, str]] = []  # (chunk_row_id, embed_text)
            for d in batch:
                meta_json = json.dumps(d.meta, sort_keys=True) if d.meta else None
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
                    (d.doc_id, d.title, d.body, d.source, meta_json, now, now),
                )
                # stable chunk row_id across re-indexes => stable vec keys
                row = self.conn.execute(
                    """
                    INSERT INTO chunks (doc_id, seq, text) VALUES (?, 0, ?)
                    ON CONFLICT(doc_id, seq) DO UPDATE SET text = excluded.text
                    RETURNING row_id
                    """,
                    (d.doc_id, d.body),
                ).fetchone()
                # standalone fts copy, resynced per document
                self.conn.execute("DELETE FROM fts WHERE doc_id = ?", (d.doc_id,))
                self.conn.execute(
                    "INSERT INTO fts (doc_id, title, body) VALUES (?, ?, ?)",
                    (d.doc_id, d.title, d.body),
                )
                if self.embedder is not None:
                    chunk_rows.append((int(row[0]), compose_embed_text(d.title, d.body)))

            if self.embedder is not None and chunk_rows:
                self._sync_vectors(chunk_rows)

            self.conn.commit()

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
        """Encode texts once; store normalized float32 vectors."""
        from .embed import calibrate_scale, serialize_float32

        texts = [t for _, t in chunk_rows]
        vectors = self.embedder.encode_documents(texts)

        scale_raw = self.get_meta("int8_scale")
        if scale_raw is None:  # first calibration wins; later vectors are clipped
            scale = calibrate_scale(vectors)
            self.set_meta("int8_scale", repr(scale))

        for (row_id, _text), vec in zip(chunk_rows, vectors):
            self.conn.execute("DELETE FROM vec_float WHERE rowid = ?", (row_id,))
            self.conn.execute(
                "INSERT INTO vec_float(rowid, embedding) VALUES (?, ?)",
                (row_id, serialize_float32(vec)),
            )

    def get_all_vectors(self) -> tuple["object", list[int]]:
        """All stored embeddings as (<ndarray float32 [n, dim]>, row_ids).

        Loaded fresh per call; fine at reproduction corpus sizes (a 5K-doc
        BEIR subset is ~7 MB). Caching arrives with the eval harness.
        """
        import numpy as np

        dim_raw = self.get_meta("model_dim")
        dim = int(dim_raw) if dim_raw else 0
        rows = self.conn.execute(
            "SELECT rowid, embedding FROM vec_float ORDER BY rowid"
        ).fetchall()
        row_ids = [int(r[0]) for r in rows]
        if not rows or not dim:
            return np.empty((0, dim or 0), dtype=np.float32), row_ids
        blob = b"".join(r[1] for r in rows)
        matrix = np.frombuffer(blob, dtype="<f4").reshape(len(rows), dim)
        return matrix.copy(), row_ids

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

    # -- reads ---------------------------------------------------------------

    def count(self, what: str) -> int:
        table = {
            "documents": "documents",
            "chunks": "chunks",
            "fts": "fts",
            "vectors": "vec_float",
        }[what]
        return int(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608

    def has_vectors(self) -> bool:
        return self.get_meta("model_name") is not None

    def stats(self) -> dict:
        info = {
            "path": str(self.path),
            "documents": self.count("documents"),
            "chunks": self.count("chunks"),
        }
        if self.has_vectors():
            info["vectors"] = self.count("vectors")
            info["model"] = self.get_meta("model_name")
        return info
