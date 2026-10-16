"""Result snippets: passage selection and safe highlight segments.

Everything here returns plain strings or ``{"text", "highlight"}`` segment
dicts, never markup, so untrusted document text stays text all the way to
the UI. Highlighting matches whole words and simple stems case-insensitively
("running" highlights "run", "runs", "runner"), close to what the FTS porter
tokenizer considers a match. Stopwords ("the", "how", "what's") are never
highlighted or scored unless the query consists only of stopwords.
"""

from __future__ import annotations

import bisect
import re
import unicodedata
from functools import lru_cache
from typing import Any

from .search import is_stopword

FTS_START = ""
FTS_END = ""
ELLIPSIS = "…"

DEFAULT_PASSAGE_CHARS = 320
MAX_SEGMENT_CHARS = 1200
MAX_HIGHLIGHTS = 80
_MAX_BODY_SCAN = 200_000
_MAX_UNITS = 5_000
_MAX_MATCH_CANDIDATES = 200
_MIN_PASSAGE_CHARS = 40
_MIN_PREFIX = 4

_WORD_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=\S)")
_HEADING_RE = re.compile(r"^#{1,6}\s+")
_DECORATION_RE = re.compile(r"^[\s`~|:\-=*_+#>]*$")
# Markdown syntax removed from passages (the stored body is untouched):
# blockquote and bullet / task-list markers, links and images (the text is
# kept), paired bold markers, inline-code backticks and table pipes.
# Numbered-list markers ("1.") carry meaning and stay; a single "_" stays so
# snake_case identifiers survive, and "__x__" without a space ("__init__")
# is treated as an identifier, not bold.
_LIST_MARK_RE = re.compile(r"^(?:>\s*)*(?:[-*+]\s+(?:\[[ xX]\]\s+)?)?")
_BOLD_STARS_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_BOLD_UNDERSCORES_RE = re.compile(r"(?<!\w)__(?=\S)([^_]*?\s[^_]*?)(?<=\S)__(?!\w)")
_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)\s]*\)")
_TABLE_ROW_RE = re.compile(r"^\|.*\|$")
_UNIT_END = (".", ":", ";", "!", "?")

# (suffix, replacement) tried in order; the first that leaves a 3+ char stem wins.
_SUFFIXES = (
    ("ingly", ""),
    ("edly", ""),
    ("ings", ""),
    ("ing", ""),
    ("ers", ""),
    ("er", ""),
    ("ies", "y"),
    ("ied", "y"),
    ("sses", "ss"),
    ("ches", "ch"),
    ("shes", "sh"),
    ("xes", "x"),
    ("zes", "z"),
    ("ed", ""),
    ("ly", ""),
    ("s", ""),
)
# Suffixes whose removal may have eaten a silent "e" or doubled a consonant.
_VERBAL = {"ingly", "edly", "ings", "ing", "ers", "er", "ed"}


def collapse_whitespace(text: str) -> str:
    """Collapse runs of whitespace into single spaces and trim."""
    return re.sub(r"\s+", " ", text or "").strip()


def _fold(word: str) -> str:
    folded = unicodedata.normalize("NFKD", word.casefold().replace("’", "'"))
    return "".join(character for character in folded if not unicodedata.combining(character))


@lru_cache(maxsize=4096)
def stem_variants(word: str) -> frozenset[str]:
    """Light, deterministic stems of a word used for highlight matching.

    Not a full stemmer: it strips common English inflections and returns
    one or two candidate stems ("noted" -> {"not", "note"}).
    """
    folded = _fold(word)
    if folded.endswith("'s"):
        folded = folded[:-2]
    if len(folded) < 4:
        return frozenset({folded})
    for suffix, replacement in _SUFFIXES:
        if not folded.endswith(suffix):
            continue
        if suffix == "s" and folded.endswith(("ss", "us", "is")):
            break
        stem = folded[: -len(suffix)] + replacement
        if len(stem) < 3:
            continue
        variants = {stem}
        if suffix in _VERBAL:
            if stem[-1] == stem[-2] and stem[-1] not in "aeiouylsz":
                variants = {stem[:-1]}
            else:
                variants.add(stem + "e")
        return frozenset(variants)
    return frozenset({folded})


