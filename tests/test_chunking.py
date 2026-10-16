"""Passage chunking (docs/11 section 9): splitter, storage, search, rebuild, jobs.

Most tests use deterministic fake embedders; the section at the end proves
the point of the feature with the real MiniLM model (skipped without the
[model] extra): an answer that lives only at the end of a long note is found
by meaning search with chunking on and missed with it off.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import threading
import types

import numpy as np
import pytest

from onefind import chunking as chunking_module
from onefind import store as store_module
from onefind.chunking import (
    DEFAULT_CHUNKING,
    MAX_PASSAGES,
    chunking_from_meta,
    chunking_to_meta,
    count_words,
    normalize_chunking,
    split_passages,
)
from onefind.errors import DataError, OperationCancelled, UsageError
from onefind.search import Hit, search, search_detailed, semantic_search
from onefind.snippets import ELLIPSIS, build_snippet
from onefind.store import Document, Index

pytest.importorskip("sqlite_vec")

SMALL = {"max_words": 20, "overlap_words": 5}
WORD_RE = re.compile(r"\S+")


# ---- helpers ------------------------------------------------------------------------


def filler(count: int, start: int = 0) -> str:
    """``count`` seven-word sentences with distinct numbers (no axis keywords)."""
    return " ".join(f"Sentence {start + i} talks about calm filler topics." for i in range(count))


class AxisEmbedder:
    """Bag-of-keywords vectors: similarity reflects which keywords a text has."""

    name = "axis-fake"
    revision = "axis-1"
    AXES = ("orbit", "compost", "bread", "glacier")
    dimension = len(AXES) + 1

    def __init__(self) -> None:
        self.encoded: list[str] = []
        self.lock = threading.Lock()

    def _vector(self, text: str) -> np.ndarray:
        lowered = text.lower()
        vector = np.array([lowered.count(word) for word in self.AXES] + [0.3], dtype=np.float32)
        return vector / np.linalg.norm(vector)

    def encode_documents(self, texts):
        with self.lock:
            self.encoded.extend(texts)
        return np.stack([self._vector(text) for text in texts])

    def encode_query(self, text):
        return self._vector(text)


class HashEmbedder(AxisEmbedder):
    """Distinct pseudo-random unit vectors per text (no ties)."""

    name = "hash-fake"
    revision = "hash-1"
    dimension = 6

    def _vector(self, text: str) -> np.ndarray:
        digest = hashlib.sha256(text.encode("utf-8")).digest()[: self.dimension]
        vector = np.frombuffer(digest, dtype=np.uint8).astype(np.float32) + 1.0
        return vector / np.linalg.norm(vector)


def words_of(text: str) -> list[str]:
    return WORD_RE.findall(text)


def chunk_rows(index: Index, doc_id: str) -> list[tuple[int, int, str]]:
    return [
        (int(row[0]), int(row[1]), str(row[2]))
        for row in index.conn.execute(
            "SELECT row_id, seq, text FROM chunks WHERE doc_id = ? ORDER BY seq", (doc_id,)
        )
    ]


def vector_rows(index: Index) -> set[int]:
    return {int(row[0]) for row in index.conn.execute("SELECT rowid FROM vec_float")}


def small_index(tmp_path, name="small.db", embedder=None, config=SMALL) -> Index:
    index = Index.open(tmp_path / name)
    index.set_chunking(config)
    if embedder is not None:
        index.attach_embedder(embedder)
    return index


# ---- 1. settings ----------------------------------------------------------------------


def test_normalize_chunking_accepts_the_documented_forms():
    assert normalize_chunking(None) is None
    assert normalize_chunking(False) is None
    assert normalize_chunking("off") is None and normalize_chunking(" OFF ") is None
    assert normalize_chunking(True) == DEFAULT_CHUNKING
    assert normalize_chunking("default") == DEFAULT_CHUNKING
    assert normalize_chunking("on") == DEFAULT_CHUNKING
    assert normalize_chunking({"max_words": 50}) == {
        "version": 1, "max_words": 50, "overlap_words": 25,
    }
    assert normalize_chunking('{"max_words": 64, "overlap_words": 0}') == {
        "version": 1, "max_words": 64, "overlap_words": 0,
    }
    assert DEFAULT_CHUNKING == {"version": 1, "max_words": 200, "overlap_words": 40}


@pytest.mark.parametrize(
    "bad",
    [
        {"max_words": 8},
        {"max_words": 100, "overlap_words": 51},
        {"max_words": 100, "overlap_words": -1},
        {"max_words": True},
        {"max_words": 100.0},
        {"version": 2, "max_words": 100},
        {"size": 100},
        "[1, 2]",
        "not json",
        42,
    ],
)
def test_normalize_chunking_rejects_invalid_settings(bad):
    with pytest.raises(UsageError):
        normalize_chunking(bad)


def test_meta_round_trip_and_unreadable_values():
    for config in (None, DEFAULT_CHUNKING, normalize_chunking(SMALL)):
        assert chunking_from_meta(chunking_to_meta(config)) == config
    assert chunking_to_meta(None) == "off"
    assert chunking_from_meta(None) is None
    assert chunking_from_meta('{"version": 9, "max_words": 50}') is None  # newer build
    assert chunking_from_meta("garbage{") is None


# ---- 2. splitter -------------------------------------------------------------------------


def assert_well_formed(body: str, passages: list[str], max_words: int) -> None:
    """Slices in order, word-aligned, bounded, and together covering every word."""
    words = [(m.start(), m.end()) for m in chunking_module._WORD_RE.finditer(body)]
    starts = {start for start, _end in words}
    ends = {end for _start, end in words}
    spans: list[tuple[int, int]] = []
    cursor = 0
    for passage in passages:
        start = body.find(passage, cursor)
        assert start >= 0, "a passage must be an exact slice of the body, in order"
        end = start + len(passage)
        assert start in starts and end in ends, "passages start and end on word boundaries"
        assert count_words(passage) <= max_words
        if spans:
            assert start > spans[-1][0] and end > spans[-1][1], "passages move forward"
        spans.append((start, end))
        cursor = start + 1
    for word_start, word_end in words:
        assert any(start <= word_start and word_end <= end for start, end in spans)


def test_off_or_short_notes_are_one_unchanged_passage():
    for body in ("", "   ", "one", "tiny note\r\n\r\nwith CRLF\r\n", filler(2)):
        assert split_passages(body, None) == [body]
        assert split_passages(body, DEFAULT_CHUNKING) == [body]
    config = normalize_chunking(SMALL)
    exact = " ".join(f"w{i}" for i in range(20))
    assert split_passages(exact, config) == [exact]
    assert len(split_passages(exact + " w20", config)) == 2


def test_long_note_splits_into_overlapping_word_aligned_passages():
    config = normalize_chunking({"max_words": 20, "overlap_words": 10})
    body = filler(12)  # 84 words in 7-word sentences
    passages = split_passages(body, config)
    assert len(passages) >= 6
    assert_well_formed(body, passages, 20)
    assert passages[0].startswith("Sentence 0 ") and passages[-1].endswith("topics.")
    # Consecutive passages share whole sentences (overlap), never half words.
    for left, right in zip(passages, passages[1:]):
        first_sentence = right.split(".")[0] + "."
        assert left.endswith(first_sentence)
    assert split_passages(body, config) == passages  # deterministic
    # Overlap is whole sentences: one longer than the overlap budget is not repeated.
    assert " ".join(split_passages(body, normalize_chunking(SMALL))) == body


def test_overlap_zero_means_disjoint_passages():
    config = normalize_chunking({"max_words": 20, "overlap_words": 0})
    body = filler(12)
    passages = split_passages(body, config)
    assert " ".join(passages) == body
    assert sum(count_words(p) for p in passages) == count_words(body)


def test_one_huge_word_is_never_cut():
    config = normalize_chunking(SMALL)
    huge = "x" * 50_000
    assert split_passages(huge, config) == [huge]
    body = f"{filler(3)} {huge} {filler(3, start=10)}"
    passages = split_passages(body, config)
    assert sum(huge in p for p in passages) >= 1
    assert all(("x" not in p) or (huge in p) for p in passages)
    assert_well_formed(body, passages, 20)


def test_unpunctuated_text_is_cut_between_words_with_overlap():
    config = normalize_chunking(SMALL)
    body = " ".join(f"token{i}" for i in range(100))
    passages = split_passages(body, config)
    assert all(count_words(p) <= 20 for p in passages)
    assert all(p.split()[0].startswith("token") for p in passages)
    assert {word for p in passages for word in p.split()} == set(body.split())
    assert passages[1].split()[0] in passages[0].split()  # overlap


def test_unicode_and_cjk_text():
    config = normalize_chunking(SMALL)
    accented = " ".join(["café naïve façade résumé"] * 15)
    passages = split_passages(accented, config)
    assert all(set(p.split()) <= {"café", "naïve", "façade", "résumé"} for p in passages)
    cjk = "日本語の文章です。" * 12  # no spaces: every ideograph/kana is a word
    assert count_words("日本語") == 3
    passages = split_passages(cjk, config)
    assert len(passages) > 1 and all(p in cjk for p in passages)
    assert all(count_words(p) <= 20 for p in passages)


def test_crlf_paragraphs_keep_original_text():
    config = normalize_chunking(SMALL)
    body = "\r\n\r\n".join(filler(2, start=i * 2) for i in range(6))
    passages = split_passages(body, config)
    assert len(passages) > 1
    for passage in passages:
        assert passage in body and passage == passage.strip()
        assert not passage.startswith(("\r", "\n")) and not passage.endswith(("\r", "\n"))


def test_headings_and_list_items_are_respected():
    config = normalize_chunking(SMALL)
    body = (
        "# Overview\n\n" + filler(2) + "\n\n## Steps\n\n"
        + "\n".join(f"- step {i} keeps the line together" for i in range(6))
        + "\n\n## Outcome\n\n" + filler(2, start=20)
    )
    passages = split_passages(body, config)
    assert_well_formed(body, passages, 20)
    for passage in passages[:-1]:
        last_line = passage.splitlines()[-1]
        assert not last_line.startswith("#"), "a passage must not end on a heading"
    # A list item is never split across passages.
    for passage in passages:
        for line in passage.splitlines():
            if line.startswith("- step"):
                assert line.endswith("together")
    starts_with_heading = [p for p in passages if p.startswith("## Outcome")]
    assert starts_with_heading, "no overlap is carried into a new section"


def test_passage_cap(monkeypatch):
    monkeypatch.setattr(chunking_module, "MAX_PASSAGES", 3)
    passages = split_passages(filler(40), normalize_chunking(SMALL))
    assert len(passages) == 3
    assert MAX_PASSAGES == 1000


# ---- 3. storage ---------------------------------------------------------------------------


def test_new_collections_default_on_and_legacy_ones_stay_off(tmp_path):
    with Index.open(tmp_path / "new.db") as index:
        assert index.chunking_config() == DEFAULT_CHUNKING
        assert index.vectors_need_rebuild()["needed"] is False
        index.conn.execute("DELETE FROM schema_meta WHERE key IN ('chunking', 'chunking_built')")
        index.conn.commit()
    with Index.open(tmp_path / "new.db") as index:  # e.g. a pre-v1.2.1 collection
        assert index.chunking_config() is None
        index.add_documents([Document("long", filler(80), "Long")])
        assert len(chunk_rows(index, "long")) == 1  # whole note, as before


def test_long_note_gets_passages_vectors_and_healthy_counts(tmp_path):
    embedder = AxisEmbedder()
    with small_index(tmp_path, embedder=embedder) as index:
        body = filler(12)
        index.add_documents([Document("long", body, "Long title"), Document("short", "tiny", "S")])
        rows = chunk_rows(index, "long")
        passages = split_passages(body, normalize_chunking(SMALL))
        assert [seq for _row, seq, _text in rows] == list(range(len(passages)))
        assert [text for _row, _seq, text in rows] == passages
        assert chunk_rows(index, "short")[0][2] == "tiny"
        # Every passage is embedded with the title for context.
        assert embedder.encoded == [f"Long title\n{p}" for p in passages] + ["S\ntiny"]
        assert vector_rows(index) == {row for row, _seq, _text in rows} | {
            chunk_rows(index, "short")[0][0]
        }
        health = index.health()
        assert health["ok"] is True
        assert health["documents"] == health["chunks"] == health["fts"] == 2
        assert health["vectors"] == 2 and health["missing_vectors"] == 0
        assert health["passages"] == len(passages) + 1
        assert health["passage_vectors"] == len(passages) + 1
        stats = index.stats()
        assert stats["chunks"] == stats["passages"] == len(passages) + 1
        assert stats["vectors"] == len(passages) + 1
        assert stats["long_notes"] == 1
        assert stats["chunking"] == normalize_chunking(SMALL)
        assert stats["vectors_need_rebuild"]["needed"] is False
        assert index.passage_info(["long", "short", "nope"]) == {"long": len(passages), "short": 1}
        vectors = index.vectors_for_docs(["long", "short"])
        assert vectors["long"].shape == (len(passages), AxisEmbedder.dimension)
        assert vectors["short"].shape == (AxisEmbedder.dimension,)


def test_upsert_replaces_passages_and_vectors_atomically(tmp_path):
    embedder = AxisEmbedder()
    with small_index(tmp_path, embedder=embedder) as index:
        index.add_documents([Document("doc", filler(12), "T")])
        first = chunk_rows(index, "doc")
        index.add_documents([Document("doc", filler(5), "T")])  # shorter, still long
        second = chunk_rows(index, "doc")
        assert len(second) < len(first)
        assert second[0][0] == first[0][0]  # seq 0 keeps its row id
        assert index.health()["ok"] is True and index.health()["orphan_vectors"] == 0
        index.add_documents([Document("doc", "now short", "T")])
        third = chunk_rows(index, "doc")
        assert third == [(first[0][0], 0, "now short")]
        assert vector_rows(index) == {first[0][0]}
        assert index.health()["ok"] is True


def test_editing_a_long_note_reembeds_only_changed_passages(tmp_path):
    embedder = AxisEmbedder()
    with small_index(tmp_path, embedder=embedder) as index:
        body = filler(12)
        index.add_documents([Document("doc", body, "T")])
        total = len(chunk_rows(index, "doc"))
        embedder.encoded.clear()
        index.add_documents([Document("doc", body + " A brand new final sentence about glacier.", "T")])
        assert 1 <= len(embedder.encoded) < total
        assert index.health()["ok"] is True
        hits = semantic_search(index, "glacier", k=1)
        assert hits[0].doc_id == "doc" and "glacier" in hits[0].passage


def test_remove_documents_drops_every_passage_and_vector(tmp_path):
    with small_index(tmp_path, embedder=AxisEmbedder()) as index:
        index.add_documents([Document("a", filler(12), "A"), Document("b", filler(12, 50), "B")])
        assert index.remove_documents(["a"]) == 1
        assert chunk_rows(index, "a") == []
        assert vector_rows(index) == {row for row, _s, _t in chunk_rows(index, "b")}
        assert index.health()["ok"] is True


def test_health_flags_orphans_and_missing_passages_and_repair_fixes_them(tmp_path):
    embedder = AxisEmbedder()
    with small_index(tmp_path, embedder=embedder) as index:
        index.add_documents([Document("a", filler(12), "A"), Document("b", filler(12, 50), "B")])
        rows_b = chunk_rows(index, "b")
        index.conn.execute("PRAGMA foreign_keys=OFF")
        # Damage: a passage of b goes missing (with its vector), a's middle
        # passage text goes stale, and an orphan passage appears.
        index.conn.execute("DELETE FROM vec_float WHERE rowid = ?", (rows_b[-1][0],))
        index.conn.execute("DELETE FROM chunks WHERE row_id = ?", (rows_b[-1][0],))
        index.conn.execute("UPDATE chunks SET text = 'stale' WHERE doc_id = 'a' AND seq = 1")
        index.conn.execute("INSERT INTO chunks (doc_id, seq, text) VALUES ('ghost', 0, 'boo')")
        index.conn.commit()
        index.conn.execute("PRAGMA foreign_keys=ON")
        health = index.health()
        assert health["ok"] is False and health["orphan_passages"] == 1
        report = index.integrity_check()
        text = " ".join(report["problems"])
        assert "out-of-date search data" in text and "search passage" in text
        repaired = index.repair()
        assert repaired["ok"] is True and repaired["vectors_missing"] == 0
        assert repaired["chunks_fixed"] == 1 + 2  # orphan + two re-split notes
        assert [t for _r, _s, t in chunk_rows(index, "a")] == split_passages(
            filler(12), normalize_chunking(SMALL)
        )
        assert index.integrity_check()["ok"] is True


def test_document_without_passages_is_unhealthy(tmp_path):
    with small_index(tmp_path) as index:
        index.add_documents([Document("a", filler(12), "A")])
        index.conn.execute("DELETE FROM chunks WHERE doc_id = 'a'")
        index.conn.commit()
        health = index.health()
        assert health["ok"] is False and health["documents_without_passages"] == 1
        assert health["chunks"] == 0 != health["documents"]
        assert index.repair()["ok"] is True


def test_clear_keeps_the_setting(tmp_path):
    with small_index(tmp_path, embedder=AxisEmbedder()) as index:
        index.add_documents([Document("a", filler(12), "A")])
        index.clear()
        assert index.chunking_config() == normalize_chunking(SMALL)
        assert index.vectors_need_rebuild()["needed"] is False


# ---- 4. search ------------------------------------------------------------------------------


@pytest.fixture()
def corpus(tmp_path):
    embedder = AxisEmbedder()
    index = small_index(tmp_path, "search.db", embedder=embedder)
    deep = filler(10) + " The probe entered a stable orbit around the planet at last."
    index.add_documents(
        [
            Document("deep.md", deep, "Mission log", folder="logs", ext=".md"),
            Document("many.md", " ".join(
                f"Orbit fact {i}: the orbit period changes with altitude." for i in range(8)
            ), "Orbit facts", folder="space", ext=".md"),
            Document("bread.md", "Bread needs flour, water and salt.", "Bread", folder="food",
                     ext=".md"),
            Document("compost.md", "Compost turns kitchen scraps into soil.", "Compost",
                     folder="garden", ext=".txt"),
            Document("short-orbit.md", "An orbit is a curved path.", "Orbit basics",
                     folder="space", ext=".md"),
        ]
    )
    yield index
    index.close()


def ids(hits):
    return [hit.doc_id for hit in hits]


def best_seq(index, doc_id: str, query: str) -> tuple[int, float]:
    """(seq, cosine) of the note's best passage under the fake model (first on ties)."""
    title = index.get_document(doc_id)["title"]
    vector = index.embedder.encode_query(query)
    sims = [
        float(index.embedder._vector(title + "\n" + text) @ vector)
        for _row, _seq, text in chunk_rows(index, doc_id)
    ]
    return int(np.argmax(sims)), max(sims)


