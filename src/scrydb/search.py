"""Search modes. Phase 1: lexical (FTS5 BM25). Phase 2: semantic KNN at
three precisions. Hybrid RRF fusion arrives in Phase 3."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .errors import EmptyQueryError, UsageError


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


# ---- semantic (Phase 2, application-side quantization per ADR-7) -------------

def _require_embedder(index):
    if getattr(index, "embedder", None) is None:
        raise UsageError(
            "no embedding model attached to this session; "
            "rebuild the index with 'scrydb index --embed' first"
        )
    return index.embedder


def semantic_search(index, query: str, k: int = 10, precision: str = "float") -> list[Hit]:
    """KNN in three precisions, computed over stored float32 vectors.

    Score conventions (higher = better, like lexical):
    - float : cosine similarity (unit vectors -> dot product), [-1, 1]
    - int8  : cosine similarity of globally-scaled integer quantizations
    - binary: -Hamming distance between sign-bit packings (integer)
    """
    import numpy as np

    from .embed import quantize_int8

    q = query.strip()
    if not q:
        raise EmptyQueryError("query is empty")
    embedder = _require_embedder(index)

    matrix, row_ids = index.get_all_vectors()
    if not row_ids:
        return []

    meta = {
        rid: (doc_id, title)
        for rid, doc_id, title in index.conn.execute(
            """
            SELECT c.row_id, c.doc_id, d.title
            FROM chunks c JOIN documents d ON d.doc_id = c.doc_id
            """
        )
    }

    qvec = embedder.encode_query(q)

    if precision == "float":
        sims = matrix @ qvec
    elif precision == "int8":
        scale_raw = index.get_meta("int8_scale")
        if scale_raw is None:
            raise UsageError("index has no int8 calibration; re-index with --embed")
        scale = float(scale_raw)
        q_i = np.frombuffer(quantize_int8(qvec, scale), dtype=np.int8).astype(np.float64)
        m_i = np.round(matrix.astype(np.float64) / scale).clip(-127, 127)
        norms_q = np.linalg.norm(q_i) or 1.0
        norms_m = np.linalg.norm(m_i, axis=1)
        norms_m[norms_m == 0] = 1.0
        sims = (m_i @ q_i) / (norms_m * norms_q)
    elif precision == "binary":
        bits_m = matrix > 0
        bits_q = qvec > 0
        hamming = (bits_m != bits_q).sum(axis=1)
        sims = -hamming.astype(np.float64)
    else:
        raise UsageError(f"unknown precision '{precision}' (float|int8|binary)")

    doc_ids = [meta[rid][0] for rid in row_ids]
    titles = {rid: meta[rid][1] for rid in row_ids}
    order = sorted(range(len(row_ids)), key=lambda i: (-float(sims[i]), doc_ids[i]))[:k]

    return [
        Hit(doc_ids[i], titles[row_ids[i]], float(sims[i]), "") for i in order
    ]


def search(index, query: str, mode: str = "hybrid", k: int = 10,
           precision: str = "float") -> list[Hit]:
    """Mode dispatcher. Hybrid lands in Phase 3 (task T-06)."""
    if mode == "lexical":
        return lexical_search(index.conn, query, k=k)
    if mode == "semantic":
        return semantic_search(index, query, k=k, precision=precision)
    raise UsageError("mode 'hybrid' arrives in Phase 3 (task T-06)")

