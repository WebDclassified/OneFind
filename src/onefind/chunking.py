"""Passage chunking: split long notes so meaning search can see all of them.

The embedding model reads only about the first 256 word pieces of a text
(roughly 200 English words), so a note embedded as one vector is invisible
to meaning search after that point. With chunking on, a note longer than
``max_words`` words is split into overlapping passages of at most
``max_words`` words, each embedded as ``title + "\\n" + passage``; search
then ranks a note by its best passage.

Rules (see docs/11-implementation-contract.md section 9):

- Passages are exact slices of the note body, cut only between words, and
  preferably at paragraph, heading, list-item and sentence boundaries.
- A note with at most ``max_words`` words is exactly one passage whose text
  is the whole body (byte-identical to the whole-note behaviour).
- Consecutive passages overlap by up to ``overlap_words`` words of whole
  sentences/lines, so an answer spanning a boundary is still seen whole.
- A passage never ends with a heading when more text follows, and no
  overlap is carried into a passage that starts with a heading.
- At most :data:`MAX_PASSAGES` passages per note (about 160 000 words);
  text beyond that is still found by keyword search.

The setting is stored per index in ``schema_meta`` as JSON
(``{"version": 1, "max_words": 200, "overlap_words": 40}``) or ``"off"``;
this module only parses and applies it. No third-party imports.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .errors import UsageError

__all__ = [
    "CHUNKING_VERSION",
    "DEFAULT_CHUNKING",
    "MAX_PASSAGES",
    "chunking_from_meta",
    "chunking_to_meta",
    "count_words",
    "normalize_chunking",
    "split_passages",
]

CHUNKING_VERSION = 1
# Calibrated 2026-10-14 with all-MiniLM-L6-v2 (256 word-piece window) on
# showcase-data: 200/40 matched or beat whole-note vectors on hit@1/hit@3/MRR
# in hybrid and semantic mode, while 160/40 and section-sized passages (a
# break before every heading) lost hit@3 (see docs/11 section 9).
DEFAULT_MAX_WORDS = 200
DEFAULT_OVERLAP_WORDS = 40
DEFAULT_CHUNKING: dict[str, int] = {
    "version": CHUNKING_VERSION,
    "max_words": DEFAULT_MAX_WORDS,
    "overlap_words": DEFAULT_OVERLAP_WORDS,
}
MIN_MAX_WORDS = 16
MAX_MAX_WORDS = 4000
MAX_PASSAGES = 1000
OFF = "off"

# A "word" is a run of non-space characters, except that CJK ideographs and
# kana/hangul count one word each (those scripts do not separate words with
# spaces, so a whole paragraph would otherwise be one unsplittable "word").
_CJK = "぀-ヿ㐀-䶿一-鿿가-힯豈-﫿"
_WORD_RE = re.compile(rf"[{_CJK}]|[^\s{_CJK}]+")
_LINE_RE = re.compile(r"[^\r\n]+")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}(?:\s|$)")
_ITEM_RE = re.compile(r"^\s*(?:[-*+]\s|\d{1,9}[.)]\s|>|\|)")
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
# A sentence ends at . ! ? … (or CJK 。！？), optionally followed by closing
# quotes/brackets, then whitespace (or straight after CJK punctuation).
_SENTENCE_END_RE = re.compile(r"(?:[.!?…][\"'”’)\]]*\s+|[。！？][\"'”’)\]」』]*\s*)")


def count_words(text: str) -> int:
    """Words in ``text`` as the splitter counts them (CJK characters count one each)."""
    return sum(1 for _ in _WORD_RE.finditer(text or ""))


def _validate_int(name: str, value: Any, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise UsageError(f"chunking '{name}' must be an integer within {low}..{high}")
    return value


def normalize_chunking(config: Any) -> dict[str, int] | None:
    """Validate a chunking setting; returns a canonical dict or None (off).

    Accepts None/False/``"off"`` (off), True/``"on"``/``"default"`` (the
    default setting), a dict with ``max_words``/``overlap_words`` (and
    optionally ``version``), or the JSON text of such a dict. Raises
    :class:`UsageError` for anything else.
    """
    if config is None or config is False:
        return None
    if config is True:
        return dict(DEFAULT_CHUNKING)
    if isinstance(config, str):
        text = config.strip()
        if text.lower() in ("", OFF, "none", "false"):
            return None
        if text.lower() in ("on", "default", "true"):
            return dict(DEFAULT_CHUNKING)
        try:
            config = json.loads(text)
        except ValueError as exc:
            raise UsageError(f"invalid chunking setting {text!r}") from exc
        if not isinstance(config, dict):
            raise UsageError("a chunking setting must be a JSON object, 'off' or 'default'")
    if not isinstance(config, dict):
        raise UsageError("a chunking setting must be a dict, 'off' or 'default'")
    unknown = sorted(str(key) for key in config if key not in DEFAULT_CHUNKING)
    if unknown:
        raise UsageError(f"unknown chunking option '{unknown[0]}'")
    version = config.get("version", CHUNKING_VERSION)
    if version != CHUNKING_VERSION:
        raise UsageError(f"unsupported chunking version {version!r} (this build: {CHUNKING_VERSION})")
    max_words = _validate_int(
        "max_words", config.get("max_words", DEFAULT_MAX_WORDS), MIN_MAX_WORDS, MAX_MAX_WORDS
    )
    overlap = _validate_int(
        "overlap_words",
        config.get("overlap_words", min(DEFAULT_OVERLAP_WORDS, max_words // 2)),
        0,
        max_words // 2,
    )
    return {"version": CHUNKING_VERSION, "max_words": max_words, "overlap_words": overlap}


def chunking_to_meta(config: dict[str, int] | None) -> str:
    """The ``schema_meta`` text for a normalized setting (``"off"`` when None)."""
    if config is None:
        return OFF
    return json.dumps(config, sort_keys=True, separators=(",", ":"))


def chunking_from_meta(raw: str | None) -> dict[str, int] | None:
    """Parse a stored setting; absent, ``"off"`` or unreadable values mean off."""
    if raw is None:
        return None
    try:
        return normalize_chunking(raw)
    except UsageError:
        # A setting written by a newer build: whole-note vectors are the
        # safe reading (search still works; a rebuild will be suggested).
        return None


@dataclass(frozen=True)
class _Unit:
    start: int
    end: int
    words: int
    kind: str  # "heading" | "item" | "text"


def _word_spans(text: str, start: int, end: int) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _WORD_RE.finditer(text, start, end)]


def _sentence_units(body: str, start: int, end: int, kind: str) -> list[_Unit]:
    """Sentence units of ``body[start:end]`` (whitespace never belongs to a unit)."""
    spans: list[tuple[int, int]] = []
    cursor = start
    for match in _SENTENCE_END_RE.finditer(body, start, end):
        spans.append((cursor, match.end()))
        cursor = match.end()
    if cursor < end:
        spans.append((cursor, end))
    units: list[_Unit] = []
    for low, high in spans:
        words = _word_spans(body, low, high)
        if words:
            units.append(_Unit(words[0][0], words[-1][1], len(words), kind))
    return units


def _units(body: str, max_words: int) -> list[_Unit]:
    """Split ``body`` into ordered, non-overlapping units.

    Units are sentences of prose paragraphs, and whole lines for headings,
    list items, quotes, table rows and fenced code. A unit longer than half
    a passage (a run-on sentence, an unpunctuated transcript) is cut into
    word-aligned pieces of ``max_words // 8`` words so passages still pack
    and overlap evenly.
    """
    units: list[_Unit] = []
    run: list[int] | None = None  # [start, end] of consecutive prose lines
    in_fence = False

    def flush() -> None:
        nonlocal run
        if run is not None:
            units.extend(_sentence_units(body, run[0], run[1], "text"))
            run = None

    for match in _LINE_RE.finditer(body):
        line = match.group(0)
        if not line.strip():
            flush()
            continue
        fence = bool(_FENCE_RE.match(line))
        if fence or in_fence:
            flush()
            if fence:
                in_fence = not in_fence
            units.extend(_sentence_units(body, match.start(), match.end(), "item"))
        elif _HEADING_RE.match(line):
            flush()
            units.extend(_sentence_units(body, match.start(), match.end(), "heading"))
        elif _ITEM_RE.match(line):
            flush()
            units.extend(_sentence_units(body, match.start(), match.end(), "item"))
        elif run is None:
            run = [match.start(), match.end()]
        else:
            run[1] = match.end()
    flush()

    piece = max(1, max_words // 8)
    result: list[_Unit] = []
    for unit in units:
        if unit.words <= max_words // 2:
            result.append(unit)
            continue
        words = _word_spans(body, unit.start, unit.end)
        for offset in range(0, len(words), piece):
            group = words[offset : offset + piece]
            result.append(_Unit(group[0][0], group[-1][1], len(group), unit.kind))
    return result


def split_passages(body: str, config: dict[str, int] | None) -> list[str]:
    """Passages of ``body`` under a normalized chunking ``config``.

    Returns ``[body]`` unchanged when chunking is off or the body has at
    most ``max_words`` words; otherwise slices of ``body`` in order.
    """
    body = body if isinstance(body, str) else str(body or "")
    if config is None:
        return [body]
    max_words = int(config["max_words"])
    overlap = int(config["overlap_words"])
    if count_words(body) <= max_words:
        return [body]
    units = _units(body, max_words)
    if not units:
        return [body]

    passages: list[str] = []
    total = len(units)
    start = 0  # first unit of the passage being built (may be overlap)
    fresh = 0  # first unit not yet in any passage
    while fresh < total and len(passages) < MAX_PASSAGES:
        end = start
        words = 0
        while end < total:
            unit = units[end]
            if end > fresh and words + unit.words > max_words:
                break
            words += unit.words
            end += 1
        # Never end on a heading when text follows: it belongs to what comes next.
        if end < total and end - 1 > fresh and units[end - 1].kind == "heading":
            end -= 1
        passages.append(body[units[start].start : units[end - 1].end])
        fresh = end
        if fresh >= total:
            break
        if overlap <= 0 or units[fresh].kind == "heading":
            start = fresh
            continue
        budget = min(overlap, max_words - units[fresh].words)
        back = fresh
        carried = 0
        while back - 1 >= start + 1 and carried + units[back - 1].words <= budget:
            back -= 1
            carried += units[back].words
        start = back
    return passages