def test_semantic_hits_are_distinct_notes_ranked_by_best_passage(corpus):
    hits = semantic_search(corpus, "orbit", k=5)
    assert len(ids(hits)) == len(set(ids(hits))) == 5
    deep = next(hit for hit in hits if hit.doc_id == "deep.md")
    count = len(chunk_rows(corpus, "deep.md"))
    assert count > 1 and deep.passage_count == count
    seq, cosine = best_seq(corpus, "deep.md", "orbit")
    assert deep.passage_seq == seq >= count - 2 and "stable orbit" in deep.passage
    short = next(hit for hit in hits if hit.doc_id == "short-orbit.md")
    assert short.passage_count == 1 and short.passage is None and short.passage_seq == 0
    # The note's score is its best passage's cosine.
    assert deep.score == pytest.approx(cosine, abs=1e-5)


def test_knn_expands_until_k_distinct_notes(corpus):
    # many.md has many orbit passages; k notes must still be distinct.
    for k in (1, 2, 3, 4, 5, 10):
        hits = semantic_search(corpus, "orbit", k=k)
        assert len(hits) == min(k, 5) and len(set(ids(hits))) == len(hits)


@pytest.mark.parametrize("precision", ["float", "int8", "binary"])
def test_precisions_and_filters_aggregate_the_same_way(corpus, precision):
    plain = semantic_search(corpus, "orbit", k=3, precision=precision)
    assert len(set(ids(plain))) == 3
    filtered = semantic_search(corpus, "orbit", k=5, precision=precision,
                               filters={"folders": ["space", "logs"]})
    assert set(ids(filtered)) == {"deep.md", "many.md", "short-orbit.md"}
    deep = next(hit for hit in filtered if hit.doc_id == "deep.md")
    assert deep.passage_seq >= 1 and "stable orbit" in deep.passage
    if precision != "binary":
        float_hits = semantic_search(corpus, "orbit", k=5, filters={"folders": ["space", "logs"]})
        assert ids(filtered) == ids(float_hits)


