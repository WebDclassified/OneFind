"""v1.2 ingestion: excludes, front matter, incremental runs, prune, progress."""

import hashlib
import os
import sqlite3

import numpy as np
import pytest

import onefind.ingest as ingest_module
from onefind.errors import CorpusNotFoundError, DataError, OperationCancelled, UsageError
from onefind.ingest import (
    DEFAULT_EXCLUDES,
    MAX_FILE_BYTES,
    SUPPORTED_SUFFIXES,
    ingest_path,
    is_excluded,
    load_documents,
    normalize_excludes,
    parse_front_matter,
    scan_changes,
)
from onefind.search import lexical_search
from onefind.store import BATCH_SIZE, Index


class FakeEmbedder:
    name = "fake-mini"
    revision = "test-revision-1"
    dimension = 3

    def encode_documents(self, texts):
        return np.stack([self._vector(text) for text in texts])

    def encode_query(self, text):
        return self._vector(text)

    @staticmethod
    def _vector(text):
        digest = hashlib.sha256(text.encode("utf-8")).digest()[:3]
        vector = np.frombuffer(digest, dtype=np.uint8).astype(np.float32) + 1.0
        return vector / np.linalg.norm(vector)


def write(path, text="", *, data=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if data is not None:
        path.write_bytes(data)
    else:
        path.write_text(text, encoding="utf-8")
    return path


def doc_ids(index):
    return {row[0] for row in index.conn.execute("SELECT doc_id FROM documents")}


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "ingest.db"


@pytest.fixture()
def notes(tmp_path):
    root = tmp_path / "notes"
    write(root / "today.md", "# Today\nstandup about the rollback plan")
    write(root / "projects" / "alpha.txt", "alpha project kickoff notes")
    write(root / "projects" / "deep" / "beta.md", "# Beta\nbeta launch checklist")
    return root


# -- discovery & excludes ------------------------------------------------------------


def test_supported_suffixes_and_defaults():
    assert SUPPORTED_SUFFIXES == {".txt", ".md", ".markdown", ".rst", ".org", ".text"}
    assert DEFAULT_EXCLUDES == [".*", "node_modules", "__pycache__", ".venv", "venv"]
    assert MAX_FILE_BYTES == 5 * 1024 * 1024


def test_all_supported_suffixes_are_indexed(tmp_path, db):
    root = tmp_path / "kinds"
    for suffix in [".txt", ".md", ".markdown", ".rst", ".org", ".text"]:
        write(root / f"file{suffix}", f"content for {suffix}")
    write(root / "upper.MD", "upper-case suffix")
    write(root / "ignored.pdf", "not text we read")
    write(root / "data.jsonl", '{"_id": "x", "text": "skip"}')
    with Index.open(db) as index:
        result = ingest_path(index, root)
        assert result["added"] == 7
        assert {d["ext"] for d in index.facets()["types"]} == {
            ".txt", ".md", ".markdown", ".rst", ".org", ".text"
        }


def test_default_excludes_skip_noise(tmp_path, db):
    root = tmp_path / "proj"
    write(root / "keep.md", "keep me")
    for noise in [
        ".hidden.md",
        ".git/notes.md",
        "node_modules/pkg/readme.md",
        "__pycache__/x.txt",
        ".venv/lib/readme.md",
        "venv/readme.md",
        "sub/.obsidian/workspace.md",
    ]:
        write(root / noise, "noise")
    with Index.open(db) as index:
        ingest_path(index, root)
        assert doc_ids(index) == {"keep.md"}


@pytest.mark.parametrize(
    "patterns,expected",
    [
        (["drafts"], {"keep.md", "archive/old.md", "notes/x.log.md"}),
        (["archive/*"], {"keep.md", "drafts/d.md", "notes/drafts/e.md", "notes/x.log.md"}),
        (["*.log.md"], {"keep.md", "drafts/d.md", "notes/drafts/e.md", "archive/old.md"}),
        ([".\\ARCHIVE\\"], {"keep.md", "drafts/d.md", "notes/drafts/e.md", "notes/x.log.md"}),
        (["notes/drafts"], {"keep.md", "drafts/d.md", "archive/old.md", "notes/x.log.md"}),
    ],
)
def test_user_excludes(tmp_path, db, patterns, expected):
    root = tmp_path / "corpus"
    for name in ["keep.md", "drafts/d.md", "notes/drafts/e.md", "archive/old.md", "notes/x.log.md"]:
        write(root / name, f"text of {name}")
    with Index.open(db) as index:
        ingest_path(index, root, exclude=patterns)
        assert doc_ids(index) == expected


def test_exclude_helpers():
    assert normalize_excludes(None) == []
    assert normalize_excludes("drafts") == ["drafts"]
    assert normalize_excludes([" ./a/b/ ", "a/b", "", "c\\d"]) == ["a/b", "c/d"]
    with pytest.raises(UsageError):
        normalize_excludes([3])
    assert is_excluded("a/b/c.md", ["b"])
    assert is_excluded("a/b/c.md", ["a/b"])
    assert is_excluded("A/B/C.MD", ["*.md"])
    assert not is_excluded("a/b/c.md", ["c"])
    assert not is_excluded("", ["*"])


def test_excludes_are_remembered_per_root(tmp_path, db):
    root = tmp_path / "corpus"
    write(root / "keep.md", "keep")
    write(root / "drafts" / "d.md", "draft")
    with Index.open(db) as index:
        ingest_path(index, root, exclude=["drafts"])
        assert index.roots()[0]["exclude"] == ["drafts"]
        ingest_path(index, root, prune=True)  # None reuses stored patterns
        assert doc_ids(index) == {"keep.md"}
        ingest_path(index, root, exclude=[])  # explicit empty list clears them
        assert doc_ids(index) == {"keep.md", "drafts/d.md"}
        assert index.roots()[0]["exclude"] == []


def test_invalid_exclude_type_is_usage_error(notes, db):
    with Index.open(db) as index, pytest.raises(UsageError):
        ingest_path(index, notes, exclude=[42])


# -- front matter & metadata -------------------------------------------------------------


@pytest.mark.parametrize(
    "text,title,tags,body",
    [
        ("---\ntitle: Weekly Sync\ntags: [Work, 'Meetings']\n---\nbody text", "Weekly Sync",
         ["work", "meetings"], "body text"),
        ("---\ntags:\n  - alpha\n  - \"#Beta\"\n---\n# Heading Title\nrest", "Heading Title",
         ["alpha", "beta"], "rest"),
        ("---\ntitle: \"Quoted\"\ntags: one, two , ONE\n---\n\ntext", "Quoted",
         ["one", "two"], "text"),
        ("---\nauthor: me\n---\nplain body", "note", [], "plain body"),
        ("---\n---\njust body", "note", [], "just body"),
    ],
)
def test_front_matter_is_parsed_and_stripped(tmp_path, db, text, title, tags, body):
    root = tmp_path / "fm"
    write(root / "note.md", text)
    with Index.open(db) as index:
        ingest_path(index, root)
        doc = index.get_document("note.md")
    assert doc["title"] == title
    assert doc["tags"] == tags
    assert doc["body"] == body
    assert "---" not in doc["body"]


@pytest.mark.parametrize(
    "text",
    [
        "---\nThis is a thematic break, not front matter.\n---\nmore",
        "---\ntitle: never closed\nbody continues",
        "intro\n---\ntitle: late\n---\n",
    ],
)
def test_non_front_matter_is_left_alone(text):
    meta, body = parse_front_matter(text)
    assert meta == {}
    assert body == text


def test_file_metadata_is_recorded(notes, db):
    with Index.open(db) as index:
        result = ingest_path(index, notes)
        root_id = result["root_id"]
        doc = index.get_document("projects/deep/beta.md")
        raw = (notes / "projects" / "deep" / "beta.md").read_bytes()
        stat = (notes / "projects" / "deep" / "beta.md").stat()
        assert doc["content_hash"] == hashlib.sha256(raw).hexdigest()
        assert doc["size_bytes"] == stat.st_size
        assert doc["mtime"] == pytest.approx(stat.st_mtime)
        assert doc["ext"] == ".md"
        assert doc["folder"] == "projects/deep"
        assert doc["root_id"] == root_id
        assert doc["source"] == "projects/deep/beta.md"
        assert index.get_document("today.md")["folder"] == ""
        assert result["root"] == str(notes.resolve())
        assert index.roots()[0]["path"] == str(notes.resolve())


def test_encodings_and_line_endings(tmp_path, db):
    root = tmp_path / "enc"
    write(root / "bom.md", data="﻿# Bom Title\r\nline one\r\nline two".encode("utf-8"))
    write(root / "utf16.txt", data="utf sixteen text".encode("utf-16"))
    write(root / "latin.txt", data="caf\xe9 au lait".encode("latin-1"))
    with Index.open(db) as index:
        result = ingest_path(index, root)
        assert result["skipped"] == []
        bom = index.get_document("bom.md")
        assert bom["title"] == "Bom Title" and bom["body"] == "line one\nline two"
        assert index.get_document("utf16.txt")["body"] == "utf sixteen text"
        # Non-UTF-8 text is read as Windows "ANSI" (cp1252), never as U+FFFD.
        assert index.get_document("latin.txt")["body"] == "caf\xe9 au lait"


def test_skips_are_reported_not_errors(tmp_path, db, monkeypatch):
    root = tmp_path / "skips"
    write(root / "good.md", "fine")
    write(root / "empty.md", "")
    write(root / "blank.txt", "   \n\n")
    write(root / "only-front.md", "---\ntitle: Nothing else\n---\n")
    write(root / "binary.txt", data=b"abc\x00def")
    write(root / "big.md", "x" * 200)
    monkeypatch.setattr(ingest_module, "MAX_FILE_BYTES", 100)
    with Index.open(db) as index:
        result = ingest_path(index, root)
        assert doc_ids(index) == {"good.md"}
    assert sorted((item["path"], item["reason"]) for item in result["skipped"]) == [
        ("big.md", "too large"),
        ("binary.txt", "binary"),
        ("blank.txt", "empty"),
        ("empty.md", "empty"),
        ("only-front.md", "empty"),
    ]
    assert result["files_indexed"] == 1


def test_heading_only_note_is_still_indexed(tmp_path, db):
    root = tmp_path / "heading"
    write(root / "h.md", "# Just A Title\n")
    with Index.open(db) as index:
        ingest_path(index, root)
        doc = index.get_document("h.md")
    assert doc["title"] == "Just A Title" and doc["body"] == ""


def test_folder_with_only_skipped_files_fails_cleanly(tmp_path, db):
    root = tmp_path / "nothing"
    write(root / "empty.md", "")
    with Index.open(db) as index:
        with pytest.raises(CorpusNotFoundError, match="skipped"):
            ingest_path(index, root)
        assert index.roots() == []
        assert index.last_run()["status"] == "failed"


# -- incremental & prune -------------------------------------------------------------------


def test_incremental_reindex_counts(notes, db):
    with Index.open(db) as index:
        first = ingest_path(index, notes)
        assert (first["added"], first["updated"], first["unchanged"]) == (3, 0, 0)
        assert first["files_indexed"] == 3

        second = ingest_path(index, notes)
        assert (second["added"], second["updated"], second["unchanged"]) == (0, 0, 3)
        assert second["files_indexed"] == 3

        write(notes / "today.md", "# Today\nrollback plan changed")
        write(notes / "new.md", "brand new note")
        third = ingest_path(index, notes)
        assert (third["added"], third["updated"], third["unchanged"]) == (1, 1, 2)
        assert [h.doc_id for h in lexical_search(index.conn, "changed")] == ["today.md"]

        full = ingest_path(index, notes, incremental=False)
        assert (full["added"], full["updated"], full["unchanged"]) == (0, 4, 0)
        assert index.health()["ok"] is True


def test_touched_file_updates_metadata_only(notes, db):
    target = notes / "today.md"
    with Index.open(db) as index:
        ingest_path(index, notes)
        before = index.get_document("today.md")
        os.utime(target, (before["mtime"] + 100, before["mtime"] + 100))
        result = ingest_path(index, notes)
        after = index.get_document("today.md")
    assert result["unchanged"] == 3 and result["updated"] == 0
    assert after["mtime"] == pytest.approx(before["mtime"] + 100)
    assert after["updated_at"] == before["updated_at"]


def test_prune_removes_deleted_and_newly_excluded(notes, db):
    with Index.open(db) as index:
        ingest_path(index, notes)
        (notes / "today.md").unlink()
        kept = ingest_path(index, notes)  # prune defaults to False
        assert kept["removed"] == 0 and "today.md" in doc_ids(index)

        pruned = ingest_path(index, notes, prune=True)
        assert pruned["removed"] == 1
        assert doc_ids(index) == {"projects/alpha.txt", "projects/deep/beta.md"}

        excluded = ingest_path(index, notes, exclude=["deep"], prune=True)
        assert excluded["removed"] == 1
        assert doc_ids(index) == {"projects/alpha.txt"}
        assert index.health()["ok"] is True
        assert index.count("fts") == 1


def test_prune_only_touches_its_own_root(tmp_path, db):
    first = tmp_path / "one"
    second = tmp_path / "two"
    write(first / "a.md", "first root")
    write(second / "b.md", "second root")
    with Index.open(db) as index:
        ingest_path(index, first)
        ingest_path(index, second)
        (second / "b.md").unlink()
        write(second / "c.md", "replacement")
        result = ingest_path(index, second, prune=True)
        assert result["removed"] == 1
        assert doc_ids(index) == {"a.md", "c.md"}


def test_prune_of_emptied_folder_succeeds(notes, db):
    with Index.open(db) as index:
        ingest_path(index, notes)
        for path in list(notes.rglob("*.*")):
            path.unlink()
        result = ingest_path(index, notes, prune=True)
        assert result["removed"] == 3 and result["files_indexed"] == 0
        assert index.count("documents") == 0
        with pytest.raises(CorpusNotFoundError):
            ingest_path(index, notes)  # nothing to prune any more


def test_prune_removes_vectors_too(notes, db):
    with Index.open(db) as index:
        index.attach_embedder(FakeEmbedder())
        events = []
        ingest_path(index, notes, progress=events.append)
        assert "embed" in {event["phase"] for event in events}
        assert index.count("vectors") == 3
        (notes / "today.md").unlink()
        ingest_path(index, notes, prune=True)
        assert index.count("vectors") == 2
        assert index.health()["ok"] is True


def test_unchanged_rerun_on_embedded_index_needs_no_embedder(notes, db):
    with Index.open(db) as index:
        index.attach_embedder(FakeEmbedder())
        ingest_path(index, notes)
    with Index.open(db) as index:  # no embedder attached this time
        assert ingest_path(index, notes)["unchanged"] == 3
        write(notes / "today.md", "edited")
        with pytest.raises(UsageError, match="stay synchronized"):
            ingest_path(index, notes)
        assert index.last_run()["status"] == "failed"


def test_v2_documents_are_refreshed_by_first_v3_run(notes, db):
    from test_store_v3 import build_v2_database  # noqa: PLC0415

    build_v2_database(db)
    write(notes / "orbit.md", "satellites orbit the earth")  # same id as a v2 row
    with Index.open(db) as index:
        result = ingest_path(index, notes)
        # v2 rows have no content_hash, so a matching id is rewritten once.
        assert (result["added"], result["updated"], result["unchanged"]) == (3, 1, 0)
        assert index.count("documents") == 6
        assert index.get_document("orbit.md")["content_hash"]
        assert index.get_document("water.md")["content_hash"] is None
        assert ingest_path(index, notes)["unchanged"] == 4


# -- duplicates, runs, progress, cancel ----------------------------------------------------


def test_duplicates_are_reported(tmp_path, db):
    root = tmp_path / "dups"
    write(root / "a.md", "same text")
    write(root / "copy" / "a.md", "same text")
    write(root / "b.md", "different")
    with Index.open(db) as index:
        result = ingest_path(index, root)
        assert result["duplicates"] == [["a.md", "copy/a.md"]]
        assert index.duplicates_of("a.md") == ["copy/a.md"]


def test_runs_are_recorded(notes, db):
    with Index.open(db) as index:
        result = ingest_path(index, notes)
        run = index.last_run()
        assert run["id"] == result["run_id"]
        assert run["status"] == "succeeded" and run["added"] == 3
        assert run["root_path"] == str(notes.resolve())
        assert index.stats()["last_indexed_at"] is not None
        assert index.roots()[0]["last_indexed_at"] is not None


def test_failed_jsonl_run_is_recorded(tmp_path, db):
    corpus = tmp_path / "bad.jsonl"
    corpus.write_text("not-json\n", encoding="utf-8")
    with Index.open(db) as index:
        with pytest.raises(DataError, match="invalid JSON"):
            ingest_path(index, corpus)
        run = index.last_run()
        assert run["status"] == "failed" and "invalid JSON" in run["error"]


def test_progress_events(notes, db):
    events = []
    with Index.open(db) as index:
        ingest_path(index, notes, progress=events.append)
    phases = [event["phase"] for event in events]
    assert phases[0] == "scan" and phases[-1] == "done"
    assert {"scan", "read", "write", "done"} <= set(phases)
    assert phases.index("write") > phases.index("read")
    reads = [event for event in events if event["phase"] == "read"]
    assert [(e["done"], e["total"]) for e in reads] == [(1, 3), (2, 3), (3, 3)]
    assert all(set(event) == {"phase", "done", "total", "message"} for event in events)
    assert all(isinstance(event["message"], str) and event["message"] for event in events)
    assert events[-1]["done"] == 3


def test_prune_emits_progress(notes, db):
    events = []
    with Index.open(db) as index:
        ingest_path(index, notes)
        (notes / "today.md").unlink()
        ingest_path(index, notes, prune=True, progress=events.append)
    prune_events = [event for event in events if event["phase"] == "prune"]
    assert prune_events[-1]["done"] == prune_events[-1]["total"] == 1


def test_cancel_keeps_committed_batches(tmp_path, db):
    root = tmp_path / "many"
    for number in range(BATCH_SIZE + 40):
        write(root / f"n{number:04d}.md", f"note number {number}")
    reads = {"count": 0}

    def progress(event):
        if event["phase"] == "read":
            reads["count"] = event["done"]

    with Index.open(db) as index:
        with pytest.raises(OperationCancelled) as excinfo:
            ingest_path(
                index,
                root,
                progress=progress,
                cancel=lambda: reads["count"] >= BATCH_SIZE + 20,
            )
        assert excinfo.value.exit_code == 130
        assert index.count("documents") == BATCH_SIZE
        run = index.last_run()
        assert run["status"] == "cancelled" and run["added"] == BATCH_SIZE
        assert index.health()["ok"] is True
        resumed = ingest_path(index, root)
        assert resumed["unchanged"] == BATCH_SIZE and resumed["added"] == 40


def test_cancel_before_start(notes, db):
    with Index.open(db) as index:
        with pytest.raises(OperationCancelled):
            ingest_path(index, notes, cancel=lambda: True)
        assert index.count("documents") == 0
        assert index.last_run()["status"] == "cancelled"


# -- single files & JSONL ---------------------------------------------------------------------


def test_single_file_registers_parent_root(notes, db):
    target = notes / "projects" / "alpha.txt"
    with Index.open(db) as index:
        result = ingest_path(index, target)
        doc = index.get_document("alpha.txt")
        assert result["added"] == 1 and result["files_indexed"] == 1
        assert doc["folder"] == "" and doc["ext"] == ".txt"
        [root] = index.roots()
        assert root["path"] == str(target.resolve().parent)
        assert doc["root_id"] == root["root_id"] == result["root_id"]
        assert ingest_path(index, target)["unchanged"] == 1


def test_skipped_single_file_raises(tmp_path, db):
    target = write(tmp_path / "empty.md", "")
    with Index.open(db) as index:
        with pytest.raises(CorpusNotFoundError, match="empty"):
            ingest_path(index, target)
        assert index.roots() == []


def test_jsonl_metadata_and_incremental(tmp_path, db):
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text(
        '{"_id":"d1","title":"First","text":"alpha"}\n'
        '{"_id":"d2","text":"beta"}\n'
        '{"_id":"d3","text":"alpha"}\n',
        encoding="utf-8",
    )
    with Index.open(db) as index:
        first = ingest_path(index, corpus)
        doc = index.get_document("d1")
        assert doc["folder"] is None and doc["ext"] is None and doc["mtime"] is None
        assert doc["content_hash"] == hashlib.sha256(b"alpha").hexdigest()
        assert doc["root_id"] is None and index.roots() == []
        assert first["duplicates"] == [["d1", "d3"]]
        assert ingest_path(index, corpus)["unchanged"] == 3

        corpus.write_text(
            '{"_id":"d1","title":"Renamed","text":"alpha"}\n'
            '{"_id":"d2","text":"beta"}\n'
            '{"_id":"d3","text":"alpha"}\n',
            encoding="utf-8",
        )
        changed = ingest_path(index, corpus)
        assert (changed["updated"], changed["unchanged"]) == (1, 2)
        assert index.get_document("d1")["title"] == "Renamed"


def test_unsupported_path_message(tmp_path, db):
    target = write(tmp_path / "file.pdf", "x")
    with Index.open(db) as index:
        with pytest.raises(CorpusNotFoundError, match="unsupported"):
            ingest_path(index, target)
        assert index.last_run() is None  # nothing recorded for an invalid path


def test_load_documents_applies_default_excludes(tmp_path):
    root = tmp_path / "ld"
    write(root / "a.md", "alpha")
    write(root / ".hidden" / "b.md", "beta")
    write(root / "c.md", "")
    assert [doc.doc_id for doc in load_documents(root)] == ["a.md"]
    assert [doc.doc_id for doc in load_documents(root, exclude=["a.md"])] == []


# -- scan_changes ----------------------------------------------------------------------------


def test_scan_changes_detects_new_changed_deleted(notes, db):
    with Index.open(db) as index:
        unregistered = scan_changes(index, notes)
        assert unregistered["new"] == 3 and unregistered["deleted"] == 0

        ingest_path(index, notes)
        clean = scan_changes(index, notes)
        assert clean == {
            "root": str(notes.resolve()),
            "exists": True,
            "new": 0,
            "changed": 0,
            "deleted": 0,
            "unchanged": 3,
            "truncated": False,
        }

        mtime = (notes / "today.md").stat().st_mtime
        os.utime(notes / "today.md", (mtime + 50, mtime + 50))  # touched, same bytes
        write(notes / "projects" / "alpha.txt", "alpha project kickoff notes, revised")
        (notes / "projects" / "deep" / "beta.md").unlink()
        write(notes / "fresh.md", "fresh")
        write(notes / "drafts" / "skip.md", "excluded later")
        changes_before = index.conn.total_changes
        result = scan_changes(index, notes, exclude=["drafts"])
        assert index.conn.total_changes == changes_before  # read-only
        assert (result["new"], result["changed"], result["deleted"], result["unchanged"]) == (
            1, 1, 1, 1
        )


def test_scan_changes_uses_stored_excludes_and_limit(tmp_path, db):
    root = tmp_path / "scan"
    for number in range(5):
        write(root / f"n{number}.md", f"note {number}")
    write(root / "drafts" / "d.md", "draft")
    with Index.open(db) as index:
        ingest_path(index, root, exclude=["drafts"])
        assert scan_changes(index, root)["new"] == 0
        truncated = scan_changes(index, root, limit=2)
        assert truncated["truncated"] is True
        assert truncated["unchanged"] == 2 and truncated["deleted"] == 0
        with pytest.raises(UsageError):
            scan_changes(index, root, limit=0)


def test_scan_changes_missing_root(notes, db):
    with Index.open(db) as index:
        ingest_path(index, notes)
        for path in sorted(notes.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        notes.rmdir()
        result = scan_changes(index, notes)
        assert result["exists"] is False and result["deleted"] == 3


def test_database_stays_openable_by_plain_sqlite(notes, db):
    with Index.open(db) as index:
        ingest_path(index, notes)
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