_QueryKey = tuple[str, frozenset[str], tuple[str, ...]]


def _query_keys(query: str) -> list[_QueryKey]:
    """(folded term, stem variants, prefixes) for each distinct query word of 2+ chars.

    Stems of 4+ characters also act as prefixes, so "renew" highlights
    "renewals" and "photosynth" highlights "photosynthesis". Stopwords are
    left out unless every query word is one.
    """
    keys: dict[str, _QueryKey] = {}
    stop: set[str] = set()
    for match in _WORD_RE.findall(query or ""):
        folded = _fold(match)
        if len(folded) >= 2 and folded not in keys:
            variants = stem_variants(match)
            prefixes = tuple(sorted(v for v in variants | {folded} if len(v) >= _MIN_PREFIX))
            keys[folded] = (folded, variants, prefixes)
            if is_stopword(folded):
                stop.add(folded)
    if stop and len(stop) < len(keys):
        return [key for term, key in keys.items() if term not in stop]
    return list(keys.values())


def _match_term(word: str, keys: list[_QueryKey]) -> str | None:
    """The query term a document word matches, if any."""
    folded = _fold(word)
    variants = None
    for term, term_variants, prefixes in keys:
        if folded == term:
            return term
        if variants is None:
            variants = stem_variants(word)
        if variants & term_variants:
            return term
        if any(folded.startswith(prefix) for prefix in prefixes):
            return term
    return None


def _word_matches(text: str, keys: list[_QueryKey],
                  limit: int | None = None) -> list[tuple[int, int, str]]:
    """(start, end, term) for every query-matching word in ``text``."""
    if not keys:
        return []
    found: list[tuple[int, int, str]] = []
    for match in _WORD_RE.finditer(text):
        term = _match_term(match.group(0), keys)
        if term is not None:
            found.append((match.start(), match.end(), term))
            if limit is not None and len(found) >= limit:
                break
    return found


def _clip_to_words(text: str, limit: int) -> tuple[str, bool]:
    """Cut ``text`` to at most ``limit`` chars at a word boundary."""
    if len(text) <= limit:
        return text, False
    cut = text.rfind(" ", 0, limit + 1)
    if cut <= 0:
        # A single token longer than the limit cannot be cut between words.
        return text[:limit], True
    return text[:cut].rstrip(), True


def segments_from_fts(snippet: str) -> list[dict[str, object]]:
    """Split an FTS5 ``snippet()`` string with private-use markers into segments."""
    segments: list[dict[str, object]] = []
    buffer: list[str] = []
    highlighted = False
    for character in snippet or "":
        if character == FTS_START:
            if buffer:
                segments.append({"text": "".join(buffer), "highlight": highlighted})
                buffer = []
            highlighted = True
        elif character == FTS_END:
            if buffer:
                segments.append({"text": "".join(buffer), "highlight": True})
                buffer = []
            highlighted = False
        else:
            buffer.append(character)
    if buffer:
        segments.append({"text": "".join(buffer), "highlight": highlighted})
    return segments or [{"text": "", "highlight": False}]


def segments_with_terms(text: str, query: str, *,
                        max_chars: int = MAX_SEGMENT_CHARS) -> list[dict[str, object]]:
    """Split plain text into highlighted segments for whole-word query matches.

    Whitespace is collapsed, text longer than ``max_chars`` is cut at a word
    boundary (with a trailing ellipsis), and at most ``MAX_HIGHLIGHTS`` words
    are highlighted.
    """
    text = collapse_whitespace(text)
    text, clipped = _clip_to_words(text, max(1, int(max_chars)))
    if clipped:
        text = f"{text} {ELLIPSIS}"
    if not text:
        return []
    matches = _word_matches(text, _query_keys(query), limit=MAX_HIGHLIGHTS)
    if not matches:
        return [{"text": text, "highlight": False}]

    segments: list[dict[str, object]] = []
    cursor = 0
    for start, end, _term in matches:
        if start > cursor:
            segments.append({"text": text[cursor:start], "highlight": False})
        segments.append({"text": text[start:end], "highlight": True})
        cursor = end
    if cursor < len(text):
        segments.append({"text": text[cursor:], "highlight": False})
    return segments