def test_block_scan_matches_knn_for_float(corpus, monkeypatch):
    import onefind.search as search_module

    knn = semantic_search(corpus, "orbit compost", k=5)
    exact = search_module._attach_passages(
        corpus,
        search_module._application_side_search(
            corpus, corpus.embedder.encode_query("orbit compost"), 5, "float",
            blocks=search_module._vector_blocks(corpus, None, block_size=1),
        ),
    )
    assert ids(knn) == ids(exact)
    for left, right in zip(knn, exact):
        assert left.score == pytest.approx(right.score, abs=1e-5)
        assert left.passage_seq == right.passage_seq


def test_knn_limit_falls_back_to_exact_scan(corpus, monkeypatch):
    import onefind.search as search_module

    monkeypatch.setattr(search_module, "_KNN_LIMIT", 2)
    hits = semantic_search(corpus, "orbit", k=5)
    assert len(set(ids(hits))) == 5


def test_rerank_uses_best_passage(corpus):
    hits = search(corpus, "orbit", mode="hybrid", k=3, rerank=True, candidate_depth=5)
    deep = [hit for hit in hits if hit.doc_id == "deep.md"]
    vector = corpus.embedder.encode_query("orbit")
    for hit in deep:
        passages = [t for _r, _s, t in chunk_rows(corpus, "deep.md")]
        sims = [float(corpus.embedder._vector(f"Mission log\n{p}") @ vector) for p in passages]
        assert hit.score == pytest.approx(max(sims), abs=1e-5)
        assert hit.passage_seq == int(np.argmax(sims))


