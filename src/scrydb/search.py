"""Search modes. Phase 1 implements lexical (FTS5 BM25); semantic and
hybrid arrive in Phases 2-3 per docs/06-engineering-plan.md."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .errors import EmptyQueryError


@dataclass(frozen=True)
class Hit:
    doc_id: str
    title: str
    score: float
    snippet: str


def sanitize_fts_query(raw: str) -> str:
    """Make arbitrary user input safe for the FTS5 MATCH parser.

    We quote every whitespace token (dropping embedded double quotes) so
    operators/parens in user text can never corrupt the query syntax.
    Quoted tokens are implicit ANDs in FTS5.
    """
    quoted = []
    for token in raw.split():
        cleaned = '"' + token.replace('"', "") + '"'
        if cleaned != '""':
            quoted.append(cleaned)
    return " ".join(quoted)


def lexical_search(conn: sqlite3.Connection, query: str, k: int = 10) -> list[Hit]:
    q = query.strip()
    if not q:
        raise EmptyQueryError("query is empty")
    match = sanitize_fts_query(q)
    rows = conn.execute(
        """
        SELECT doc_id,
               title,
               -bm25(fts)              AS score,
               snippet(fts, -1, '[', ']', ' … ', 12) AS snip
        FROM fts
        WHERE fts MATCH ?
        ORDER BY score ASC, doc_id ASC
        LIMIT ?
        """,
        (match, k),
    ).fetchall()
    # -bm25 => higher is better; deterministic tie-break by doc_id.
    return [Hit(r["doc_id"], r["title"], float(r["score"]), r["snip"]) for r in rows]
