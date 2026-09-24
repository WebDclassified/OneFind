"""Lexical, semantic, and hybrid retrieval modes.

The public search surface intentionally accepts an :class:`~onefind.store.Index`
so lexical-only callers do not need to load the optional embedding stack.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .errors import EmptyQueryError, UsageError


_MAX_RESULTS = 10_000


@dataclass(frozen=True)
class Hit:
    doc_id: str
    title: str
    score: float
    snippet: str
    source: str | None = None


def _validate_k(k: int) -> int:
    if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= _MAX_RESULTS:
        raise UsageError(f"k must be an integer within 1..{_MAX_RESULTS}")
    return k


def _document_metadata(conn: sqlite3.Connection, doc_ids: list[str]) -> dict[str, tuple[str, str | None]]:
    if not doc_ids:
        return {}
    placeholders = ",".join("?" for _ in doc_ids)
    rows = conn.execute(
        f"""
        SELECT doc_id, title, source
        FROM documents
        WHERE doc_id IN ({placeholders})
        """,  # noqa: S608 - placeholders only; values remain bound parameters
        doc_ids,
    ).fetchall()
    return {str(row["doc_id"]): (str(row["title"] or ""), row["source"]) for row in rows}


def sanitize_fts_query(raw: str) -> str:
    """Quote arbitrary input so FTS5 operators are treated as document text."""
    quoted: list[str] = []
    for token in raw.split():
        cleaned = '"' + token.replace('"', "") + '"'
        if cleaned != '""':
            quoted.append(cleaned)
    return " ".join(quoted)


def lexical_search(conn: sqlite3.Connection, query: str, k: int = 10) -> list[Hit]:
    """Return FTS5 BM25 matches ordered from strongest to weakest relevance.

    SQLite's ``bm25()`` is lower-is-better and normally negative. The query
    negates it to produce a conventional higher-is-better score, so SQL must
    sort that alias descending.
    """
    _validate_k(k)
    cleaned_query = query.strip()
    if not cleaned_query:
        raise EmptyQueryError("query is empty")
    match = sanitize_fts_query(cleaned_query)
    if not match:
        return []

    rows = conn.execute(
        """
        SELECT doc_id,
               title,
               -bm25(fts)              AS score,
               snippet(fts, -1, '', '', ' … ', 12) AS snip
        FROM fts
        WHERE fts MATCH ?
        ORDER BY score DESC, doc_id ASC
        LIMIT ?
        """,
        (match, k),
    ).fetchall()
    metadata = _document_metadata(conn, [str(row["doc_id"]) for row in rows])
    return [
        Hit(
            doc_id=str(row["doc_id"]),
            title=str(row["title"] or ""),
            score=float(row["score"]),
            snippet=str(row["snip"] or ""),
            source=metadata.get(str(row["doc_id"]), ("", None))[1],
        )
        for row in rows
    ]


def _require_embedder(index):
    if getattr(index, "embedder", None) is None:
        raise UsageError(
            "no embedding model attached to this session; "
            "rebuild the index with 'onefind index --embed' first"
        )
    return index.embedder


def _metadata_for_rows(index, row_ids: list[int]) -> dict[int, tuple[str, str, str | None]]:
    if not row_ids:
        return {}
    placeholders = ",".join("?" for _ in row_ids)
    rows = index.conn.execute(
        f"""
        SELECT c.row_id, c.doc_id, d.title, d.source
        FROM chunks c
        JOIN documents d ON d.doc_id = c.doc_id
        WHERE c.row_id IN ({placeholders})
        """,  # noqa: S608 - placeholders only; row ids remain bound parameters
        row_ids,
    ).fetchall()
    return {
        int(row["row_id"]): (str(row["doc_id"]), str(row["title"] or ""), row["source"])
        for row in rows
    }


def _native_float_search(index, query_vector, k: int):
    """Use sqlite-vec's cosine KNN path for full-precision vectors."""
    from .embed import serialize_float32

    vector_count = index.count("vectors")
    if vector_count == 0:
        return []
    query_k = min(k, vector_count)
    rows = index.conn.execute(
        """
        SELECT rowid, distance
        FROM vec_float
        WHERE embedding MATCH ? AND k = ?
        ORDER BY distance ASC
        """,
        (serialize_float32(query_vector), query_k),
    ).fetchall()
    metadata = _metadata_for_rows(index, [int(row["rowid"]) for row in rows])
    hits: list[Hit] = []
    for row in rows:
        row_id = int(row["rowid"])
        doc_id, title, source = metadata.get(row_id, ("", "", None))
        if not doc_id:
            continue
        hits.append(
            Hit(
                doc_id=doc_id,
                title=title,
                score=1.0 - float(row["distance"]),
                snippet="",
                source=source,
            )
        )
    hits.sort(key=lambda hit: (-hit.score, hit.doc_id))
    return hits[:k]