def test_search_is_deterministic(corpus):
    first = [(h.doc_id, h.score, h.passage_seq) for h in search(corpus, "orbit", mode="semantic", k=5)]
    for _ in range(3):
        again = [(h.doc_id, h.score, h.passage_seq)
                 for h in search(corpus, "orbit", mode="semantic", k=5)]
        assert again == first
    scores = [score for _id, score, _seq in first]
    assert scores == sorted(scores, reverse=True)
    for (a, sa, _), (b, sb, _) in zip(first, first[1:]):
        assert sa > sb or a < b  # ties broken by doc_id


def test_explain_reports_the_deep_passage(corpus):
    outcome = search_detailed(corpus, "stable orbit planet", mode="semantic", k=5, explain=True)
    deep = next(hit for hit in outcome.hits if hit.doc_id == "deep.md")
    assert deep.explain["best_passage_seq"] == best_seq(corpus, "deep.md", "stable orbit planet")[0]
    assert deep.explain["best_passage_seq"] > 0
    assert deep.explain["passages"] == deep.passage_count
    assert any("further down the note" in reason for reason in deep.explain["reasons"])
    short = next(hit for hit in outcome.hits if hit.doc_id == "bread.md")
    assert short.explain["best_passage_seq"] == 0
    assert not any("further down" in reason for reason in short.explain["reasons"])
    lexical = search_detailed(corpus, "planet", mode="lexical", k=5, explain=True)
    assert lexical.hits[0].explain["best_passage_seq"] is None


