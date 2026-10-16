"""v1.2 CLI (docs/11-implementation-contract.md section 7).

Model-free: every test runs lexical-only against temporary folders, with
``ONEFIND_HOME`` pointing at a temporary library and the working directory
moved to a temporary folder (so the default ``onefind.db`` never lands in
the repository or the real user profile).
"""

from __future__ import annotations

import io
import json
import os
import signal
import sqlite3
import sys
import time
import types
from pathlib import Path

import pytest

import onefind.ingest as ingest_module
from onefind import cli
from onefind.cli import EXIT_CANCELLED, EXIT_DATA, EXIT_OK, EXIT_USAGE, ProgressPrinter, main
from onefind.errors import OperationCancelled
from onefind.library import Library
from onefind.store import Index

NOTES = {
    "rollback.md": "---\ntitle: Rollback plan\ntags: [ops, release]\n---\n"
    "Rollback the deployment when the canary error rate rises.",
    "garden.txt": "Tomatoes need full sun and regular watering in summer.",
    "projects/alpha.md": "# Alpha launch\nThe alpha launch checklist covers rollback owners.",
    "projects/drafts/idea.md": "# Draft idea\nA rough rollback idea for later.",
    "empty.md": "",
}


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
    for relative, text in NOTES.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (root / "image.txt").write_bytes(b"\x89PNG\x00\x00binary")
    return root


