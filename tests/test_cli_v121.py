"""v1.2.1 CLI: rebuild-vectors, passage hints in status/doctor, and folder management.

Model-free like tests/test_cli_v3.py: a tiny deterministic fake embedder
stands in for MiniLM where vectors are needed, ``ONEFIND_HOME`` points at a
temporary library and the working directory is a temporary folder.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import signal
from pathlib import Path

import numpy as np
import pytest

import onefind.store as store_module
from onefind import cli
from onefind.cli import EXIT_CANCELLED, EXIT_DATA, EXIT_OK, EXIT_USAGE, main
from onefind.ingest import ingest_path
from onefind.library import Library
from onefind.store import Index

SENTENCE = "Day {n} at the weekly market: bread, cheese and apples were on sale early."


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


def long_body(sentences: int = 60) -> str:
    return "\n\n".join(
        " ".join(SENTENCE.format(n=n * 4 + i) for i in range(4)) for n in range(sentences // 4)
    )


@pytest.fixture
def home(tmp_path, monkeypatch):
    library_home = tmp_path / "home"
    monkeypatch.setenv("ONEFIND_HOME", str(library_home))
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return library_home


@pytest.fixture
def corpus(tmp_path):
    root = tmp_path / "notes"
    (root / "drafts").mkdir(parents=True)
    (root / "short.md").write_text("# Short\n\nTomatoes need sun.", encoding="utf-8")
    (root / "diary.md").write_text(f"# Market diary\n\n{long_body()}\n", encoding="utf-8")
    (root / "drafts" / "idea.md").write_text("# Idea\n\nA rough idea.", encoding="utf-8")
    return root


def run(capsys, *argv):
    code = main([str(arg) for arg in argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def old_collection(db: Path, corpus: Path, *, vectors: bool, many: int = 0) -> None:
    """A pre-1.2.1 style collection: passage search off, optionally with (fake) vectors."""
    for number in range(many):
        (corpus / f"long-{number:02d}.md").write_text(f"# Long {number}\n\n{long_body()}\n", encoding="utf-8")
    with Index.open(db) as index:
        index.set_chunking(None)
        if vectors:
            index.attach_embedder(FakeEmbedder())
        ingest_path(index, corpus)


# -- rebuild-vectors ---------------------------------------------------------------------------


def test_keyword_only_collection_gets_passages_without_a_model(home, corpus, capsys):
    db = Path("old.db").resolve()
    old_collection(db, corpus, vectors=False)
    code, out, _ = run(capsys, "status", "--db", db, "--no-scan")
    assert code == EXIT_OK
    assert "passages : off - one vector per note; meaning search reads only the beginning of 1 note" in out
    assert "recommended" not in out  # nothing to gain without meaning search
    code, out, err = run(capsys, "rebuild-vectors", "--db", db, "--chunking", "default", "-q")
    assert code == EXIT_OK, err
    assert "passage search: on (200 words per passage)" in out
    assert "rebuilt 1 note into" in out and "keyword-only collection" in out
    code, out, _ = run(capsys, "status", "--db", db, "--no-scan")
    assert "passages : on (200 words, 40 overlap; 1 long note split into passages)" in out


def test_status_and_doctor_recommend_passages_and_rebuild_builds_vectors(home, corpus, capsys, monkeypatch):
    db = Path("old.db").resolve()
    old_collection(db, corpus, vectors=True)
    code, out, _ = run(capsys, "status", "--db", db, "--no-scan")
    assert code == EXIT_OK
    assert "recommended: 1 note is longer than one passage" in out
    assert f"onefind rebuild-vectors --db {db} --chunking default" in out
    code, out, _ = run(capsys, "status", "--db", db, "--no-scan", "--json")
    report = json.loads(out)
    assert report["chunking"] is None and report["long_notes"] == 1
    assert report["vectors_need_rebuild"]["recommended"] is True
    code, out, _ = run(capsys, "doctor", "--db", db)
    assert code == EXIT_OK  # a recommendation is not damage
    assert "passages      : recommended" in out and "no problems found" in out

    loaded = []
    monkeypatch.setattr(cli, "_load_embedder", lambda name: loaded.append(name) or FakeEmbedder())
    code, out, err = run(capsys, "rebuild-vectors", "--db", db, "--chunking", "default")
    assert code == EXIT_OK, err
    assert loaded == ["fake-mini"]  # the collection's own model
    assert "rebuilt 1 note into" in out and "vectors built" in out
    assert "passages : on (200 words, 40 overlap)" in out
    with Index.open(db) as index:
        assert index.vectors_need_rebuild()["needed"] is False
        assert index.health()["ok"] is True
        assert index.count("vectors") > index.count("documents")
    code, out, _ = run(capsys, "status", "--db", db, "--no-scan")
    assert "recommended" not in out and "rebuild needed" not in out
    # Nothing left to do: a second run says so.
    code, out, _ = run(capsys, "rebuild-vectors", "--db", db, "-q")
    assert code == EXIT_OK and "vectors are up to date" in out


def test_a_pending_rebuild_is_reported_and_finished(home, corpus, capsys, monkeypatch):
    code, _, err = run(capsys, "index", corpus, "--collection", "Notes", "-q")
    assert code == EXIT_OK, err
    db = Path(Library().get("Notes")["db"])
    with Index.open(db) as index:
        index.set_chunking({"max_words": 120, "overlap_words": 20})
    code, out, _ = run(capsys, "status", "--collection", "Notes", "--no-scan")
    assert "rebuild needed: 1 note still follow an older passage setting" in out
    assert "onefind rebuild-vectors --collection Notes (updates the search passages)" in out
    code, out, _ = run(capsys, "doctor", "--collection", "Notes")
    assert code == EXIT_OK and "rebuild needed" in out
    code, out, err = run(capsys, "rebuild-vectors", "--collection", "Notes", "-q")
    assert code == EXIT_OK, err
    assert "passages : on (120 words, 20 overlap)" in out
    code, out, _ = run(capsys, "status", "--collection", "Notes", "--no-scan")
    assert "rebuild needed" not in out


def test_rebuild_with_vectors_needs_the_model(home, corpus, capsys, monkeypatch):
    db = Path("old.db").resolve()
    old_collection(db, corpus, vectors=True)

    def unavailable(name):
        raise cli.EnvError("semantic search needs the model stack: no module named torch")

    monkeypatch.setattr(cli, "_load_embedder", unavailable)
    code, _, err = run(capsys, "rebuild-vectors", "--db", db, "--chunking", "default")
    assert code == 3 and "model stack" in err
    with Index.open(db) as index:  # the setting is only changed once the model is there
        assert index.chunking_config() is None


def test_ctrl_c_during_rebuild_keeps_finished_notes_and_a_rerun_finishes(home, corpus, capsys, monkeypatch):
    db = Path("old.db").resolve()
    old_collection(db, corpus, vectors=False, many=20)  # 21 long notes: two batches of 16
    original = store_module.split_passages
    calls = {"n": 0}

    def interrupting(body, config):
        calls["n"] += 1
        if calls["n"] == 1 and config is not None:
            handler = signal.getsignal(signal.SIGINT)
            assert callable(handler) and handler is not signal.default_int_handler
            handler(signal.SIGINT, None)  # the first Ctrl+C
        return original(body, config)

    monkeypatch.setattr(store_module, "split_passages", interrupting)
    before = signal.getsignal(signal.SIGINT)
    code, _, err = run(capsys, "rebuild-vectors", "--db", db, "--chunking", "default", "-q")
    assert code == EXIT_CANCELLED
    assert "stopping rebuilding vectors after the current step" in err
    assert "Notes finished so far keep their new passages" in err
    assert f"onefind rebuild-vectors --db {db}" in err
    assert signal.getsignal(signal.SIGINT) is before
    with Index.open(db) as index:
        pending = index.vectors_need_rebuild()
        assert pending["needed"] is True and 0 < pending["documents"] < 21
    monkeypatch.setattr(store_module, "split_passages", original)
    code, out, err = run(capsys, "rebuild-vectors", "--db", db, "-q")
    assert code == EXIT_OK, err
    assert f"rebuilt {pending['documents']} notes into" in out
    with Index.open(db) as index:
        assert index.vectors_need_rebuild()["needed"] is False


def test_rebuild_vectors_needs_an_existing_collection(home, capsys):
    code, _, err = run(capsys, "rebuild-vectors", "--db", "missing.db")
    assert code == EXIT_DATA and "no OneFind collection" in err
    assert not Path("missing.db").exists()
    with pytest.raises(SystemExit) as excinfo:
        main(["rebuild-vectors", "--chunking", "huge"])
    assert excinfo.value.code == EXIT_USAGE


# -- folders -----------------------------------------------------------------------------------


def test_folders_list_shows_ids_exclusions_and_missing_folders(home, corpus, tmp_path, capsys):
    code, _, err = run(capsys, "index", corpus, "--collection", "Notes", "--exclude", "drafts", "-q")
    assert code == EXIT_OK, err
    code, out, _ = run(capsys, "folders", "list", "--collection", "Notes")
    assert code == EXIT_OK
    assert f"[1] {corpus.resolve()}" in out
    assert "2 notes, indexed" in out and ", ok" in out
    assert "excluding: drafts" in out
    code, out, _ = run(capsys, "folders", "list", "--collection", "Notes", "--json")
    listed = json.loads(out)
    assert listed["collection"] == "Notes"
    assert listed["folders"][0]["exclude"] == ["drafts"] and listed["folders"][0]["exists"] is True
    moved = tmp_path / "moved-notes"
    shutil.move(str(corpus), str(moved))
    code, out, _ = run(capsys, "folders", "list", "--collection", "Notes")
    assert "missing - moved or deleted?" in out
    assert "onefind folders relocate OLD NEW --collection Notes" in out


def test_folders_relocate_keeps_note_ids(home, corpus, tmp_path, capsys):
    code, _, err = run(capsys, "index", corpus, "--collection", "Notes", "-q")
    assert code == EXIT_OK, err
    db = Path(Library().get("Notes")["db"])
    with Index.open(db) as index:
        ids_before = sorted(index.fingerprints())
    moved = tmp_path / "moved-notes"
    shutil.move(str(corpus), str(moved))
    code, _, err = run(capsys, "folders", "relocate", corpus, tmp_path / "nowhere", "--collection", "Notes")
    assert code == EXIT_DATA and "does not exist" in err
    code, _, err = run(capsys, "folders", "relocate", tmp_path / "unknown", moved, "--collection", "Notes")
    assert code == EXIT_DATA and "is not a folder of this collection" in err
    code, out, err = run(capsys, "folders", "relocate", "1", moved, "--collection", "Notes")
    assert code == EXIT_OK, err
    assert f"-> {moved.resolve()}" in out and "keep their ids" in out
    assert f"onefind index {moved.resolve()} --collection Notes --prune" in out
    with Index.open(db) as index:
        assert sorted(index.fingerprints()) == ids_before
        assert index.roots()[0]["path"] == str(moved.resolve())
    # The update afterwards finds nothing new: the notes were not indexed twice.
    code, out, _ = run(capsys, "index", moved, "--collection", "Notes", "-q")
    assert code == EXIT_OK and "0 added" in out and "3 unchanged" in out


def test_folders_remove_deletes_or_keeps_the_notes(home, corpus, tmp_path, capsys):
    other = tmp_path / "travel"
    other.mkdir()
    (other / "trip.md").write_text("# Trip\n\nTram 28 to the castle.", encoding="utf-8")
    for folder in (corpus, other):
        code, _, err = run(capsys, "index", folder, "--collection", "Notes", "-q")
        assert code == EXIT_OK, err
    code, out, err = run(capsys, "folders", "remove", other, "--collection", "Notes", "--keep-documents")
    assert code == EXIT_OK, err
    assert "stay searchable but are no longer updated" in out
    assert "now holds 4 notes" in out
    code, out, err = run(capsys, "folders", "remove", corpus, "--collection", "Notes")
    assert code == EXIT_OK, err
    assert "and its 3 notes from the index; files on disk were not touched" in out
    assert "now holds 1 note" in out
    assert f"add it back: onefind index {corpus.resolve()} --collection Notes" in out
    assert (corpus / "short.md").is_file()
    code, _, err = run(capsys, "folders", "remove", corpus, "--collection", "Notes")
    assert code == EXIT_DATA and "is not a folder of this collection" in err
    with pytest.raises(SystemExit) as excinfo:
        main(["folders", "remove", str(corpus), "--keep-documents", "--delete-documents"])
    assert excinfo.value.code == EXIT_USAGE


# -- smoke uses the app's keyword matching -------------------------------------------------------


def test_run_smoke_matches_like_the_app(home, corpus, tmp_path):
    from onefind.errors import UsageError
    from onefind.evaluate import run_smoke

    db = tmp_path / "smoke.db"
    with Index.open(db) as index:
        ingest_path(index, corpus)
    queries = tmp_path / "queries.jsonl"
    queries.write_text(
        "\n".join(json.dumps(case) for case in (
            # Only "tomatoes" is in a note: all-words finds nothing, auto tops up.
            {"query": "tomatoes zeppelin", "relevant": ["short.md"], "mode": "lexical"},
            # A no-answer question sharing one common word with a note: auto returns
            # only weak partial hits, which the app shows as "No strong matches".
            {"query": "zeppelin hangar mooring sun", "relevant": [], "mode": "lexical"},
        )) + "\n",
        encoding="utf-8",
    )
    auto = run_smoke(db, queries, k=5)
    assert auto["lexical_match"] == "auto"
    assert auto["rows"][0]["rank"] == 1 and auto["hit_at_3"] == 1.0
    assert auto["rows"][1]["returned"] and auto["no_answer_accuracy"] == 1.0
    strict = run_smoke(db, queries, k=5, lexical_match="all")
    assert strict["lexical_match"] == "all"
    assert strict["rows"][0]["rank"] is None and strict["rows"][1]["returned"] == []
    assert strict["no_answer_accuracy"] == 1.0
    with pytest.raises(UsageError):
        run_smoke(db, queries, lexical_match="some")