def test_hybrid_hits_carry_the_passage_and_snippets_use_it(corpus):
    outcome = search_detailed(corpus, "stable orbit", mode="hybrid", k=5,
                              lexical_match="auto", explain=True)
    deep = next(hit for hit in outcome.hits if hit.doc_id == "deep.md")
    assert deep.passage and "stable orbit" in deep.passage
    body = corpus.get_document("deep.md")["body"]
    query = "final destination of the spacecraft"  # no keyword shared with the note
    meaning_only = Hit("deep.md", "Mission log", 1.0, "", passage=deep.passage)
    segments = build_snippet(meaning_only, body, query)
    text = "".join(segment["text"] for segment in segments)
    assert "stable orbit" in text and text.startswith(ELLIPSIS)
    plain = build_snippet(Hit("deep.md", "Mission log", 1.0, ""), body, query)
    assert "stable orbit" not in "".join(segment["text"] for segment in plain)


def test_build_snippet_passage_argument_and_keyword_precedence():
    body = "Intro about orbit mechanics. " + filler(30) + " The answer lives here at the end."
    hint = "The answer lives here at the end."
    segments = build_snippet({"snippet": "", "title": "T"}, body, "zzz", passage=hint)
    text = "".join(segment["text"] for segment in segments)
    assert text == f"{ELLIPSIS} {hint}"
    # A keyword in the matched passage is highlighted there.
    segments = build_snippet({"snippet": "", "title": "T"}, body, "answer", passage=hint)
    assert any(s["highlight"] and s["text"].lower() == "answer" for s in segments)
    # A keyword only elsewhere in the body still wins over an unhighlighted hint.
    segments = build_snippet({"snippet": "", "title": "T"}, body, "mechanics", passage=hint)
    assert any(s["highlight"] for s in segments)


# ---- 5. setting changes & rebuild ---------------------------------------------------------


def legacy_index(tmp_path, embedder, docs, name="legacy.db") -> Index:
    """A collection built whole-note (chunking off), like one made before v1.2.1."""
    index = Index.open(tmp_path / name)
    index.set_chunking(None)
    if embedder is not None:
        index.attach_embedder(embedder)
    index.add_documents(docs)
    return index


LONG_DOCS = [
    Document(f"long{i}.md", filler(12, start=100 * i) + f" Finally the glacier note {i}.", f"L{i}")
    for i in range(3)
] + [Document("short.md", "tiny compost note", "S")]


