"""SQLite storage layer: connection, schema, transactional document upserts.

Schema follows docs/05-backend-design.md. V1 indexes whole documents
(one chunk per doc) so the BEIR evaluation protocol stays honest; the
chunks table exists from day one because Phase 2 vec tables key off it.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from .errors import DataError

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


class Index:
    """Owns the SQLite connection; all writes go through add_documents()."""

    def __init__(self, conn: sqlite3.Connection, path: str | Path) -> None:
        self.conn = conn
        self.path = Path(path)

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

    # -- writes ------------------------------------------------------------

    def add_documents(self, docs: Iterable[Document]) -> int:
        """Upsert documents (+fts+chunk rows) in committed batches.

        Returns the number of documents processed. A failure part-way
        leaves earlier batches committed and the database openable.
        """
        processed = 0
        batch: list[Document] = []

        def _flush(batch: list[Document]) -> None:
            now = utcnow_iso()
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
                # keep exactly one whole-doc chunk (V1 protocol, ADR-4)
                self.conn.execute("DELETE FROM chunks WHERE doc_id = ?", (d.doc_id,))
                self.conn.execute(
                    "INSERT INTO chunks (doc_id, seq, text) VALUES (?, 0, ?)",
                    (d.doc_id, d.body),
                )
                # standalone fts copy, resynced per document
                self.conn.execute("DELETE FROM fts WHERE doc_id = ?", (d.doc_id,))
                self.conn.execute(
                    "INSERT INTO fts (doc_id, title, body) VALUES (?, ?, ?)",
                    (d.doc_id, d.title, d.body),
                )
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

    # -- reads ---------------------------------------------------------------

    def count(self, what: str) -> int:
        table = {"documents": "documents", "chunks": "chunks", "fts": "fts"}[what]
        return int(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608

    def stats(self) -> dict:
        return {
            "path": str(self.path),
            "documents": self.count("documents"),
            "chunks": self.count("chunks"),
        }
