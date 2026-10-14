"""Lexical, semantic, and hybrid retrieval modes.

The public search surface intentionally accepts an :class:`~onefind.store.Index`
so lexical-only callers do not need to load the optional embedding stack.

v1.2 adds metadata filters (:class:`SearchFilters`), per-hit explanations,
confidence labels with abstention, and timed pipeline stages
(:func:`search_detailed`). Ranking itself is unchanged: explanations and
confidence describe a result list, they never reorder it. The opt-in
``lexical_match="any"|"auto"`` lets natural-language questions find
keyword matches without every word; the default ``"all"`` keeps the
historical AND semantics (and evaluation numbers) unchanged.
"""

from __future__ import annotations

import datetime
import json
import math
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Iterator

from .errors import DataError, EmptyQueryError, UsageError


_MAX_RESULTS = 10_000
SEARCH_MODES = ("lexical", "semantic", "hybrid")
PRECISIONS = ("float", "int8", "binary")
STAGE_NAMES = ("parse", "lexical", "embed", "semantic", "fuse", "rerank")
CONFIDENCE_LEVELS = ("strong", "moderate", "weak")

MAX_FILTER_VALUES = 50
_MAX_FILTER_CHARS = 512
_MAX_EXPLAIN_TERMS = 32
_VECTOR_BLOCK_SIZE = 1024

# Confidence thresholds for semantic similarity (cosine for float/int8).
# Calibrated 2026-10-13 with sentence-transformers/all-MiniLM-L6-v2 on
# sample-data (20 docs, 25 answerable queries) and showcase-data (120 docs,
# 192 answerable queries), plus 25 off-topic natural-language queries per
# corpus and the corpus no-answer queries:
#   float  relevant-doc cosine: min 0.345, p05 0.39, median 0.52-0.60
#          off-topic top-1:     max 0.328 (sample), 0.189 (showcase)
#          precision of top-10 hits >= 0.50: 0.72 (>= 0.45: 0.43)
#   int8   within 0.005 of float everywhere, so it shares the float values.
#   binary (1 - 2*hamming/dim) runs lower: relevant min 0.172, p05 0.245;
#          off-topic top-1 max 0.266; precision of hits >= 0.36: ~0.65.
# For float and int8, WEAK_SIM sits in the gap between the strongest
# off-topic top-1 and the weakest relevant document, so no answerable query
# abstained and every off-topic query did. Binary has no such gap (relevant
# min 0.172 < off-topic max 0.266): any binary cut-off either drops answers
# or lets off-topic questions through. search_detailed therefore labels
# binary hits by the float cosine of their stored vectors (re-checked
# 2026-10-14: 40 off-topic questions per corpus abstain at every precision).
# The BINARY_* values remain only as the fallback for a hit whose float
# vector is unavailable. The showcase "no-answer" queries are adversarial
# keyword soups (top-1 0.375-0.425); lexical search returns nothing for them.
STRONG_SIM = 0.50
WEAK_SIM = 0.34
BINARY_STRONG_SIM = 0.36
BINARY_WEAK_SIM = 0.19

StageCallback = Callable[[str, str, float], None]

# How the lexical leg combines query words: "all" (FTS AND of every word, the
# historical behaviour and the default), "any" (OR of the non-stopword
# words), or "auto" (AND of the non-stopword words first, topped up with
# "any" hits when it finds fewer than k).
LEXICAL_MATCHES = ("all", "any", "auto")

# Compact English function-word list. Stopwords never count as keywords in
# explanations, snippet highlighting, or the "any" query, unless the query
# consists only of stopwords. Words that are often content in notes ("may",
# "will", "us", "won", "d") are deliberately left out. Contraction pieces
# ("don", "t", "ll") cover tokenizers that split on apostrophes.
STOPWORDS = frozenset(
    """
    a about above after again against all am an and any are as at be because
    been before being below between both but by can could did do does doing
    down during each few for from further had has have having he her here hers
    herself him himself his how i if in into is it its itself just me more most
    my myself no nor not now of off on once only or other ought our ours
    ourselves out over own same shall she should so some such than that the
    their theirs them themselves then there these they this those through to
    too under until up very was we were what when where which while who whom
    whose why with would you your yours yourself yourselves
    s t ll re ve don doesn didn isn aren wasn weren hasn haven hadn wouldn
    shouldn couldn
    """.split()
)

_TERM_RE = re.compile(r"[^\W_]+", re.UNICODE)
_APOSTROPHE_RE = re.compile(r"['’]")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_EXT_RE = re.compile(r"^\.[^\s/\\]{1,31}$")


@dataclass(frozen=True)
class Hit:
    doc_id: str
    title: str
    score: float
    snippet: str
    source: str | None = None
    # Explanations are presentation data: they never take part in equality,
    # hashing, or ordering, so existing Hit comparisons keep working.
    explain: dict | None = field(default=None, compare=False, hash=False, repr=False)


@dataclass
class SearchOutcome:
    """Result of :func:`search_detailed`.

    ``confidences`` is aligned with ``hits`` and is filled even when
    ``explain`` is off, so callers can group "weaker matches" cheaply.
    """

    hits: list[Hit]
    stages: list[dict] = field(default_factory=list)
    abstained: bool = False
    weak_count: int = 0
    confidences: list[str] = field(default_factory=list)
    # lexical_match="auto" only: True when only ``strict_count`` documents
    # contained every keyword (stopwords excluded), so the lexical leg was
    # topped up with documents matching some of the keywords.
    relaxed: bool = False
    # Lexical-leg hits found by the strict pass (every word for "all", every
    # keyword for "auto"); None when the lexical leg did not run or used
    # lexical_match="any".
    strict_count: int | None = None