def test_turning_chunking_on_marks_a_rebuild_and_rebuild_applies_it(tmp_path):
    embedder = AxisEmbedder()
    with legacy_index(tmp_path, embedder, LONG_DOCS) as index:
        status = index.vectors_need_rebuild()
        assert status["needed"] is False and status["recommended"] is False  # 86 < 200 words
        index.set_chunking(SMALL)
        status = index.vectors_need_rebuild()
        assert status["needed"] is True and status["documents"] == 3
        assert "single whole-note vector" in status["reason"]
        assert index.stats()["vectors_need_rebuild"] == status
        assert index.integrity_check()["ok"] is True  # pending rebuild is not damage
        events = []
        result = index.rebuild_vectors(progress=events.append)
        assert result["rebuilt"] == 3 and result["complete"] is True
        assert result["vectors_embedded"] == result["passages"] > 3
        assert events[0]["phase"] == "scan" and events[-1]["phase"] == "done"
        assert index.vectors_need_rebuild()["needed"] is False
        assert index.get_meta("chunking_built") == chunking_to_meta(normalize_chunking(SMALL))
        for doc in LONG_DOCS:
            assert [t for _r, _s, t in chunk_rows(index, doc.doc_id)] == split_passages(
                doc.body, normalize_chunking(SMALL)
            )
        assert index.health()["ok"] is True
        hits = semantic_search(index, "glacier", k=1)
        assert hits[0].passage_seq == hits[0].passage_count - 1
        # Nothing left to do: a second run is a no-op.
        assert index.rebuild_vectors()["rebuilt"] == 0


def test_rebuild_cancelled_midway_leaves_every_note_old_or_new(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "REBUILD_BATCH_DOCS", 1)
    embedder = AxisEmbedder()
    with legacy_index(tmp_path, embedder, LONG_DOCS) as index:
        index.set_chunking(SMALL)
        calls = {"n": 0}

        def cancel():
            calls["n"] += 1
            return calls["n"] > 2  # finish two notes, then stop

        with pytest.raises(OperationCancelled):
            index.rebuild_vectors(cancel=cancel)
        new = old = 0
        for doc in LONG_DOCS[:3]:
            texts = [t for _r, _s, t in chunk_rows(index, doc.doc_id)]
            if texts == [doc.body]:
                old += 1
            else:
                assert texts == split_passages(doc.body, normalize_chunking(SMALL))
                new += 1
        assert (new, old) == (2, 1)
        health = index.health()
        assert health["ok"] is True and health["missing_vectors"] == 0
        status = index.vectors_need_rebuild()
        assert status["needed"] is True and status["documents"] == 1
        assert index.rebuild_vectors()["rebuilt"] == 1
        assert index.vectors_need_rebuild()["needed"] is False


def test_rebuild_needs_the_model_when_vectors_exist(tmp_path):
    with legacy_index(tmp_path, AxisEmbedder(), LONG_DOCS):
        pass
    with Index.open(tmp_path / "legacy.db") as index:
        index.set_chunking(SMALL)
        with pytest.raises(UsageError, match="embedding model"):
            index.rebuild_vectors()


def test_keyword_only_collection_rebuilds_without_a_model(tmp_path):
    with legacy_index(tmp_path, None, LONG_DOCS) as index:
        index.set_chunking(SMALL)
        assert index.vectors_need_rebuild()["needed"] is True
        result = index.rebuild_vectors()
        assert result["rebuilt"] == 3 and result["vectors_embedded"] == 0
        assert index.health()["ok"] is True
        # Attaching a model later embeds every passage.
        index.attach_embedder(AxisEmbedder())
        assert index.health()["ok"] is True
        assert index.health()["passage_vectors"] == index.count("chunks")


def test_turning_chunking_off_returns_to_whole_notes(tmp_path):
    with small_index(tmp_path, embedder=AxisEmbedder()) as index:
        index.add_documents(LONG_DOCS)
        index.set_chunking(None)
        status = index.vectors_need_rebuild()
        assert status["needed"] is True and "turned off" in status["reason"]
        index.rebuild_vectors()
        for doc in LONG_DOCS:
            assert [t for _r, _s, t in chunk_rows(index, doc.doc_id)] == [doc.body]
        assert index.health()["ok"] is True and not index.vectors_need_rebuild()["needed"]


def test_set_chunking_without_affected_notes_needs_no_rebuild(tmp_path):
    with legacy_index(tmp_path, AxisEmbedder(), [Document("s", "short note", "S")]) as index:
        index.set_chunking(SMALL)
        assert index.vectors_need_rebuild()["needed"] is False
        assert index.get_meta("chunking_built") == index.get_meta("chunking")
        assert index.set_chunking(SMALL) == normalize_chunking(SMALL)  # no-op
        with pytest.raises(UsageError):
            index.set_chunking({"max_words": 3})


def test_changing_settings_twice_with_writes_in_between_is_tracked(tmp_path):
    embedder = AxisEmbedder()
    with small_index(tmp_path, embedder=embedder) as index:
        index.add_documents(LONG_DOCS[:1])
        index.set_chunking({"max_words": 30, "overlap_words": 5})
        index.add_documents(LONG_DOCS[1:2])  # written with 30/5
        index.set_chunking(SMALL)  # back to the first setting
        assert index.get_meta("chunking_built") == "mixed"
        status = index.vectors_need_rebuild()
        assert status["needed"] is True and status["documents"] == 1
        index.rebuild_vectors()
        assert index.vectors_need_rebuild()["needed"] is False
        assert index.integrity_check()["ok"] is True


def test_recommendation_for_whole_note_collections_with_long_notes(tmp_path):
    long_doc = Document("big.md", filler(40), "Big")  # 280 words > 200
    with legacy_index(tmp_path, AxisEmbedder(), [long_doc]) as index:
        status = index.vectors_need_rebuild()
        assert status == {
            "needed": False, "documents": 0, "reason": status["reason"], "chunking": None,
            "long_notes": 1, "recommended": True,
        }
        assert "Turn on passage search" in status["reason"]
        index.set_meta("eval_manifest", "{}")
        assert index.vectors_need_rebuild()["recommended"] is False