def run(capsys, *argv):
    code = main([str(arg) for arg in argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def indexed(capsys, corpus, *extra):
    code, out, err = run(capsys, "index", corpus, *extra)
    assert code == EXIT_OK, err
    return out, err


# -- index ---------------------------------------------------------------------------


def test_index_defaults_to_onefind_db_and_prints_rich_summary(home, corpus, capsys):
    out, err = indexed(capsys, corpus)
    assert Path("onefind.db").is_file()
    assert out.startswith("indexed 4 files (4 documents total) -> onefind.db")
    assert "[lexical only - add --embed for semantic search]" in out
    assert "changes    : 4 added, 0 updated, 0 unchanged, 0 removed, 2 skipped" in out
    assert "1 binary, 1 empty" in out
    assert "image.txt (binary)" in out and "empty.md (empty)" in out
    assert "vectors    : none - keyword search only" in out
    # non-TTY progress: plain lines on stderr, one per phase
    assert "Found 6 supported files" in err or "Looking for notes" in err
    assert "\r" not in err
    assert not home.exists()  # the default database does not touch the library


def test_reindex_counts_unchanged_and_full_rewrites(home, corpus, capsys):
    indexed(capsys, corpus)
    out, _ = indexed(capsys, corpus)
    assert "0 added, 0 updated, 4 unchanged" in out
    out, _ = indexed(capsys, corpus, "--full")
    assert "0 added, 4 updated, 0 unchanged" in out


def test_exclude_is_repeatable_and_remembered(home, corpus, capsys):
    out, _ = indexed(capsys, corpus, "--exclude", "drafts", "--exclude", "*.txt")
    assert "changes    : 2 added" in out
    assert "excluding  : drafts, *.txt (remembered for this folder)" in out
    code, out, _ = run(capsys, "search", "rough", "--mode", "lexical")
    assert code == EXIT_OK and "no results" in out
    # a later run without --exclude reuses the stored patterns
    out, _ = indexed(capsys, corpus)
    assert "0 added, 0 updated, 2 unchanged" in out


def test_prune_removes_deleted_files_and_hint_without_it(home, corpus, capsys):
    indexed(capsys, corpus)
    (corpus / "garden.txt").unlink()
    out, _ = indexed(capsys, corpus)
    assert "0 removed" in out
    assert "1 indexed note no longer matches a file here" in out
    out, _ = indexed(capsys, corpus, "--prune")
    assert "3 unchanged, 1 removed" in out
    assert "--prune to remove" not in out


def test_index_quiet_suppresses_progress(home, corpus, capsys):
    _, err = indexed(capsys, corpus, "--quiet")
    assert err == ""


def test_index_into_new_collection_creates_and_lists_it(home, corpus, capsys):
    out, err = indexed(capsys, corpus, "--collection", "My Notes")
    assert "created collection 'My Notes'" in err
    assert "collection : My Notes" in out
    assert not Path("onefind.db").exists()
    entry = Library(home).get("my notes")
    assert entry["active"] and Path(entry["db"]).is_file()

    code, out, _ = run(capsys, "collections", "list")
    assert code == EXIT_OK
    line = next(row for row in out.splitlines() if "My Notes" in row)
    assert line.startswith("*") and " 4 " in line

    code, out, _ = run(capsys, "collections", "list", "--json")
    payload = json.loads(out)
    assert payload["collections"][0]["documents"] == 4
    assert payload["home"] == str(home)


def test_index_hints_when_active_collection_exists(home, corpus, capsys):
    run(capsys, "collections", "create", "Work")
    _, err = indexed(capsys, corpus)
    assert "use --collection Work" in err
    assert Path("onefind.db").is_file()


def test_missing_corpus_is_a_data_error(home, tmp_path, capsys):
    code, _, err = run(capsys, "index", tmp_path / "nope")
    assert code == EXIT_DATA
    assert err.startswith("error: corpus path not found")


# -- search ----------------------------------------------------------------------------


def test_search_filters_folder_type_tag_since(home, corpus, capsys):
    indexed(capsys, corpus)
    code, out, _ = run(capsys, "search", "rollback", "--mode", "lexical")
    assert code == EXIT_OK
    assert "rollback.md" in out and "projects/alpha.md" in out

    _, out, _ = run(capsys, "search", "rollback", "--mode", "lexical", "--folder", "projects")
    assert "projects/alpha.md" in out and "rollback.md -" not in out.replace("/rollback", "")
    assert "1. " in out and "projects/drafts/idea.md" in out

    _, out, _ = run(capsys, "search", "rollback", "--mode", "lexical", "--tag", "ops")
    assert out.count(". [") == 1 and "rollback.md" in out

    _, out, _ = run(capsys, "search", "tomatoes", "--mode", "lexical", "--type", "md")
    assert "no results" in out and "filters: type=md" in out

    _, out, _ = run(capsys, "search", "tomatoes", "--mode", "lexical", "--type", "txt")
    assert "garden.txt" in out

    _, out, _ = run(
        capsys, "search", "rollback", "--mode", "lexical", "--since", "2999-01-01"
    )
    assert "no results" in out and "since=2999-01-01" in out

    _, out, _ = run(
        capsys, "search", "rollback", "--mode", "lexical", "--since", "2000-01-01"
    )
    assert "rollback.md" in out


def test_search_rejects_bad_since(home, corpus, capsys):
    indexed(capsys, corpus)
    code, _, err = run(capsys, "search", "x", "--mode", "lexical", "--since", "last week")
    assert code == EXIT_USAGE
    assert "--since must be a date" in err


def test_search_explain_prints_reasons(home, corpus, capsys):
    indexed(capsys, corpus)
    code, out, _ = run(
        capsys, "search", "rollback owners", "--mode", "lexical", "--explain"
    )
    assert code == EXIT_OK
    lines = out.splitlines()
    assert lines[0].startswith(" 1. ") and "projects/alpha.md" in lines[0]
    assert any(line.strip().startswith("why (strong match)") for line in lines)
    assert any("Matched all 2 keywords" in line for line in lines)


def test_search_missing_database_is_friendly_and_creates_nothing(home, capsys):
    code, _, err = run(capsys, "search", "anything", "--mode", "lexical")
    assert code == EXIT_DATA
    assert "no OneFind collection at onefind.db" in err
    assert not Path("onefind.db").exists()


def test_search_by_collection_and_unknown_collection(home, corpus, capsys):
    indexed(capsys, corpus, "--collection", "Notes")
    code, out, _ = run(
        capsys, "search", "tomatoes", "--collection", "notes", "--mode", "lexical"
    )
    assert code == EXIT_OK and "garden.txt" in out
    code, _, err = run(capsys, "search", "x", "--collection", "Nope", "--mode", "lexical")
    assert code == EXIT_DATA
    assert "no collection named 'Nope'" in err and "known: Notes" in err


def test_db_option_wins_over_collection(home, corpus, tmp_path, capsys):
    indexed(capsys, corpus, "--collection", "Notes")
    other = tmp_path / "other.db"
    indexed(capsys, corpus, "--db", other, "--exclude", "*.md")
    code, out, _ = run(
        capsys, "search", "rollback", "--db", other, "--collection", "Notes",
        "--mode", "lexical",
    )
    assert code == EXIT_OK and "no results" in out


def test_search_falls_back_to_active_collection(home, corpus, capsys):
    indexed(capsys, corpus, "--collection", "Notes")
    code, out, err = run(capsys, "search", "tomatoes", "--mode", "lexical")
    assert code == EXIT_OK and "garden.txt" in out
    assert "using the active collection 'Notes'" in err


def test_semantic_search_without_vectors_explains_fix(home, corpus, capsys):
    indexed(capsys, corpus)
    code, _, err = run(capsys, "search", "anything", "--mode", "hybrid")
    assert code == EXIT_USAGE
    assert "--mode lexical" in err


def test_non_onefind_database_is_a_data_error(home, tmp_path, capsys):
    bogus = tmp_path / "bogus.db"
    conn = sqlite3.connect(bogus)
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()
    code, _, err = run(capsys, "status", "--db", bogus)
    assert code == EXIT_DATA
    assert "not a OneFind database" in err


# -- collections -------------------------------------------------------------------------


def test_collections_lifecycle(home, tmp_path, capsys):
    code, out, _ = run(capsys, "collections", "list")
    assert code == EXIT_OK and "no collections yet" in out

    code, out, _ = run(capsys, "collections", "create", "Work")
    assert code == EXIT_OK and "created collection 'Work'" in out
    assert "it is the active collection" in out
    code, out, _ = run(capsys, "collections", "create", "Home")
    assert "active" not in out.splitlines()[1]

    code, _, err = run(capsys, "collections", "create", "work")
    assert code == EXIT_USAGE and "already exists" in err

    code, out, _ = run(capsys, "collections", "use", "home")
    assert code == EXIT_OK and "active collection: 'Home'" in out
    assert Library(home).active()["name"] == "Home"

    code, out, _ = run(capsys, "collections", "rename", "Home", "Personal")
    assert code == EXIT_OK and "renamed 'Home' to 'Personal'" in out
    assert Library(home).active()["name"] == "Personal"

    db = Path(Library(home).get("Personal")["db"])
    code, _, err = run(capsys, "collections", "remove", "Personal", "--delete-file")
    assert code == EXIT_USAGE and "add --yes" in err
    assert db.is_file() and Library(home).get("Personal")

    code, out, _ = run(capsys, "collections", "remove", "Personal", "--delete-file", "--yes")
    assert code == EXIT_OK and "deleted" in out
    assert not db.exists()
    assert "active collection is now 'Work'" in out

    work_db = Path(Library(home).get("Work")["db"])
    code, out, _ = run(capsys, "collections", "remove", "Work")
    assert code == EXIT_OK and "the file is kept" in out
    assert work_db.is_file()
    assert Library(home).list() == []

    code, _, err = run(capsys, "collections", "use", "Ghost")
    assert code == EXIT_DATA and "unknown collection" in err


def test_collections_create_adds_existing_database(home, corpus, tmp_path, capsys):
    db = tmp_path / "moved.db"
    indexed(capsys, corpus, "--db", db)
    code, out, _ = run(capsys, "collections", "create", "Moved", "--db", db)
    assert code == EXIT_OK and "added existing collection 'Moved'" in out
    code, out, _ = run(capsys, "search", "tomatoes", "--collection", "Moved", "--mode", "lexical")
    assert "garden.txt" in out


# -- backup / restore ----------------------------------------------------------------------


def test_backup_and_restore_round_trip(home, corpus, tmp_path, capsys):
    indexed(capsys, corpus, "--collection", "Notes")
    code, out, _ = run(capsys, "backup", "--collection", "Notes")
    assert code == EXIT_OK
    backups = Library(home).list_backups("Notes")
    assert len(backups) == 1
    backup = Path(backups[0]["path"])
    assert backup.parent == (home / "backups").resolve() or backup.parent == home / "backups"
    assert "4 documents" in out and "onefind restore" in out

    code, out, _ = run(capsys, "backup", "--list")
    assert code == EXIT_OK and backup.name in out

    code, out, _ = run(capsys, "restore", backup, "--as", "Notes copy")
    assert code == EXIT_OK
    assert "restored 4 documents" in out and "'Notes copy'" in out
    assert "collections use" in out  # Notes stays active
    restored = Library(home).get("Notes copy")
    assert restored["db"] != Library(home).get("Notes")["db"]
    with Index.open(restored["db"]) as index:
        assert index.count("documents") == 4
    # restoring by file name inside the backups folder also works
    code, _, _ = run(capsys, "restore", backup.name, "--as", "Second")
    assert code == EXIT_OK

    code, _, err = run(capsys, "restore", backup, "--as", "notes")
    assert code == EXIT_USAGE and "already exists" in err


def test_backup_out_folder_and_default_beside_db(home, corpus, tmp_path, capsys):
    indexed(capsys, corpus)
    code, out, _ = run(capsys, "backup")
    assert code == EXIT_OK
    siblings = list(Path(".").glob("onefind-backup-*.db"))
    assert len(siblings) == 1

    out_dir = tmp_path / "keep"
    out_dir.mkdir()
    code, out, _ = run(capsys, "backup", "--out", out_dir)
    assert code == EXIT_OK and len(list(out_dir.glob("onefind-*.db"))) == 1

    target = tmp_path / "exact.db"
    assert run(capsys, "backup", "--out", target)[0] == EXIT_OK
    code, _, err = run(capsys, "backup", "--out", target)
    assert code == EXIT_USAGE and "already exists" in err
    assert run(capsys, "backup", "--out", target, "--overwrite")[0] == EXIT_OK

    # A new path without an extension (or with a trailing slash) is a folder,
    # created on demand: never a backup file called "fresh" with no .db.
    for raw in (tmp_path / "fresh", f"{tmp_path / 'slashed'}/"):
        assert run(capsys, "backup", "--out", raw)[0] == EXIT_OK
        folder = Path(str(raw).rstrip("/"))
        assert folder.is_dir() and len(list(folder.glob("onefind-*.db"))) == 1


def test_restore_rejects_missing_and_invalid_files(home, tmp_path, capsys):
    code, _, err = run(capsys, "restore", tmp_path / "missing.db", "--as", "X")
    assert code == EXIT_DATA and "backup file not found" in err

    junk = tmp_path / "junk.db"
    junk.write_bytes(b"this is not sqlite at all" * 100)
    code, _, err = run(capsys, "restore", junk, "--as", "Junk")
    assert code == EXIT_DATA and "not a OneFind backup" in err
    assert Library(home).list() == []
    leftovers = list((home / "collections").glob("*")) if (home / "collections").exists() else []
    assert leftovers == []

    code, _, err = run(capsys, "restore", junk, "--as", "   ")
    assert code == EXIT_USAGE


# -- doctor / compact / remove / status --------------------------------------------------------


def test_doctor_reports_and_repairs(home, corpus, capsys):
    indexed(capsys, corpus)
    code, out, _ = run(capsys, "doctor")
    assert code == EXIT_OK
    assert "keyword index : ok" in out and "no problems found" in out
    assert "vectors       : not built" in out

    with Index.open("onefind.db") as index:
        index.conn.execute("DELETE FROM fts WHERE doc_id = 'garden.txt'")
        index.conn.commit()
    code, out, _ = run(capsys, "doctor")
    assert code == EXIT_DATA
    assert "problems:" in out and "onefind doctor --repair" in out

    code, out, _ = run(capsys, "doctor", "--repair")
    assert code == EXIT_OK
    assert "repaired: rebuilt the keyword index for 4 notes" in out
    assert "no problems found" in out
    code, out, _ = run(capsys, "search", "tomatoes", "--mode", "lexical")
    assert "garden.txt" in out


def test_compact_reports_sizes(home, corpus, capsys):
    indexed(capsys, corpus)
    code, out, _ = run(capsys, "compact")
    assert code == EXIT_OK
    assert out.startswith("compacted onefind.db: ") and "saved" in out


def test_remove_documents(home, corpus, capsys):
    indexed(capsys, corpus)
    code, out, err = run(capsys, "remove", "garden.txt", "ghost.md")
    assert code == EXIT_OK
    assert "removed 1 document from the index" in out
    assert "not found: ghost.md" in err
    assert (corpus / "garden.txt").is_file()
    with Index.open("onefind.db") as index:
        assert index.get_document("garden.txt") is None

    code, _, err = run(capsys, "remove", "ghost.md")
    assert code == EXIT_DATA and "none of these documents" in err


def test_status_health_stale_scan_and_last_run(home, corpus, capsys):
    # scan_changes counts a skipped (binary) file as "new" on every scan, so
    # this folder holds only indexable notes.
    (corpus / "image.txt").unlink()
    indexed(capsys, corpus, "--collection", "Notes")
    code, out, _ = run(capsys, "status")
    assert code == EXIT_OK
    assert "collection : Notes" in out
    assert "health   : ready - Ready - 4 notes searchable by keyword." in out
    assert "up to date" in out, out
    assert "last run : succeeded" in out and "4 added" in out

    time.sleep(0.01)
    (corpus / "new.md").write_text("# New\nfresh note", encoding="utf-8")
    (corpus / "garden.txt").unlink()
    code, out, _ = run(capsys, "status", "--collection", "Notes")
    assert "1 new, 1 deleted since the last index" in out
    assert "--prune" in out

    code, out, _ = run(capsys, "status", "--json")
    payload = json.loads(out)
    assert payload["collection"] == "Notes" and payload["stale"] is True
    assert payload["state"] == "ready"
    assert payload["roots"][0]["changes"]["new"] == 1
    assert payload["last_run"]["status"] == "succeeded"

    code, out, _ = run(capsys, "status", "--no-scan")
    assert code == EXIT_OK and "since the last index" not in out


def test_status_of_empty_collection_teaches_next_step(home, capsys):
    run(capsys, "collections", "create", "Empty")
    code, out, _ = run(capsys, "status")
    assert code == EXIT_OK
    assert "empty - Empty - index a folder to start" in out
    assert "last run : never" in out


def test_status_reports_missing_vectors(home, corpus, capsys):
    indexed(capsys, corpus)
    with Index.open("onefind.db") as index:
        if not index.vec_ready:
            pytest.skip("sqlite-vec unavailable")
        index.conn.execute(
            "CREATE VIRTUAL TABLE vec_float USING vec0(embedding float[4] distance_metric=cosine)"
        )
        index.set_meta("model_name", "fake-model")
        index.set_meta("model_dim", "4")
    code, out, _ = run(capsys, "status")
    assert code == EXIT_OK
    assert "partly ready" in out and "4 notes have no meaning vector" in out
    code, out, _ = run(capsys, "index", corpus)
    assert "4 of 4 notes have no meaning vector yet - rerun with --embed" in out


# -- cancellation --------------------------------------------------------------------------


def test_ctrl_c_during_index_cancels_cleanly(home, corpus, capsys, monkeypatch):
    original = ingest_module.read_text_document
    calls = {"n": 0}

    def interrupting(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            handler = signal.getsignal(signal.SIGINT)
            assert callable(handler) and handler is not signal.default_int_handler
            handler(signal.SIGINT, None)  # what the OS does on the first Ctrl+C
        return original(*args, **kwargs)

    monkeypatch.setattr(ingest_module, "read_text_document", interrupting)
    before = signal.getsignal(signal.SIGINT)
    code, _, err = run(capsys, "index", corpus)
    assert code == EXIT_CANCELLED
    assert "stopping indexing after the current step" in err
    assert "cancelled: indexing stopped early" in err
    assert "Run the same command again" in err
    assert signal.getsignal(signal.SIGINT) is before
    with Index.open("onefind.db") as index:
        assert index.last_run()["status"] == "cancelled"


def test_hard_keyboard_interrupt_during_index(home, corpus, capsys, monkeypatch):
    def boom(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(ingest_module, "read_text_document", boom)
    code, _, err = run(capsys, "index", corpus)
    assert code == EXIT_CANCELLED
    assert "cancelled: indexing stopped early" in err
    assert "0 added" in err


def test_operation_cancelled_maps_to_130(home, capsys, monkeypatch):
    def cancelled(_args):
        raise OperationCancelled("stopped")

    monkeypatch.setattr(cli, "cmd_status", cancelled)
    code, _, err = run(capsys, "status")
    assert code == EXIT_CANCELLED and err.strip() == "cancelled: stopped"


# -- progress printer ------------------------------------------------------------------------


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_progress_plain_lines_are_throttled_per_phase():
    stream, clock = io.StringIO(), FakeClock()
    printer = ProgressPrinter(stream, tty=False, interval=2.0, clock=clock)
    printer({"phase": "scan", "done": 0, "total": None, "message": "Looking"})
    printer({"phase": "read", "done": 1, "total": 4, "message": "Reading a.md"})
    clock.now = 1.0
    printer({"phase": "read", "done": 2, "total": 4, "message": "Reading b.md"})
    clock.now = 3.5
    printer({"phase": "read", "done": 3, "total": 4, "message": "Reading c.md"})
    printer({"phase": "done", "done": 4, "total": 4, "message": "finished"})
    printer.finish()
    assert stream.getvalue().splitlines() == [
        "Looking",
        "[1/4  25%] Reading a.md",
        "[3/4  75%] Reading c.md",
    ]


def test_progress_tty_rewrites_one_line_and_clears_it():
    stream, clock = io.StringIO(), FakeClock()
    printer = ProgressPrinter(stream, tty=True, clock=clock)
    printer({"phase": "read", "done": 1, "total": 2, "message": "Reading a-long-name.md"})
    clock.now = 1.0
    printer({"phase": "read", "done": 2, "total": 2, "message": "Reading b.md"})
    printer({"phase": "embed", "done": 2, "total": None})
    printer.finish()
    value = stream.getvalue()
    assert "\n" not in value
    assert value.count("\r") == 5  # three updates + finish() clearing the line
    assert "[2/2 100%] Reading b.md" in value and "Embedding" in value
    assert value.endswith("\r")


def test_progress_detects_tty_from_stream():
    class Tty(io.StringIO):
        def isatty(self):
            return True

    assert ProgressPrinter(Tty()).tty is True
    assert ProgressPrinter(io.StringIO()).tty is False


# -- serve -------------------------------------------------------------------------------------


@pytest.fixture
def fake_server(monkeypatch):
    calls = {}

    def create_app(index_path=None, *, reset_token=None, allowed_hosts=None,
                   home=None, allow_open=True):
        calls["create_app"] = {
            "index_path": index_path, "reset_token": reset_token,
            "allowed_hosts": allowed_hosts, "home": home, "allow_open": allow_open,
        }
        return "app"

    serve_module = types.ModuleType("onefind.serve")
    serve_module.create_app = create_app
    uvicorn = types.ModuleType("uvicorn")
    uvicorn.run = lambda app, **kwargs: calls.setdefault("run", (app, kwargs))
    monkeypatch.setitem(sys.modules, "onefind.serve", serve_module)
    monkeypatch.setitem(sys.modules, "uvicorn", uvicorn)
    return calls, serve_module


def test_serve_library_mode_without_db(home, capsys, fake_server):
    calls, _ = fake_server
    code, out, _ = run(capsys, "serve", "--no-open", "--port", "9999")
    assert code == EXIT_OK
    assert calls["create_app"]["index_path"] is None
    assert calls["create_app"]["home"] is None
    assert calls["create_app"]["allow_open"] is False
    assert calls["run"] == ("app", {"host": "127.0.0.1", "port": 9999, "log_level": "warning"})
    assert f"library: {home}" in out


def test_serve_with_db_and_home(home, tmp_path, capsys, fake_server):
    calls, _ = fake_server
    code, out, _ = run(capsys, "serve", "--db", "demo.db", "--home", tmp_path / "h")
    assert code == EXIT_OK
    assert calls["create_app"]["index_path"] == "demo.db"
    assert calls["create_app"]["home"] == str(tmp_path / "h")
    assert calls["create_app"]["allow_open"] is True
    assert "(db: demo.db)" in out


def test_serve_classic_call_works_with_old_create_app(home, capsys, fake_server):
    calls, serve_module = fake_server

    def old_create_app(index_path, *, reset_token=None, allowed_hosts=None):
        calls["old"] = index_path
        return "old-app"

    serve_module.create_app = old_create_app
    code, _, _ = run(capsys, "serve", "--db", "demo.db")
    assert code == EXIT_OK and calls["old"] == "demo.db"
    assert calls["run"][0] == "old-app"

    code, _, err = run(capsys, "serve")
    assert code == EXIT_USAGE and "needs --db" in err


def test_serve_still_refuses_remote_bind(home, capsys, fake_server):
    code, _, err = run(capsys, "serve", "--host", "0.0.0.0")
    assert code == EXIT_USAGE and "refusing to expose" in err


# -- parser ---------------------------------------------------------------------------------------


def test_parser_shapes():
    parser = cli.build_parser()
    args = parser.parse_args(["index", "notes", "--exclude", "a", "--exclude", "b/*"])
    assert args.exclude == ["a", "b/*"] and args.db is None and not args.prune
    args = parser.parse_args(["search", "q", "--tag", "x", "--tag", "y", "--type", "md"])
    assert args.tag == ["x", "y"] and args.type == ["md"] and args.db is None
    args = parser.parse_args(["restore", "b.db", "--as", "Name"])
    assert args.as_name == "Name"
    with pytest.raises(SystemExit):
        parser.parse_args(["restore", "b.db"])
    with pytest.raises(SystemExit):
        parser.parse_args(["collections", "explode"])


def test_index_embed_and_hybrid_explain_with_real_model(home, corpus, capsys):
    pytest.importorskip("sentence_transformers")
    out, _ = indexed(capsys, corpus, "--embed", "--quiet")
    assert "4 documents total, 4 vectors" in out
    assert "vectors    : 4 of 4 notes (" in out
    code, out, _ = run(capsys, "search", "plants in the sun", "--explain", "--k", "2")
    assert code == EXIT_OK
    assert " 1. " in out and "garden.txt" in out.splitlines()[1]
    assert "meaning" in out.lower()
    code, out, _ = run(capsys, "status")
    assert "searchable by keyword and meaning" in out


def test_cli_never_touches_real_home(home):
    assert os.environ["ONEFIND_HOME"] == str(home)
