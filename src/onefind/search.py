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
            "rebuild the index with 'OneFind index --embed' first"
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
           precision: str = "float", rrf_k: int = 60,
           rerank: bool = False, fusion: str = "rrf",
           alpha: float = 0.5) -> list[Hit]:
    """Mode dispatcher (lexical | semantic | hybrid)."""
    if mode == "lexical":
        return lexical_search(index.conn, query, k=k)
    if mode == "semantic":
        return semantic_search(index, query, k=k, precision=precision)
    if mode == "hybrid":
        return hybrid_search(index, query, k=k, precision=precision,
                             rrf_k=rrf_k, rerank=rerank,
                             fusion=fusion, alpha=alpha)
    raise UsageError(f"unknown mode '{mode}'")


# ---- hybrid RRF fusion (Phase 3) ---------------------------------------------

def rrf_fuse(rankings: list[list[str]], k: int = 60) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion (Cormock et al. 2009; the paper's ref [7]).

    score(d) = sum over rankings of 1 / (k + rank), rank starting at 1.
    Deterministic ordering: descending score, then ascending doc_id.
    """
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


def hybrid_search(index, query: str, k: int = 10, precision: str = "float",
                  rrf_k: int = 60, rerank: bool = False,
                  fusion: str = "rrf", alpha: float = 0.5) -> list[Hit]:
    """Fuse lexical BM25 with one semantic-precision ranking.

    Fusion strategies (T-10 extension):
    - 'rrf'    (default): Reciprocal Rank Fusion, k=60
    - 'linear': blend min-max-normalized leg scores via alpha

    Each leg retrieves depth k; fused top-k is returned. With rerank=True
    the fused candidates (<=2k docs) are rescored by full-precision cosine
    against the stored vectors - the paper's optional costly second stage.
    """
    q = query.strip()
    if not q:
        raise EmptyQueryError("query is empty")
    if not 0.0 <= alpha <= 1.0:
        raise UsageError("alpha must be in [0, 1]")
    if fusion not in ("rrf", "linear"):
        raise UsageError(f"unknown fusion '{fusion}' (rrf|linear)")
    _require_embedder(index)

    lex = lexical_search(index.conn, q, k=k)
    sem = semantic_search(index, q, k=k, precision=precision)

    if fusion == "rrf":
        fused = rrf_fuse(
            [[h.doc_id for h in lex], [h.doc_id for h in sem]], k=rrf_k
        )[:k]
    else:  # linear
        fused = linear_fuse(lex, sem, alpha)[:k]

    snippets = {h.doc_id: h.snippet for h in lex}
    titles: dict[str, str] = {}
    for hit in (*lex, *sem):
        titles.setdefault(hit.doc_id, hit.title)

    hits = [
        Hit(doc_id, titles.get(doc_id, ""), score, snippets.get(doc_id, ""))
        for doc_id, score in fused
    ]
    return _rerank_candidates(index, q, hits, k) if rerank else hits


def linear_fuse(lex_hits: list[Hit], sem_hits: list[Hit],
                alpha: float) -> list[tuple[str, float]]:
    """Min-max-normalize each leg's scores to [0, 1] within the retrieved
    pool, then blend: score = alpha * sem + (1 - alpha) * lex.

    Deterministic tie-break by (-score, doc_id). At alpha=0 the ranking
    equals the (tied) lexical ranking; at alpha=1 it equals the semantic
    ranking.
    """
    import numpy as np

    def _normalize(hits: list[Hit]) -> dict[str, float]:
        if not hits:
            return {}
        scores = np.array([h.score for h in hits], dtype=float)
        lo, hi = float(scores.min()), float(scores.max())
        if hi - lo == 0.0:
            return {h.doc_id: 1.0 for h in hits}
        return {
            h.doc_id: float((h.score - lo) / (hi - lo))
            for h in hits
        }

    lex_norm = _normalize(lex_hits)
    sem_norm = _normalize(sem_hits)
    out = [
        (d, alpha * sem_norm.get(d, 0.0) + (1.0 - alpha) * lex_norm.get(d, 0.0))
        for d in (set(lex_norm) | set(sem_norm))
    ]
    out.sort(key=lambda kv: (-kv[1], kv[0]))
    return out


def _rerank_candidates(index, query: str, hits: list[Hit], k: int) -> list[Hit]:
    """Rescore fused candidates by true cosine; never returns fewer than
    min(k, len(hits)) results."""
    import numpy as np

    if not hits:
        return hits
    vectors = index.vectors_for_docs([h.doc_id for h in hits])
    if not vectors:
        return hits[:k]
    qvec = index.embedder.encode_query(query)
    rescored = []
    for hit in hits:
        vec = vectors.get(hit.doc_id)
        similarity = float(np.dot(vec, qvec)) if vec is not None else -2.0
        rescored.append((similarity, hit))
    rescored.sort(key=lambda pair: (-pair[0], pair[1].doc_id))
    return [hit for _sim, hit in rescored][:k]