# ---- filters -----------------------------------------------------------------


def _string_list(name: str, value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise UsageError(f"filter '{name}' must be a list of strings")
    if len(value) > MAX_FILTER_VALUES:
        raise UsageError(f"filter '{name}' accepts at most {MAX_FILTER_VALUES} values")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise UsageError(f"filter '{name}' must contain only strings")
        if len(item) > _MAX_FILTER_CHARS:
            raise UsageError(f"filter '{name}' values must be at most {_MAX_FILTER_CHARS} characters")
        items.append(item)
    return items


def _normalize_folder(raw: str) -> str:
    folder = re.sub(r"/+", "/", raw.strip().replace("\\", "/"))
    while folder.startswith("./"):
        folder = folder[2:]
    folder = folder.strip("/")
    return "" if folder == "." else folder


def _normalize_ext(raw: str) -> str:
    ext = raw.strip().lower()
    if ext and not ext.startswith("."):
        ext = "." + ext
    if not _EXT_RE.match(ext):
        raise UsageError(f"invalid file type filter '{raw}' (expected e.g. '.md')")
    return ext


def _normalize_tag(raw: str) -> str:
    tag = raw.strip().lower()
    if not tag:
        raise UsageError("tag filters cannot be blank")
    return tag


def _dedupe(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _local_timestamp(moment: datetime.datetime) -> float:
    try:
        return moment.timestamp()
    except (OverflowError, OSError, ValueError):
        # Windows cannot resolve local offsets for some out-of-range dates.
        return moment.replace(tzinfo=datetime.timezone.utc).timestamp()


def _coerce_time(name: str, value: Any, *, end_of_day: bool) -> float | None:
    """Epoch seconds from a number or an ISO date/datetime string.

    Date-only strings name a calendar day in the local timezone; as an upper
    bound they include the whole day.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise UsageError(f"filter '{name}' must be epoch seconds or a YYYY-MM-DD date")
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            raise UsageError(f"filter '{name}' must be a finite number")
        return number
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            if _ISO_DATE_RE.match(text):
                day = datetime.date.fromisoformat(text)
                start = datetime.datetime(day.year, day.month, day.day)
                if not end_of_day:
                    return _local_timestamp(start)
                try:
                    following = start + datetime.timedelta(days=1)
                except OverflowError:
                    return _local_timestamp(start.replace(hour=23, minute=59, second=59))
                return _local_timestamp(following) - 0.001
            parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
            return _local_timestamp(parsed)
        except ValueError:
            pass
    raise UsageError(f"filter '{name}' must be epoch seconds or a YYYY-MM-DD date")


@dataclass(frozen=True)
class SearchFilters:
    """Metadata restrictions applied *before* ranking.

    ``folders`` match a folder and everything below it (``""`` = files at the
    collection root), ``exts`` are lowercase suffixes with a dot, ``tags``
    match any-of, and the ``modified_*`` bounds are inclusive epoch seconds.
    Values are validated and normalized on construction.
    """

    folders: tuple[str, ...] = ()
    exts: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    modified_after: float | None = None
    modified_before: float | None = None

    def __post_init__(self) -> None:
        folders = _dedupe(_normalize_folder(item) for item in _string_list("folders", self.folders))
        exts = _dedupe(_normalize_ext(item) for item in _string_list("exts", self.exts))
        tags = _dedupe(_normalize_tag(item) for item in _string_list("tags", self.tags))
        after = _coerce_time("modified_after", self.modified_after, end_of_day=False)
        before = _coerce_time("modified_before", self.modified_before, end_of_day=True)
        if after is not None and before is not None and after > before:
            raise UsageError("modified_after must not be later than modified_before")
        object.__setattr__(self, "folders", folders)
        object.__setattr__(self, "exts", exts)
        object.__setattr__(self, "tags", tags)
        object.__setattr__(self, "modified_after", after)
        object.__setattr__(self, "modified_before", before)

    def is_empty(self) -> bool:
        """True when the filters would not restrict anything."""
        return not (
            self.folders
            or self.exts
            or self.tags
            or self.modified_after is not None
            or self.modified_before is not None
        )

    @classmethod
    def from_dict(cls, data: dict | None) -> "SearchFilters":
        """Build validated filters from a JSON-style dict; raises UsageError."""
        if data is None:
            return cls()
        if isinstance(data, SearchFilters):
            return data
        if not isinstance(data, dict):
            raise UsageError("filters must be a JSON object")
        known = {"folders", "exts", "tags", "modified_after", "modified_before"}
        unknown = sorted(str(key) for key in data if key not in known)
        if unknown:
            raise UsageError(f"unknown filter '{unknown[0]}'")
        return cls(**{key: data[key] for key in known if key in data})

    def to_dict(self) -> dict:
        """Compact JSON-safe form: only the restrictions that are set."""
        result: dict[str, Any] = {}
        if self.folders:
            result["folders"] = list(self.folders)
        if self.exts:
            result["exts"] = list(self.exts)
        if self.tags:
            result["tags"] = list(self.tags)
        if self.modified_after is not None:
            result["modified_after"] = self.modified_after
        if self.modified_before is not None:
            result["modified_before"] = self.modified_before
        return result


def _coerce_filters(filters: Any) -> SearchFilters | None:
    """Normalize the ``filters`` argument; ``None`` means "no restriction"."""
    if filters is None:
        return None
    if isinstance(filters, dict):
        filters = SearchFilters.from_dict(filters)
    if not isinstance(filters, SearchFilters):
        raise UsageError("filters must be SearchFilters or a dict")
    return None if filters.is_empty() else filters


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _filter_clause(filters: SearchFilters, alias: str = "d") -> tuple[str, list[Any]]:
    """SQL predicate over ``documents AS alias`` plus its bound parameters."""
    clauses: list[str] = []
    params: list[Any] = []
    if filters.folders:
        options: list[str] = []
        for folder in filters.folders:
            if folder == "":
                options.append(f"{alias}.folder = ''")
            else:
                escaped = _escape_like(folder)
                options.append(
                    f"({alias}.folder LIKE ? ESCAPE '\\' OR {alias}.folder LIKE ? ESCAPE '\\')"
                )
                params.extend([escaped, escaped + "/%"])
        clauses.append("(" + " OR ".join(options) + ")")
    if filters.exts:
        clauses.append(f"{alias}.ext IN ({','.join('?' for _ in filters.exts)})")
        params.extend(filters.exts)
    if filters.tags:
        clauses.append(
            "EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid("
            f"{alias}.tags_json) THEN {alias}.tags_json ELSE '[]' END) AS tag "
            f"WHERE tag.value IN ({','.join('?' for _ in filters.tags)}))"
        )
        params.extend(filters.tags)
    if filters.modified_after is not None:
        clauses.append(f"{alias}.mtime >= ?")
        params.append(filters.modified_after)
    if filters.modified_before is not None:
        clauses.append(f"{alias}.mtime <= ?")
        params.append(filters.modified_before)
    return " AND ".join(clauses) or "1", params


# ---- shared helpers ------------------------------------------------------------


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


def query_terms(query: str, limit: int = _MAX_EXPLAIN_TERMS) -> list[str]:
    """Distinct casefolded word terms of a query, in order (for explanations)."""
    terms = dict.fromkeys(match.casefold() for match in _TERM_RE.findall(query or ""))
    return list(terms)[:limit]


def is_stopword(word: str) -> bool:
    """True for an English function word (contractions: every part must be one)."""
    parts = [part for part in _APOSTROPHE_RE.split((word or "").casefold()) if part]
    return bool(parts) and all(part in STOPWORDS for part in parts)


def content_terms(terms: Iterable[str]) -> list[str]:
    """``terms`` without stopwords, or all of them when every term is a stopword."""
    terms = list(terms)
    kept = [term for term in terms if not is_stopword(term)]
    return kept or terms


def keyword_terms(query: str, limit: int = _MAX_EXPLAIN_TERMS) -> list[str]:
    """The query words that count as keywords: :func:`query_terms` minus stopwords.

    A query made only of stopwords ("what is this") keeps them all.
    """
    return content_terms(query_terms(query, limit=len(query or "")))[:limit]


def _validate_lexical_match(lexical_match: Any) -> str:
    if lexical_match not in LEXICAL_MATCHES:
        raise UsageError(f"unknown lexical_match '{lexical_match}' (all|any|auto)")
    return lexical_match


def _content_tokens(raw: str) -> list[str]:
    """Distinct keyword tokens of ``raw`` for the keyword AND / OR queries.

    Tokens are the same whitespace-separated phrases that
    :func:`sanitize_fts_query` quotes; tokens without any word characters
    are dropped, and stopword tokens are dropped unless nothing else is left.
    """
    tokens: dict[str, tuple[str, bool]] = {}
    for token in raw.split():
        cleaned = token.replace('"', "")
        words = _TERM_RE.findall(cleaned)
        if not words:
            continue
        key = cleaned.casefold()
        if key not in tokens:
            tokens[key] = (cleaned, all(is_stopword(word) for word in words))
    selected = [text for text, stop in tokens.values() if not stop]
    return selected or [text for text, _stop in tokens.values()]


def any_terms_fts_query(raw: str) -> str:
    """FTS5 OR query over the non-stopword tokens of ``raw``.

    Tokens are the same whitespace-separated, quoted phrases that
    :func:`sanitize_fts_query` ANDs together; tokens without any word
    characters are dropped, and stopword tokens are dropped unless nothing
    else is left.
    """
    return " OR ".join(f'"{text}"' for text in _content_tokens(raw))


def all_keywords_fts_query(raw: str) -> str:
    """FTS5 AND query over the same tokens as :func:`any_terms_fts_query`.

    This is the strict pass of ``lexical_match="auto"``: a note must contain
    every *keyword*, not every word, so "what is wild yeast" requires "wild"
    and "yeast" but not "what" and "is". ``lexical_match="all"`` keeps
    :func:`sanitize_fts_query` (every token) unchanged.
    """
    return " ".join(f'"{text}"' for text in _content_tokens(raw))


# ---- lexical -------------------------------------------------------------------


def _fts_hits(conn: sqlite3.Connection, match: str, k: int,
              active_filters: SearchFilters | None) -> list[Hit]:
    """Run one FTS5 MATCH expression and return scored hits."""
    filter_sql = ""
    params: list[Any] = [match]
    if active_filters is not None:
        clause, filter_params = _filter_clause(active_filters)
        filter_sql = f"AND fts.doc_id IN (SELECT d.doc_id FROM documents d WHERE {clause})"
        params.extend(filter_params)
    params.append(k)

    rows = conn.execute(
        f"""
        SELECT doc_id,
               title,
               -bm25(fts)              AS score,
               snippet(fts, -1, '', '', ' … ', 12) AS snip
        FROM fts
        WHERE fts MATCH ?
        {filter_sql}
        ORDER BY score DESC, doc_id ASC
        LIMIT ?
        """,  # noqa: S608 - fixed SQL fragments; values remain bound parameters
        params,
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


def _below(extra: list[Hit], floor: float) -> list[Hit]:
    """Rescale ``extra`` scores (best first) to sit strictly below ``floor``.

    Partial-match BM25 scores come from a different FTS query than the
    all-words hits ranked before them; rescaling keeps the combined list's
    scores monotonic (linear fusion min-max normalizes them) without
    changing the order within ``extra``.
    """
    if not extra or extra[0].score < floor:
        return extra
    top = extra[0].score
    if floor > 0 and top > 0:
        factor = (floor / top) * 0.999
        return [replace(hit, score=hit.score * factor) for hit in extra]
    shift = top - floor + max(1e-6, abs(floor) * 1e-3)
    return [replace(hit, score=hit.score - shift) for hit in extra]


def _can_relax(query: str) -> bool:
    """True when the any-keywords query could find rows the all-keywords one does not."""
    return len(_content_tokens(query)) > 1


def _top_up(strict: list[Hit], candidates: list[Hit], k: int) -> tuple[list[Hit], bool]:
    """Append any-words ``candidates`` not already in ``strict``, up to ``k`` hits.

    ``candidates`` should be fetched with depth ``k + len(strict)`` so that,
    after skipping the all-words hits the OR query returns again, enough new
    documents remain.
    """
    seen = {hit.doc_id for hit in strict}
    extra = [hit for hit in candidates if hit.doc_id not in seen][: max(0, k - len(strict))]
    if strict:
        extra = _below(extra, strict[-1].score)
    return strict + extra, bool(extra)


def _lexical_leg(conn: sqlite3.Connection, query: str, k: int,
                 active_filters: SearchFilters | None,
                 lexical_match: str) -> tuple[list[Hit], int | None, bool]:
    """``(hits, strict_count, relaxed)`` for an already-validated query."""
    if lexical_match == "any":
        match = any_terms_fts_query(query)
        return (_fts_hits(conn, match, k, active_filters) if match else []), None, False

    # "all" ANDs every token (historical, byte-identical); "auto" ANDs the
    # keywords only, so stopwords never push a note that contains every
    # keyword into the relaxed tier.
    strict_match = (sanitize_fts_query(query) if lexical_match == "all"
                    else all_keywords_fts_query(query))
    strict = _fts_hits(conn, strict_match, k, active_filters) if strict_match else []
    if lexical_match == "all" or len(strict) >= k or not _can_relax(query):
        return strict, len(strict), False
    candidates = _fts_hits(conn, any_terms_fts_query(query), k + len(strict), active_filters)
    hits, relaxed = _top_up(strict, candidates, k)
    return hits, len(strict), relaxed


def lexical_search(conn: sqlite3.Connection, query: str, k: int = 10, *,
                   filters: SearchFilters | dict | None = None,
                   lexical_match: str = "all") -> list[Hit]:
    """Return FTS5 BM25 matches ordered from strongest to weakest relevance.

    SQLite's ``bm25()`` is lower-is-better and normally negative. The query
    negates it to produce a conventional higher-is-better score, so SQL must
    sort that alias descending. ``filters`` restrict the candidate documents
    inside the same query, so filtered results are never short.

    ``lexical_match`` chooses how words combine: ``"all"`` (default) requires
    every word, ``"any"`` ORs the non-stopword words (BM25 still ranks, so
    documents with more and rarer words come first), and ``"auto"`` returns
    the hits containing every keyword (non-stopword word) first and, when
    there are fewer than ``k``, fills up with any-keyword hits (scaled to
    score below them).
    """
    _validate_k(k)
    _validate_lexical_match(lexical_match)
    cleaned_query = query.strip()
    if not cleaned_query:
        raise EmptyQueryError("query is empty")
    active_filters = _coerce_filters(filters)
    hits, _strict_count, _relaxed = _lexical_leg(
        conn, cleaned_query, k, active_filters, lexical_match
    )
    return hits


# ---- semantic ------------------------------------------------------------------


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


def _vector_blocks(index, filters: SearchFilters | None = None,
                   block_size: int = _VECTOR_BLOCK_SIZE) -> Iterator[tuple[Any, list[int]]]:
    """Yield bounded ``(matrix, row_ids)`` blocks in row-id order.

    With ``filters`` only chunks whose documents pass them are read. The
    filter is evaluated in SQL while paging by row id, so neither the allowed
    id set nor the matrix is ever materialized for the whole corpus, and the
    cost stays linear in the number of rows read.

    The whole lazy scan (and whatever the consumer reads on the same
    connection between blocks, such as row metadata) runs inside one read
    transaction, so every page comes from a single WAL snapshot. Commits by
    other connections (an index job batch, a history write) proceed
    normally and are simply not visible to this scan, instead of failing
    it. The transaction ends when the scan finishes or the generator is
    closed; a transaction the caller already had open is left alone.
    """
    import numpy as np

    if not 1 <= block_size <= 10_000:
        raise UsageError("vector block size must be within 1..10000")
    dim_raw = index.get_meta("model_dim")
    dim = int(dim_raw) if dim_raw else 0
    if not dim:
        return
    if filters is not None:
        clause, params = _filter_clause(filters)
    else:
        clause, params = "1", []
    conn = index.conn
    owned = not conn.in_transaction
    if owned:
        # Deferred: the snapshot is taken by the first SELECT and held until COMMIT.
        conn.execute("BEGIN")
    try:
        start_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
        last_row_id = -1
        while True:
            # CROSS JOIN pins the join order: page through chunks by primary key
            # and fetch each vector with a vec0 rowid point lookup. A plain JOIN
            # lets the planner scan the whole vec0 table for every page.
            rows = conn.execute(
                f"""
                SELECT c.row_id, v.embedding
                FROM chunks c
                CROSS JOIN documents d
                CROSS JOIN vec_float v
                WHERE d.doc_id = c.doc_id
                  AND v.rowid = c.row_id
                  AND c.row_id > ?
                  AND ({clause})
                ORDER BY c.row_id
                LIMIT ?
                """,  # noqa: S608 - fixed SQL fragments; values remain bound parameters
                [last_row_id, *params, block_size],
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
        # Safety net only: inside one snapshot the version cannot move unless
        # the consumer itself wrote through this connection mid-scan.
        end_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
        if start_version != end_version:
            raise DataError("vector index changed during scanning; retry the search")
    finally:
        if owned and conn.in_transaction:
            # Also runs when the generator is closed early or garbage-collected.
            conn.commit()


def _application_side_search(index, query_vector, k: int, precision: str,
                             blocks: Iterable[tuple[Any, list[int]]] | None = None) -> list[Hit]:
    """Exact ranking over bounded vector blocks.

    Used for int8/binary precision and for every filtered search (``blocks``
    then yields only allowed rows). Float scores are cosine similarity, int8
    scores are cosine over quantized vectors, and binary scores are negated
    Hamming distances.
    """
    import numpy as np

    from .embed import quantize_int8

    if precision == "float":
        query_float = np.asarray(query_vector, dtype=np.float64)
        query_norm = np.linalg.norm(query_float) or 1.0
    elif precision == "int8":
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

    def score(matrix):
        if precision == "float":
            values = matrix.astype(np.float64)
            matrix_norms = np.linalg.norm(values, axis=1)
            matrix_norms[matrix_norms == 0] = 1.0
            return (values @ query_float) / (matrix_norms * query_norm)
        if precision == "int8":
            quantized = np.round(matrix.astype(np.float64) / scale).clip(-127, 127)
            matrix_norms = np.linalg.norm(quantized, axis=1)
            matrix_norms[matrix_norms == 0] = 1.0
            return (quantized @ query_quantized) / (matrix_norms * query_norm)
        return -((matrix > 0) != query_bits).sum(axis=1).astype(np.float64)

    block_iter = iter(_vector_blocks(index) if blocks is None else blocks)
    best: list[Hit] = []
    try:
        for matrix, row_ids in block_iter:
            similarities = score(matrix)

            if not np.isfinite(similarities).all():
                raise UsageError("quantized search produced non-finite scores")

            # Only rows that can still reach the top k need metadata. Keeping
            # every score tied with the k-th best preserves the doc_id tie-break.
            candidates = np.arange(len(row_ids))
            if len(row_ids) > k:
                kth = np.partition(similarities, len(row_ids) - k)[len(row_ids) - k]
                candidates = np.nonzero(similarities >= kth)[0]
            if len(best) >= k:
                candidates = candidates[similarities[candidates] >= best[-1].score]
            if candidates.size == 0:
                continue
            candidate_rows = [row_ids[int(position)] for position in candidates]
            metadata = _metadata_for_rows(index, candidate_rows)
            for position, row_id in zip(candidates, candidate_rows):
                doc_id, title, source = metadata.get(row_id, ("", "", None))
                if not doc_id:
                    continue
                best.append(
                    Hit(
                        doc_id=doc_id,
                        title=title,
                        score=float(similarities[int(position)]),
                        snippet="",
                        source=source,
                    )
                )
            best.sort(key=lambda hit: (-hit.score, hit.doc_id))
            del best[k:]
    finally:
        # End the scan's read transaction promptly, also when ranking fails.
        close = getattr(block_iter, "close", None)
        if close is not None:
            close()

    return best


def semantic_search(index, query: str, k: int = 10, precision: str = "float",
                    _query_vector=None, *,
                    filters: SearchFilters | dict | None = None) -> list[Hit]:
    """Return semantic neighbors in float, int8, or binary precision.

    Unfiltered float search uses sqlite-vec KNN. With ``filters`` every
    precision runs an exact block scan restricted to the allowed rows, so a
    restrictive filter never loses matches the way post-filtering a KNN
    top-k would.
    """
    _validate_k(k)
    cleaned_query = query.strip()
    if not cleaned_query:
        raise EmptyQueryError("query is empty")
    if precision not in PRECISIONS:
        raise UsageError(f"unknown precision '{precision}' (float|int8|binary)")
    active_filters = _coerce_filters(filters)

    embedder = _require_embedder(index)
    query_vector = embedder.encode_query(cleaned_query) if _query_vector is None else _query_vector
    if active_filters is not None:
        return _application_side_search(
            index,
            query_vector,
            k,
            precision,
            blocks=_vector_blocks(index, active_filters),
        )
    if precision == "float":
        return _native_float_search(index, query_vector, k)
    return _application_side_search(index, query_vector, k, precision)


# ---- fusion --------------------------------------------------------------------


def rrf_fuse(rankings: list[list[str]], k: int = 60, *,
             prefer: list[str] | None = None) -> list[tuple[str, float]]:
    """Fuse rankings with Reciprocal Rank Fusion (Cormack et al., 2009).

    Exact score ties are broken by position in ``prefer`` (documents missing
    from it come after those in it), then by ``doc_id``. Without ``prefer``
    ties fall straight to ``doc_id``, as before.
    """
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise UsageError("rrf k must be a positive integer")
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    order: dict[str, int] = {}
    for position, doc_id in enumerate(prefer or ()):
        order.setdefault(doc_id, position)
    missing = len(order)
    return sorted(scores.items(),
                  key=lambda item: (-item[1], order.get(item[0], missing), item[0]))


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


# ---- confidence & explanations -------------------------------------------------


def binary_similarity(hamming: float, dim: int) -> float:
    """Map a Hamming distance onto ``[-1, 1]`` as ``1 - 2*hamming/dim``."""
    return 1.0 - 2.0 * float(hamming) / dim if dim else 0.0


def _float_similarities(index, query_vector, doc_ids: list[str]) -> dict[str, float]:
    """doc_id -> float cosine against the stored vectors of a bounded result set.

    Documents without a stored vector (or an index without the lookup) are
    left out, so callers fall back to their own similarity.
    """
    if query_vector is None or not doc_ids:
        return {}
    import numpy as np

    lookup = getattr(index, "vectors_for_docs", None)
    vectors = lookup(doc_ids) if lookup is not None else {}
    query = np.asarray(query_vector, dtype=np.float64)
    query_norm = float(np.linalg.norm(query))
    if not query_norm:
        return {}
    result: dict[str, float] = {}
    for doc_id in doc_ids:
        vector = vectors.get(doc_id)
        if vector is None or vector.shape != query.shape:
            continue
        values = np.asarray(vector, dtype=np.float64)
        norm = float(np.linalg.norm(values))
        cosine = float(values @ query) / (norm * query_norm) if norm else 0.0
        if math.isfinite(cosine):
            result[doc_id] = cosine
    return result


def semantic_confidence(similarity: float, precision: str = "float") -> str:
    """Label a semantic similarity strong / moderate / weak.

    ``precision="binary"`` applies the binary thresholds, which
    :func:`search_detailed` now uses only as a fallback: binary hits are
    labelled by the float cosine of their stored vectors.
    """
    if precision == "binary":
        strong, weak = BINARY_STRONG_SIM, BINARY_WEAK_SIM
    else:
        strong, weak = STRONG_SIM, WEAK_SIM
    if similarity >= strong:
        return "strong"
    if similarity >= weak:
        return "moderate"
    return "weak"


def lexical_confidence(matched: int, total: int, *, partial: bool = False) -> str:
    """All query terms present -> strong, some -> moderate, none -> weak.

    ``partial=True`` labels a hit found by an any-words (relaxed) query: it
    stays moderate only when it matches more than half of the keywords, so
    a lone common word ("renew" in "how do I renew my passport") cannot
    keep an off-topic query from abstaining.
    """
    if total <= 0:
        return "moderate"
    if matched >= total:
        return "strong"
    if partial:
        return "moderate" if 2 * matched > total else "weak"
    return "moderate" if matched > 0 else "weak"


def _term_matches(conn: sqlite3.Connection, terms: list[str],
                  doc_ids: list[str]) -> dict[str, set[str]]:
    """doc_id -> query terms that FTS matches in that document.

    One compound query covers every term, restricted to the result documents.
    """
    if conn is None or not terms or not doc_ids:
        return {}
    ids_json = json.dumps(list(dict.fromkeys(doc_ids)))
    part = (
        "SELECT ? AS term, doc_id FROM fts "
        "WHERE fts MATCH ? AND doc_id IN (SELECT value FROM json_each(?))"
    )
    matches: dict[str, set[str]] = {}

    def _collect(selected: list[str]) -> None:
        params: list[Any] = []
        for term in selected:
            params.extend([term, '"' + term.replace('"', "") + '"', ids_json])
        sql = " UNION ALL ".join(part for _ in selected)
        for row in conn.execute(sql, params).fetchall():
            matches.setdefault(str(row[1]), set()).add(str(row[0]))

    try:
        _collect(terms)
    except sqlite3.OperationalError:
        # A term the tokenizer cannot express must not hide the others.
        for term in terms:
            try:
                _collect([term])
            except sqlite3.OperationalError:
                continue
    return matches


def _keyword_reason(matched: list[str], total: int) -> str:
    if total == 1 and matched:
        return f"Matched the keyword “{matched[0]}”"
    if len(matched) == total:
        return f"Matched all {total} keywords"
    if not matched:
        return "No exact keyword matches"
    return f"Matched {len(matched)} of {total} keyword{'s' if total != 1 else ''}"


def _meaning_reason(similarity: float, confidence: str) -> str:
    if confidence == "strong":
        return f"Very close in meaning ({similarity:.2f})"
    if confidence == "moderate":
        return f"Related in meaning ({similarity:.2f})"
    return f"Only loosely related in meaning ({similarity:.2f})"


def _rounded(value: float | None, digits: int = 6) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(float(value), digits)


# ---- pipeline ------------------------------------------------------------------


class _StageClock:
    """Times pipeline stages and reports them to an optional callback."""

    def __init__(self, callback: StageCallback | None) -> None:
        self.callback = callback
        self.records: list[dict] = []

    @contextmanager
    def stage(self, name: str):
        if self.callback is not None:
            self.callback(name, "start", 0.0)
        started = time.perf_counter()
        yield
        elapsed = (time.perf_counter() - started) * 1000.0
        self.records.append({"name": name, "ms": round(elapsed, 3)})
        if self.callback is not None:
            self.callback(name, "done", elapsed)


def _execute(index, query: str, *, mode: str, k: int, precision: str, rrf_k: int,
             rerank: bool, fusion: str, alpha: float, candidate_depth: int,
             filters: Any, explain: bool, on_stage: StageCallback | None,
             need_confidence: bool, lexical_match: str = "all") -> SearchOutcome:
    clock = _StageClock(on_stage)

    with clock.stage("parse"):
        if mode not in SEARCH_MODES:
            raise UsageError(f"unknown mode '{mode}'")
        _validate_k(k)
        fetch_depth = k
        if mode == "hybrid":
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
        active_filters = _coerce_filters(filters)
        if mode == "hybrid":
            if not 0.0 <= alpha <= 1.0:
                raise UsageError("alpha must be in [0, 1]")
            if fusion not in {"rrf", "linear"}:
                raise UsageError(f"unknown fusion '{fusion}' (rrf|linear)")
            if fusion == "rrf" and (isinstance(rrf_k, bool) or not isinstance(rrf_k, int) or rrf_k < 1):
                raise UsageError("rrf k must be a positive integer")
        if mode != "lexical":
            if precision not in PRECISIONS:
                raise UsageError(f"unknown precision '{precision}' (float|int8|binary)")
            embedder = _require_embedder(index)
        _validate_lexical_match(lexical_match)
        terms = keyword_terms(cleaned_query) if (explain or need_confidence) else []

    filter_kwargs = {"filters": active_filters} if active_filters is not None else {}
    lexical_hits: list[Hit] = []
    semantic_hits: list[Hit] = []
    query_vector = None
    fused_scores: dict[str, float] = {}
    strict_count: int | None = None
    relaxed = False

    if mode in ("lexical", "hybrid"):
        with clock.stage("lexical"):
            if lexical_match == "auto":
                # The same helper lexical_search uses, so strict_count counts
                # the hits of the all-keywords AND query ranked first.
                lexical_hits, strict_count, relaxed = _lexical_leg(
                    index.conn, cleaned_query, fetch_depth, active_filters, lexical_match
                )
            else:
                lexical_hits = lexical_search(index.conn, cleaned_query, k=fetch_depth,
                                              lexical_match=lexical_match, **filter_kwargs)
                strict_count = len(lexical_hits) if lexical_match == "all" else None

    if mode in ("semantic", "hybrid"):
        with clock.stage("embed"):
            query_vector = embedder.encode_query(cleaned_query)
        with clock.stage("semantic"):
            semantic_hits = semantic_search(
                index,
                cleaned_query,
                k=fetch_depth,
                precision=precision,
                _query_vector=query_vector,
                **filter_kwargs,
            )

    if mode == "lexical":
        hits = list(lexical_hits)
    elif mode == "semantic":
        hits = list(semantic_hits)
    else:
        with clock.stage("fuse"):
            if fusion == "rrf":
                # A two-leg rank swap (keyword 1 + meaning 2 vs. keyword 2 +
                # meaning 1) is an exact RRF tie. Settle it by meaning, not by
                # path order, so the note closer in meaning wins over one that
                # merely shares more of the words.
                semantic_ids = [hit.doc_id for hit in semantic_hits]
                fused = rrf_fuse(
                    [[hit.doc_id for hit in lexical_hits], semantic_ids],
                    k=rrf_k,
                    prefer=semantic_ids,
                )
            else:
                fused = linear_fuse(lexical_hits, semantic_hits, alpha)
            fused_scores = dict(fused)

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
            if not rerank:
                fused_hits = fused_hits[:k]
        if rerank:
            with clock.stage("rerank"):
                hits = _rerank_candidates(index, fused_hits, k, query_vector=query_vector)
        else:
            hits = fused_hits

    if not (explain or need_confidence):
        return SearchOutcome(hits=hits, stages=clock.records, relaxed=relaxed,
                             strict_count=strict_count)

    dim = int(getattr(query_vector, "size", 0) or 0)
    lexical_info = {hit.doc_id: (rank, hit.score) for rank, hit in enumerate(lexical_hits, start=1)}
    semantic_info: dict[str, tuple[int, float, float]] = {}
    for rank, hit in enumerate(semantic_hits, start=1):
        similarity = binary_similarity(-hit.score, dim) if precision == "binary" else hit.score
        semantic_info[hit.doc_id] = (rank, hit.score, similarity)
    # Binary similarities of relevant and off-topic notes overlap, so no
    # binary threshold can both keep answers and abstain on off-topic
    # questions. Label binary hits by the float cosine of their stored
    # vector instead (bounded: only this result set); ranking is untouched.
    float_similarity = (
        _float_similarities(index, query_vector, list(semantic_info))
        if precision == "binary" and semantic_info else {}
    )

    term_hits = _term_matches(getattr(index, "conn", None), terms, [hit.doc_id for hit in hits])

    final_hits: list[Hit] = []
    confidences: list[str] = []
    for hit in hits:
        matched = [term for term in terms if term in term_hits.get(hit.doc_id, ())]
        missing = [term for term in terms if term not in term_hits.get(hit.doc_id, ())]
        in_lexical = hit.doc_id in lexical_info
        in_semantic = hit.doc_id in semantic_info
        # Hits beyond the all-words pass come from the relaxed any-words query.
        from_relaxed = in_lexical and (
            strict_count is None or lexical_info[hit.doc_id][0] > strict_count
        )
        lexical_label = (
            lexical_confidence(len(matched), len(terms), partial=from_relaxed)
            if in_lexical else None
        )
        similarity = semantic_info[hit.doc_id][2] if in_semantic else None
        label_similarity, label_precision = similarity, precision
        if in_semantic and hit.doc_id in float_similarity:
            label_similarity, label_precision = float_similarity[hit.doc_id], "float"
        semantic_label = (
            semantic_confidence(label_similarity, label_precision) if in_semantic else None
        )

        if mode == "lexical":
            confidence = lexical_label or "weak"
        elif mode == "semantic":
            confidence = semantic_label or "weak"
        elif in_lexical and in_semantic and (not from_relaxed or len(matched) >= len(terms)):
            confidence = "strong"
        else:
            labels = [label for label in (lexical_label, semantic_label) if label]
            confidence = min(labels, key=CONFIDENCE_LEVELS.index) if labels else "weak"
        confidences.append(confidence)

        if not explain:
            final_hits.append(hit)
            continue

        reasons: list[str] = []
        if terms and (mode == "lexical" or in_lexical or matched):
            reasons.append(_keyword_reason(matched, len(terms)))
        elif terms and mode != "lexical":
            reasons.append("No exact keyword matches")
        if label_similarity is not None:
            reasons.append(_meaning_reason(label_similarity, semantic_label or "weak"))
        if mode == "hybrid":
            if in_lexical and in_semantic:
                reasons.append("Found by both keyword and meaning search")
            elif in_lexical:
                reasons.append("Found by keyword search only")
            else:
                reasons.append("Found by meaning search only")
        if mode != "lexical" and precision in ("int8", "binary"):
            label = "int8" if precision == "int8" else "binary (1-bit)"
            reasons.append(f"Compared using {label} vectors")
        if mode == "hybrid" and rerank:
            reasons.append("Re-ranked by full-precision meaning similarity")

        details: dict[str, Any] = {
            "mode": mode,
            "precision": None if mode == "lexical" else precision,
            "matched_terms": matched,
            "missing_terms": missing,
            "lexical_rank": lexical_info[hit.doc_id][0] if in_lexical else None,
            "lexical_score": _rounded(lexical_info[hit.doc_id][1]) if in_lexical else None,
            "semantic_rank": semantic_info[hit.doc_id][0] if in_semantic else None,
            "semantic_score": _rounded(similarity),
            "fused_score": _rounded(fused_scores.get(hit.doc_id)) if mode == "hybrid" else None,
            "confidence": confidence,
            "reasons": reasons,
        }
        if terms:
            details["keyword_coverage"] = (
                "all" if len(matched) == len(terms) else "some" if matched else "none"
            )
        else:
            details["keyword_coverage"] = None
        if mode != "semantic":
            details["lexical_match"] = lexical_match
        if mode == "hybrid":
            details["fusion"] = fusion
            details["reranked"] = bool(rerank)
            if rerank:
                details["rerank_score"] = _rounded(hit.score)
        if in_semantic and precision == "binary":
            details["hamming_distance"] = int(round(-semantic_info[hit.doc_id][1]))
            # Full-precision cosine used for the confidence label (None when
            # the stored vector was unavailable and the binary label applied).
            details["float_similarity"] = _rounded(float_similarity.get(hit.doc_id))
        final_hits.append(replace(hit, explain=details))

    weak_count = confidences.count("weak")
    return SearchOutcome(
        hits=final_hits,
        stages=clock.records,
        abstained=bool(final_hits) and weak_count == len(final_hits),
        weak_count=weak_count,
        confidences=confidences,
        relaxed=relaxed,
        strict_count=strict_count,
    )


def search(index, query: str, mode: str = "hybrid", k: int = 10,
           precision: str = "float", rrf_k: int = 60,
           rerank: bool = False, fusion: str = "rrf",
           alpha: float = 0.5, candidate_depth: int = 50, *,
           filters: SearchFilters | dict | None = None, explain: bool = False,
           on_stage: StageCallback | None = None,
           lexical_match: str = "all") -> list[Hit]:
    """Dispatch to lexical, semantic, or hybrid retrieval.

    ``filters`` restrict candidates before ranking, ``explain`` fills
    :attr:`Hit.explain`, and ``on_stage(name, "start"|"done", ms)`` reports
    the pipeline stages as they run. ``lexical_match`` ("all", "any", or
    "auto"; see :func:`lexical_search`) controls the keyword leg of lexical
    and hybrid search; semantic mode validates but does not use it.
    """
    return _execute(
        index,
        query,
        mode=mode,
        k=k,
        precision=precision,
        rrf_k=rrf_k,
        rerank=rerank,
        fusion=fusion,
        alpha=alpha,
        candidate_depth=candidate_depth,
        filters=filters,
        explain=explain,
        on_stage=on_stage,
        need_confidence=False,
        lexical_match=lexical_match,
    ).hits


def search_detailed(index, query: str, mode: str = "hybrid", k: int = 10,
                    precision: str = "float", rrf_k: int = 60,
                    rerank: bool = False, fusion: str = "rrf",
                    alpha: float = 0.5, candidate_depth: int = 50, *,
                    filters: SearchFilters | dict | None = None, explain: bool = False,
                    on_stage: StageCallback | None = None,
                    lexical_match: str = "all") -> SearchOutcome:
    """Like :func:`search` but also returns stage timings and confidence.

    ``abstained`` is True when there are hits and every one is weak; the
    hits are still returned in their usual order. With
    ``lexical_match="auto"``, ``relaxed`` reports that too few documents
    contained every word and partial matches were added.
    """
    return _execute(
        index,
        query,
        mode=mode,
        k=k,
        precision=precision,
        rrf_k=rrf_k,
        rerank=rerank,
        fusion=fusion,
        alpha=alpha,
        candidate_depth=candidate_depth,
        filters=filters,
        explain=explain,
        on_stage=on_stage,
        need_confidence=True,
        lexical_match=lexical_match,
    )


def hybrid_search(index, query: str, k: int = 10, precision: str = "float",
                  rrf_k: int = 60, rerank: bool = False,
                  fusion: str = "rrf", alpha: float = 0.5,
                  candidate_depth: int = 50, *,
                  filters: SearchFilters | dict | None = None, explain: bool = False,
                  on_stage: StageCallback | None = None,
                  lexical_match: str = "all") -> list[Hit]:
    """Fuse lexical and semantic legs, optionally reranking the union pool.

    The query is embedded exactly once and the vector is shared by the
    semantic leg and the reranker. ``lexical_match`` selects how the lexical
    leg combines words before fusion (see :func:`lexical_search`).
    """
    return search(
        index,
        query,
        mode="hybrid",
        k=k,
        precision=precision,
        rrf_k=rrf_k,
        rerank=rerank,
        fusion=fusion,
        alpha=alpha,
        candidate_depth=candidate_depth,
        filters=filters,
        explain=explain,
        on_stage=on_stage,
        lexical_match=lexical_match,
    )