def _application_side_search(index, query_vector, k: int, precision: str) -> list[Hit]:
    """Rank float storage in bounded blocks for exact int8/binary modes."""
    import numpy as np

    from .embed import quantize_int8

    if precision == "int8":
        scale_raw = index.get_meta("int8_scale")
        if scale_raw is None:
            raise UsageError("index has no int8 calibration; rebuild it with --embed")
        scale = float(scale_raw)
        if not np.isfinite(scale) or scale <= 0:
            raise UsageError("index has an invalid int8 calibration")
        query_quantized = np.frombuffer(
            quantize_int8(query_vector, scale), dtype=np.int8
        ).astype(np.float64)
        query_norm = np.linalg.norm(query_quantized) or 1.0
    elif precision == "binary":
        query_bits = query_vector > 0
    else:
        raise UsageError(f"unknown precision '{precision}' (float|int8|binary)")

    best: list[Hit] = []
    for matrix, row_ids in index.iter_vector_blocks():
        if precision == "int8":
            quantized = np.round(matrix.astype(np.float64) / scale).clip(-127, 127)
            matrix_norms = np.linalg.norm(quantized, axis=1)
            matrix_norms[matrix_norms == 0] = 1.0
            similarities = (quantized @ query_quantized) / (matrix_norms * query_norm)
        else:
            similarities = -((matrix > 0) != query_bits).sum(axis=1).astype(np.float64)

        if not np.isfinite(similarities).all():
            raise UsageError("quantized search produced non-finite scores")
        metadata = _metadata_for_rows(index, row_ids)
        block_hits: list[Hit] = []
        for position, row_id in enumerate(row_ids):
            doc_id, title, source = metadata.get(row_id, ("", "", None))
            if not doc_id:
                continue
            block_hits.append(
                Hit(
                    doc_id=doc_id,
                    title=title,
                    score=float(similarities[position]),
                    snippet="",
                    source=source,
                )
            )
        best.extend(block_hits)
        best.sort(key=lambda hit: (-hit.score, hit.doc_id))
        del best[k:]

    return best


def semantic_search(index, query: str, k: int = 10, precision: str = "float",
                    _query_vector=None) -> list[Hit]:
    """Return semantic neighbors in float, int8, or binary precision."""
    _validate_k(k)
    cleaned_query = query.strip()
    if not cleaned_query:
        raise EmptyQueryError("query is empty")
    if precision not in {"float", "int8", "binary"}:
        raise UsageError(f"unknown precision '{precision}' (float|int8|binary)")

    embedder = _require_embedder(index)
    query_vector = embedder.encode_query(cleaned_query) if _query_vector is None else _query_vector
    if precision == "float":
        return _native_float_search(index, query_vector, k)
    return _application_side_search(index, query_vector, k, precision)


def search(index, query: str, mode: str = "hybrid", k: int = 10,
           precision: str = "float", rrf_k: int = 60,
           rerank: bool = False, fusion: str = "rrf",
           alpha: float = 0.5, candidate_depth: int = 50) -> list[Hit]:
    """Dispatch to lexical, semantic, or hybrid retrieval."""
    if mode == "lexical":
        return lexical_search(index.conn, query, k=k)
    if mode == "semantic":
        return semantic_search(index, query, k=k, precision=precision)
    if mode == "hybrid":
        return hybrid_search(
            index,
            query,
            k=k,
            precision=precision,
            rrf_k=rrf_k,
            rerank=rerank,
            fusion=fusion,
            alpha=alpha,
            candidate_depth=candidate_depth,
        )
    raise UsageError(f"unknown mode '{mode}'")