def test_stats_census_is_cached_until_something_changes(tmp_path, monkeypatch):
    with small_index(tmp_path, embedder=AxisEmbedder()) as index:
        index.add_documents(LONG_DOCS)
        index.stats()
        calls = []
        original = index._rechunk_candidates
        monkeypatch.setattr(index, "_rechunk_candidates",
                            lambda configs: calls.append(1) or original(configs))
        index.set_chunking(None)
        calls.clear()
        index.stats()
        index.stats()
        assert len(calls) == 1
        index.record_search("q", "lexical", None, None, 0)
        index.stats()
        assert len(calls) == 2


# ---- 6. jobs ------------------------------------------------------------------------------


class GatedEmbedder(AxisEmbedder):
    name = "gated-fake"

    def __init__(self) -> None:
        super().__init__()
        self.gate = threading.Event()
        self.entered = threading.Event()
        self.gated = False

    def encode_documents(self, texts):
        if self.gated:
            self.entered.set()
            self.gate.wait(10)
        return super().encode_documents(texts)


def jobs_for(embedder):
    from onefind.jobs import EmbedderCache, JobManager

    return JobManager(EmbedderCache(factory=lambda _name: embedder))


def test_rebuild_job_turns_chunking_on_and_succeeds(tmp_path):
    embedder = AxisEmbedder()
    with legacy_index(tmp_path, embedder, LONG_DOCS):
        pass
    manager = jobs_for(embedder)
    job = manager.start_rebuild_vectors(tmp_path / "legacy.db", chunking=SMALL, label="Rebuild")
    assert job["kind"] == "rebuild_vectors" and job["status"] in {"queued", "running"}
    assert [step["name"] for step in job["steps"]] == ["scan", "index", "vectors", "ready"]
    done = manager.wait(job["id"], timeout=30)
    assert done["status"] == "succeeded", done["error"]
    assert {step["status"] for step in done["steps"]} == {"done"}
    assert done["result"]["rebuilt"] == 3 and done["health"]["ok"] is True
    assert done["counts"]["found"] == 3 and done["counts"]["embedded"] == 3
    assert done["embed"] is True and done["model"] == embedder.name
    assert done["options"]["chunking"] == normalize_chunking(SMALL)
    with Index.open(tmp_path / "legacy.db") as index:
        assert index.vectors_need_rebuild()["needed"] is False
    # Keeping the setting on an up-to-date collection is a quick no-op.
    again = manager.wait(manager.start_rebuild_vectors(tmp_path / "legacy.db")["id"], timeout=30)
    assert again["status"] == "succeeded" and again["result"]["rebuilt"] == 0
    assert again["options"]["chunking"] == "keep"


def test_rebuild_job_on_keyword_only_collection_skips_vectors(tmp_path):
    with legacy_index(tmp_path, None, LONG_DOCS):
        pass
    manager = jobs_for(AxisEmbedder())
    done = manager.wait(
        manager.start_rebuild_vectors(tmp_path / "legacy.db", chunking=SMALL)["id"], timeout=30
    )
    assert done["status"] == "succeeded"
    steps = {step["name"]: step["status"] for step in done["steps"]}
    assert steps["vectors"] == "skipped" and done["embed"] is False


def test_rebuild_job_cancel_busy_and_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "REBUILD_BATCH_DOCS", 1)
    embedder = GatedEmbedder()
    with legacy_index(tmp_path, embedder, LONG_DOCS):
        pass
    manager = jobs_for(embedder)
    with pytest.raises(UsageError):
        manager.start_rebuild_vectors(tmp_path / "legacy.db", chunking={"max_words": 2})
    embedder.gated = True
    job = manager.start_rebuild_vectors(tmp_path / "legacy.db", chunking=SMALL)
    assert embedder.entered.wait(10)
    with pytest.raises(UsageError, match="already running"):
        manager.start_rebuild_vectors(tmp_path / "legacy.db")
    with pytest.raises(UsageError, match="already running"):
        manager.start_index(tmp_path / "legacy.db", str(tmp_path))
    manager.cancel(job["id"])
    embedder.gate.set()
    done = manager.wait(job["id"], timeout=30)
    assert done["status"] == "cancelled" and done["error"] is None
    assert "run it again" in done["message"]
    with Index.open(tmp_path / "legacy.db") as index:
        assert index.health()["ok"] is True
        status = index.vectors_need_rebuild()
        assert status["needed"] is True and 1 <= status["documents"] <= 2
    missing = manager.wait(manager.start_rebuild_vectors(tmp_path / "nope.db")["id"], timeout=30)
    assert missing["status"] == "failed" and missing["error"]["code"] == "collection_unreadable"


# ---- 7. evaluation stays whole-document ------------------------------------------------------