def _clean_line(line: str) -> tuple[str, str]:
    """One stripped Markdown line -> (plain text, line kind).

    The kind is ``"heading"``, ``"item"`` (list item or table row) or
    ``"text"``. Headings and items stand on their own, so
    :func:`_split_units` ends them with punctuation when they have none.
    """
    kind = "heading" if _HEADING_RE.match(line) else "text"
    line = _HEADING_RE.sub("", line)
    unmarked = _LIST_MARK_RE.sub("", line, count=1)
    if kind == "text" and unmarked != line and not line.startswith(">"):
        kind = "item"
    line = unmarked
    if _TABLE_ROW_RE.match(line):
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        cells = [cell for cell in cells if cell and not _DECORATION_RE.match(cell)]
        line = ": ".join(cells) if len(cells) == 2 else " · ".join(cells)
        kind = "item"
    line = _LINK_RE.sub(r"\1", line)
    line = _BOLD_STARS_RE.sub(r"\1", line)
    line = _BOLD_UNDERSCORES_RE.sub(r"\1", line)
    line = line.replace("`", "")
    return line.strip(), kind


def _split_units(body: str) -> list[str]:
    """Lines and sentences of a document, whitespace-collapsed, decoration dropped.

    Markdown syntax is reduced to plain text (see :func:`_clean_line`).
    A heading without final punctuation gets a colon and a list item or
    table row a full stop, so a passage reads "Summary: This control ..."
    instead of running the heading into the next sentence.
    """
    units: list[str] = []
    for line in body.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line, kind = _clean_line(line.strip())
        if not line or _DECORATION_RE.match(line):
            continue
        if kind != "text" and not line.endswith(_UNIT_END):
            line += ":" if kind == "heading" else "."
        for sentence in _SENTENCE_SPLIT_RE.split(line):
            sentence = collapse_whitespace(sentence)
            if sentence:
                units.append(sentence)
                if len(units) >= _MAX_UNITS:
                    return units
    return units