def rrf_fuse(rankings: list[list[str]], k: int = 60) -> list[tuple[str, float]]:
    """Fuse rankings with Reciprocal Rank Fusion (Cormack et al., 2009)."""
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise UsageError("rrf k must be a positive integer")
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def hybrid_search(index, query: str, k: int = 10, precision: str = "float",
                  rrf_k: int = 60, rerank: bool = False,
                  fusion: str = "rrf", alpha: float = 0.5,
                  candidate_depth: int = 50) -> list[Hit]:
    """Fuse lexical and semantic legs, optionally reranking the union pool."""
    _validate_k(k)
    if rerank and (
        isinstance(candidate_depth, bool)
        or not isinstance(candidate_depth, int)
        or not k <= candidate_depth <= _MAX_RESULTS
    ):
        raise UsageError(
            f"candidate_depth must be an integer within {k}..{_MAX_RESULTS} when reranking"
        )
    fetch_depth = max(k, candidate_depth) if rerank else k
    cleaned_query = query.strip()
    if not cleaned_query:
        raise EmptyQueryError("query is empty")
    if not 0.0 <= alpha <= 1.0:
        raise UsageError("alpha must be in [0, 1]")
    if fusion not in {"rrf", "linear"}:
        raise UsageError(f"unknown fusion '{fusion}' (rrf|linear)")
    _require_embedder(index)

    query_vector = index.embedder.encode_query(cleaned_query) if rerank else None
    lexical_hits = lexical_search(index.conn, cleaned_query, k=fetch_depth)
    semantic_hits = semantic_search(
        index,
        cleaned_query,
        k=fetch_depth,
        precision=precision,
        _query_vector=query_vector,
    )

    if fusion == "rrf":
        fused = rrf_fuse(
            [[hit.doc_id for hit in lexical_hits], [hit.doc_id for hit in semantic_hits]],
            k=rrf_k,
        )
    else:
        fused = linear_fuse(lexical_hits, semantic_hits, alpha)

    titles: dict[str, str] = {}
    sources: dict[str, str | None] = {}
    snippets: dict[str, str] = {}
    for hit in (*lexical_hits, *semantic_hits):
        titles.setdefault(hit.doc_id, hit.title)
        sources.setdefault(hit.doc_id, hit.source)
        if hit.snippet:
            snippets.setdefault(hit.doc_id, hit.snippet)

    fused_hits = [
        Hit(
            doc_id=doc_id,
            title=titles.get(doc_id, ""),
            score=score,
            snippet=snippets.get(doc_id, ""),
            source=sources.get(doc_id),
        )
        for doc_id, score in fused
    ]
    return (
        _rerank_candidates(index, fused_hits, k, query_vector=query_vector)
        if rerank
        else fused_hits[:k]
    )


def linear_fuse(lexical_hits: list[Hit], semantic_hits: list[Hit],
                alpha: float) -> list[tuple[str, float]]:
    """Blend min-max-normalized leg scores.

    Alpha endpoints are exact passthroughs: ``0`` returns only lexical hits and
    ``1`` returns only semantic hits. Intermediate values rank their union.
    """
    if not 0.0 <= alpha <= 1.0:
        raise UsageError("alpha must be in [0, 1]")
    if alpha == 0.0:
        return [(hit.doc_id, hit.score) for hit in lexical_hits]
    if alpha == 1.0:
        return [(hit.doc_id, hit.score) for hit in semantic_hits]

    import numpy as np

    def normalize(hits: list[Hit]) -> dict[str, float]:
        if not hits:
            return {}
        scores = np.asarray([hit.score for hit in hits], dtype=float)
        low, high = float(scores.min()), float(scores.max())
        if high == low:
            return {hit.doc_id: 1.0 for hit in hits}
        return {hit.doc_id: float((hit.score - low) / (high - low)) for hit in hits}

    lexical_scores = normalize(lexical_hits)
    semantic_scores = normalize(semantic_hits)
    fused = [
        (
            doc_id,
            alpha * semantic_scores.get(doc_id, 0.0)
            + (1.0 - alpha) * lexical_scores.get(doc_id, 0.0),
        )
        for doc_id in set(lexical_scores) | set(semantic_scores)
    ]
    fused.sort(key=lambda item: (-item[1], item[0]))
    return fused


def _rerank_candidates(index, hits: list[Hit], k: int, query_vector) -> list[Hit]:
    """Rescore the complete fused pool by cosine and return new scored hits."""
    if not hits:
        return []
    import numpy as np

    vectors = index.vectors_for_docs([hit.doc_id for hit in hits])
    if not vectors:
        return hits[:k]
    if query_vector is None:
        raise UsageError("reranking requires a query embedding")
    rescored: list[Hit] = []
    for hit in hits:
        vector = vectors.get(hit.doc_id)
        similarity = float(np.dot(vector, query_vector)) if vector is not None else -2.0
        rescored.append(
            Hit(
                doc_id=hit.doc_id,
                title=hit.title,
                score=similarity,
                snippet=hit.snippet,
                source=hit.source,
            )
        )
    rescored.sort(key=lambda hit: (-hit.score, hit.doc_id))
    return rescored[:k]