def test_evaluation_forces_chunking_off(tmp_path, monkeypatch):
    from onefind import evaluate

    fake_ranx = types.ModuleType("ranx")
    fake_ranx.Qrels = types.SimpleNamespace(from_dict=lambda data: data)
    fake_ranx.Run = types.SimpleNamespace(from_dict=lambda data: data)
    fake_ranx.evaluate = lambda _q, _r, metrics: {metric: 0.5 for metric in metrics}
    monkeypatch.setitem(sys.modules, "ranx", fake_ranx)
    monkeypatch.setattr(evaluate, "SentenceEmbedder", lambda _name: HashEmbedder())

    folder = tmp_path / "beir"
    folder.mkdir()
    long_text = filler(60)  # 420 words: would be split by a product collection
    (folder / "corpus.jsonl").write_text(
        "\n".join(json.dumps(row) for row in [
            {"_id": "d1", "title": "Long", "text": long_text},
            {"_id": "d2", "title": "Short", "text": "orbit"},
        ]),
        encoding="utf-8",
    )
    (folder / "queries.jsonl").write_text('{"_id": "q1", "text": "orbit"}\n', encoding="utf-8")
    (folder / "qrels.tsv").write_text("query-id\tcorpus-id\tscore\nq1\td2\t1\n", encoding="utf-8")
    db = tmp_path / "eval.db"
    evaluate.run_eval(str(folder), db=db, k=2, out_dir=tmp_path / "reports")
    with Index.open(db) as index:
        assert index.chunking_config() is None
        assert index.count("chunks") == index.count("documents") == index.count("vectors") == 2
        assert [t for _r, _s, t in chunk_rows(index, "d1")] == [long_text]
        assert json.loads(index.get_meta("eval_manifest"))["chunking"] is None
        assert index.vectors_need_rebuild()["recommended"] is False

    # An evaluation index whose documents were split is refused by the sweep.
    with Index.open(db) as index:
        index.set_chunking(SMALL)
        index.attach_embedder(HashEmbedder())
        index.rebuild_vectors()
    with pytest.raises(DataError, match="passages"):
        evaluate.run_alpha_sweep(str(folder), db=db, alphas=[0.5], out_dir=tmp_path / "s")


# ---- 8. real model: the answer at the end of a long note ---------------------------------------


LONG_NOTE_ID = "agriculture/farm-journal.md"
ANSWER = (
    "\n\n## Greenhouse access\n\nThe spare key for the greenhouse padlock is hidden inside "
    "the hollow ceramic gnome beside the rain barrel. Put it back after locking up.\n"
)


@pytest.fixture(scope="module")
def long_note_dbs(tmp_path_factory):
    pytest.importorskip("sentence_transformers")
    from onefind.embed import DEFAULT_MODEL, SentenceEmbedder
    from onefind.ingest import ingest_path

    root = tmp_path_factory.mktemp("long-note")
    notes = root / "notes"
    shutil.copytree("sample-data", notes)
    (notes / "queries.jsonl").unlink()
    source = (notes / "agriculture" / "crop-rotation.md").read_text(encoding="utf-8")
    body = source.partition("\n")[2].strip()
    long_body = "\n\n".join([body] * 8)  # ~720 words about crop rotation ...
    (notes / LONG_NOTE_ID).write_text(
        "# Farm journal\n\n" + long_body + ANSWER, encoding="utf-8"  # ... then the answer
    )
    embedder = SentenceEmbedder(DEFAULT_MODEL)
    paths = {}
    for setting in ("default", None):
        path = root / f"{setting or 'off'}.db"
        with Index.open(path) as index:
            index.set_chunking(setting)
            index.attach_embedder(embedder)
            ingest_path(index, notes)
        paths[setting or "off"] = path
    return paths, embedder


def _rank(outcome, doc_id):
    found = [hit.doc_id for hit in outcome.hits]
    return found.index(doc_id) + 1 if doc_id in found else None


def test_model_finds_an_answer_at_the_end_of_a_long_note_only_with_chunking(long_note_dbs):
    paths, embedder = long_note_dbs
    query = "where is the spare greenhouse key hidden"
    results = {}
    for name, path in paths.items():
        with Index.open(path) as index:
            index.attach_embedder(embedder)
            outcome = search_detailed(index, query, mode="semantic", k=10, explain=True)
            rank = _rank(outcome, LONG_NOTE_ID)
            hit = outcome.hits[rank - 1] if rank else None
            results[name] = (rank, hit, outcome, index.get_document(LONG_NOTE_ID)["body"])
    on_rank, on_hit, on_outcome, body = results["default"]
    off_rank, off_hit, off_outcome, _body = results["off"]
    assert on_rank == 1 and on_outcome.confidences[0] != "weak"
    assert on_hit.passage_count > 3 and on_hit.passage_seq == on_hit.passage_count - 1
    assert any("further down the note" in reason for reason in on_hit.explain["reasons"])
    snippet = "".join(segment["text"] for segment in build_snippet(on_hit, body, query))
    assert "ceramic gnome" in snippet
    # Whole-note vector: the model never read the end of the note.
    assert off_rank is None or off_rank > 1
    assert off_hit is None or off_outcome.confidences[off_rank - 1] == "weak"
    assert on_hit.score - (off_hit.score if off_hit else 0.0) > 0.15


def test_model_off_topic_queries_still_abstain_with_passages(long_note_dbs):
    from test_search_quality import BINARY_OFF_TOPIC, OFF_TOPIC

    paths, embedder = long_note_dbs
    with Index.open(paths["default"]) as index:
        index.attach_embedder(embedder)
        for query in OFF_TOPIC + BINARY_OFF_TOPIC:
            for mode, precision in (("semantic", "float"), ("hybrid", "float"),
                                    ("semantic", "binary")):
                outcome = search_detailed(index, query, mode=mode, k=5, precision=precision,
                                          lexical_match="auto")
                assert outcome.abstained, (mode, precision, query)