def best_passage(body: str, query: str, *, max_chars: int = DEFAULT_PASSAGE_CHARS) -> str:
    """The most informative passage of ``body`` for ``query``.

    Picks the window of at most ``max_chars`` characters that covers the most
    distinct query terms (then the most matches, then the earliest position),
    preferring windows that start at a sentence/line and end at one. Cuts
    happen only between words; ``…`` marks text omitted before or after.
    """
    max_chars = max(_MIN_PASSAGE_CHARS, int(max_chars))
    units = _split_units((body or "")[:_MAX_BODY_SCAN])
    if not units:
        return ""
    text = " ".join(units)
    if len(text) <= max_chars:
        return text

    unit_starts: list[int] = []
    unit_ends: list[int] = []
    offset = 0
    for unit in units:
        unit_starts.append(offset)
        unit_ends.append(offset + len(unit))
        offset += len(unit) + 1

    matches = _word_matches(text, _query_keys(query))
    match_starts = [start for start, _end, _term in matches]

    candidates = set(unit_starts)
    for start, _end, _term in matches[:_MAX_MATCH_CANDIDATES]:
        lead = max(0, start - max_chars // 4)
        if lead > 0 and not text[lead - 1].isspace():
            next_space = text.find(" ", lead)
            lead = next_space + 1 if 0 <= next_space < start else start
        candidates.add(lead)
    if not matches:
        candidates = {0}

    unit_start_set = set(unit_starts)
    best_key: tuple | None = None
    best_span = (0, 0)
    for start in sorted(candidates):
        limit = start + max_chars
        end_index = bisect.bisect_right(unit_ends, limit) - 1
        sentence_end = unit_ends[end_index] if end_index >= 0 else -1
        if sentence_end > start and sentence_end - start >= max_chars * 0.6:
            end = sentence_end
        elif limit >= len(text):
            end = len(text)
        else:
            cut = text.rfind(" ", start, limit + 1)
            end = cut if cut > start else limit
        first = bisect.bisect_left(match_starts, start)
        last = bisect.bisect_left(match_starts, end)
        inside = [term for mstart, mend, term in matches[first:last] if mend <= end]
        key = (len(set(inside)), len(inside), start in unit_start_set, -start)
        if best_key is None or key > best_key:
            best_key = key
            best_span = (start, end)

    start, end = best_span
    passage = text[start:end].strip()
    if start > 0:
        passage = f"{ELLIPSIS} {passage}"
    if end < len(text):
        passage = f"{passage} {ELLIPSIS}"
    return passage


def _hint_window(hint: str, body: str | None, query: str, max_chars: int) -> str:
    """Best window of a matched passage, with ``…`` where the note goes on."""
    window = best_passage(hint, query, max_chars=max_chars)
    if not window or not body:
        return window
    stripped = hint.strip()
    located = body.find(stripped) if stripped else -1
    before = located > 0 and bool(body[:located].strip())
    after = located >= 0 and bool(body[located + len(stripped):].strip())
    if located < 0:
        before = after = True
    if before and not window.startswith(ELLIPSIS):
        window = f"{ELLIPSIS} {window}"
    if after and not window.endswith(ELLIPSIS):
        window = f"{window} {ELLIPSIS}"
    return window


def build_snippet(hit: Any, body: str | None, query: str, *,
                  max_chars: int = DEFAULT_PASSAGE_CHARS,
                  passage: str | None = None) -> list[dict[str, object]]:
    """Highlight segments for one search result.

    Uses the best passage of the document body; falls back to the FTS
    ``snippet()`` of a lexical hit when the passage has no highlightable
    match (porter stemming can match forms the light stemmer does not) or
    when no body is available. An FTS snippet that only repeats the hit's
    title (the query matched the title column, not the body) is skipped in
    favour of the body passage, which the card does not already show.

    ``passage`` (default: the hit's ``passage`` attribute, set by meaning
    search for notes split into passages) is the part of a long note that
    matched by meaning. It is tried first, so a meaning match shows the
    relevant part of the note rather than its first lines; it is also used
    unhighlighted when neither it nor the body contains a query word.
    """
    hint = passage if passage is not None else _attr(hit, "passage")
    hint_segments: list[dict[str, object]] = []
    if hint and hint.strip():
        window = _hint_window(hint, body, query, max_chars)
        hint_segments = segments_with_terms(window, query, max_chars=len(window) or 1)
        if any(segment["highlight"] for segment in hint_segments):
            return hint_segments
    fts_snippet = _attr(hit, "snippet")
    segments: list[dict[str, object]] = []
    if body and body.strip():
        passage_text = best_passage(body, query, max_chars=max_chars)
        segments = segments_with_terms(passage_text, query, max_chars=len(passage_text) or 1)
        if any(segment["highlight"] for segment in segments):
            return segments
    if (fts_snippet and FTS_START in fts_snippet
            and not (segments and _repeats_title(fts_snippet, _attr(hit, "title")))):
        return _unmark_stopwords(segments_from_fts(fts_snippet), query)
    if hint_segments:
        # Nothing to highlight in the body: the passage that matched by
        # meaning beats the note's first lines.
        return hint_segments
    if fts_snippet and not segments:
        return _unmark_stopwords(segments_from_fts(fts_snippet), query)
    return segments


def _attr(hit: Any, name: str) -> str:
    value = hit.get(name) if isinstance(hit, dict) else getattr(hit, name, None)
    return value if isinstance(value, str) else ""


def _repeats_title(fts_snippet: str, title: str) -> bool:
    """True when an FTS snippet's text is (part of) the document title."""
    plain = fts_snippet.replace(FTS_START, "").replace(FTS_END, "").replace(ELLIPSIS, "")
    plain = collapse_whitespace(plain).casefold()
    return bool(plain) and plain in collapse_whitespace(title).casefold()


def _unmark_stopwords(segments: list[dict[str, object]], query: str) -> list[dict[str, object]]:
    """Drop FTS highlights on stopwords (FTS marks every matched token)."""
    keys = _query_keys(query)
    if not keys or all(is_stopword(term) for term, _variants, _prefixes in keys):
        return segments
    result: list[dict[str, object]] = []
    for segment in segments:
        text = str(segment["text"])
        if segment["highlight"] and is_stopword(_fold(text.strip())):
            segment = {"text": text, "highlight": False}
        if result and not segment["highlight"] and not result[-1]["highlight"]:
            result[-1] = {"text": str(result[-1]["text"]) + text, "highlight": False}
        else:
            result.append(segment)
    return result
