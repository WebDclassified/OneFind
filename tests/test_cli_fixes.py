"""Regression tests for CLI review fixes (serve Host checks, fix hints, model fallback).

Model-free: lexical-only indexes in temporary folders, ``ONEFIND_HOME`` on a
temporary library, and the working directory moved to a temporary folder.
"""

from __future__ import annotations

import sys
import types
from argparse import Namespace
from pathlib import Path

import pytest

from onefind import cli
from onefind.cli import EXIT_DATA, EXIT_ENV, EXIT_OK, EXIT_USAGE, main
from onefind.library import Library
from onefind.store import Index

NOTES = {
    "rollback.md": "# Rollback plan\nRollback the deployment when the canary error rate rises.",
    "garden.txt": "Tomatoes need full sun and regular watering in summer.",
    "projects/alpha.md": "# Alpha launch\nThe alpha launch checklist covers rollback owners.",
    "projects/drafts/idea.md": "# Draft idea\nA rough rollback idea for later.",
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
    return root


def run(capsys, *argv):
    code = main([str(arg) for arg in argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def indexed(capsys, corpus, *extra):
    code, out, err = run(capsys, "index", corpus, "--quiet", *extra)
    assert code == EXIT_OK, err
    return out, err


def _fake_vectors(db: str | Path) -> None:
    """Mark a lexical index as having vectors of a (fake) model."""
    with Index.open(db) as index:
        index.set_meta("model_name", "fake/model")
        index.set_meta("model_dim", "4")


# -- serve: Host header checks (findings 1 and 2) ---------------------------------------


@pytest.fixture
def fake_server(monkeypatch):
    calls = {}

    def create_app(index_path=None, *, reset_token=None, allowed_hosts=None,
                   home=None, allow_open=True):
        calls["create_app"] = {
            "index_path": index_path, "allowed_hosts": allowed_hosts,
            "home": home, "allow_open": allow_open,
        }
        return "app"

    serve_module = types.ModuleType("onefind.serve")
    serve_module.create_app = create_app
    uvicorn = types.ModuleType("uvicorn")
    uvicorn.run = lambda app, **kwargs: calls.setdefault("run", (app, kwargs))
    monkeypatch.setitem(sys.modules, "onefind.serve", serve_module)
    monkeypatch.setitem(sys.modules, "uvicorn", uvicorn)
    return calls


def test_ipv6_loopback_bind_accepts_bracketed_host(home, capsys, fake_server):
    code, out, _ = run(capsys, "serve", "--host", "::1", "--port", "9999")
    assert code == EXIT_OK
    hosts = fake_server["create_app"]["allowed_hosts"]
    assert "[::1]" in hosts and "::1" in hosts and "*" not in hosts
    assert fake_server["create_app"]["allow_open"] is True
    assert "http://[::1]:9999" in out


def test_real_app_serves_ipv6_loopback_host_header(home, tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from onefind.serve import create_app

    hosts = cli._allowed_hosts(Namespace(host="::1", allowed_host=[]))
    app = create_app(None, home=tmp_path / "lib", allowed_hosts=hosts, allow_open=False)
    with TestClient(app) as client:
        assert client.get("/health", headers={"host": "[::1]:8080"}).status_code == 200
        assert client.get("/health", headers={"host": "127.0.0.1:8080"}).status_code == 200
        assert client.get("/health", headers={"host": "evil.example"}).status_code == 400


def test_remote_bind_never_disables_host_check(home, capsys, fake_server):
    code, _, err = run(
        capsys, "serve", "--host", "192.168.1.5", "--allow-remote",
        "--allowed-host", "notes.lan", "--allowed-host", "fe80::1",
    )
    assert code == EXIT_OK, err
    hosts = fake_server["create_app"]["allowed_hosts"]
    assert "*" not in hosts
    for expected in ("localhost", "127.0.0.1", "[::1]", "192.168.1.5", "notes.lan",
                     "fe80::1", "[fe80::1]"):
        assert expected in hosts
    # remote mode never launches files on this machine
    assert fake_server["create_app"]["allow_open"] is False
    assert "opening files from the web app is off" in err


def test_all_interfaces_bind_needs_allowed_host(home, capsys, fake_server):
    code, _, err = run(capsys, "serve", "--host", "0.0.0.0", "--allow-remote")
    assert code == EXIT_USAGE and "--allowed-host" in err
    assert "create_app" not in fake_server

    code, _, _ = run(capsys, "serve", "--host", "0.0.0.0", "--allow-remote",
                     "--allowed-host", "box.lan:8080")
    assert code == EXIT_OK
    hosts = fake_server["create_app"]["allowed_hosts"]
    assert "box.lan" in hosts and "0.0.0.0" not in hosts and "*" not in hosts


def test_wildcard_host_only_when_explicit(home, capsys, fake_server):
    code, _, _ = run(capsys, "serve", "--host", "0.0.0.0", "--allow-remote",
                     "--allowed-host", "*")
    assert code == EXIT_OK
    assert "*" in fake_server["create_app"]["allowed_hosts"]


def test_real_app_remote_list_rejects_rebound_host(home, tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from onefind.serve import create_app

    hosts = cli._allowed_hosts(
        Namespace(host="0.0.0.0", allowed_host=["notes.lan"])
    )
    app = create_app(None, home=tmp_path / "lib", allowed_hosts=hosts, allow_open=False)
    with TestClient(app) as client:
        assert client.get("/health", headers={"host": "notes.lan:8080"}).status_code == 200
        assert client.get("/health", headers={"host": "attacker.example"}).status_code == 400


# -- remove: the re-index hint keeps saved exclusions (finding 3) ------------------------


def test_remove_hint_repeats_saved_excludes_and_names_folder(home, corpus, capsys):
    indexed(capsys, corpus, "--collection", "Notes", "--exclude", "projects/drafts")
    code, out, _ = run(capsys, "remove", "garden.txt", "--collection", "Notes")
    assert code == EXIT_OK
    assert "--exclude replaces the folder's saved patterns" in out
    command = next(line.strip() for line in out.splitlines() if "onefind index" in line)
    assert str(corpus) in command
    assert "--collection Notes" in command
    assert "--exclude projects/drafts --exclude PATTERN" in command

    # Following the hint keeps the old exclusion.
    argv = command.split()[1:]
    argv[argv.index("PATTERN")] = "garden.txt"
    code, _, err = run(capsys, *argv, "--quiet")
    assert code == EXIT_OK, err
    code, out, _ = run(capsys, "search", "rough", "--mode", "lexical",
                       "--collection", "Notes")
    assert "no results" in out


def test_exclude_help_says_patterns_replace():
    help_text = cli.build_parser()._subparsers._group_actions[0].choices["index"].format_help()
    assert "replace" in " ".join(help_text.split())


# -- search: keyword matching like the app (finding 4) ----------------------------------


def test_search_defaults_to_auto_matching_like_the_app(home, corpus, capsys):
    indexed(capsys, corpus)
    code, out, err = run(capsys, "search", "tomatoes zebra", "--mode", "lexical")
    assert code == EXIT_OK
    assert "garden.txt" in out
    assert "showing partial matches" in err

    code, out, _ = run(capsys, "search", "tomatoes zebra", "--mode", "lexical",
                       "--match", "all")
    assert code == EXIT_OK and "no results" in out


def test_search_passes_lexical_match(home, corpus, capsys, monkeypatch):
    indexed(capsys, corpus)
    seen = []
    import onefind.search as search_module

    original = search_module.search_detailed

    def spy(*args, **kwargs):
        seen.append(kwargs.get("lexical_match"))
        return original(*args, **kwargs)

    monkeypatch.setattr(search_module, "search_detailed", spy)
    run(capsys, "search", "rollback", "--mode", "lexical")
    run(capsys, "search", "rollback", "--mode", "lexical", "--match", "any")
    assert seen == ["auto", "any"]


def test_smoke_match_is_forwarded_only_when_supported(monkeypatch, capsys):
    import onefind.evaluate as evaluate

    captured = {}

    def fake_run_smoke(db, queries, k=5, model_name=None, *, lexical_match="all"):
        captured["lexical_match"] = lexical_match
        return {"cases": 0, "answerable": 0, "hit_at_3": 0.0, "mrr_at_k": 0.0,
                "no_answer_accuracy": 0.0, "rows": []}

    monkeypatch.setattr(evaluate, "run_smoke", fake_run_smoke)
    assert main(["smoke", "--db", "x.db", "--match", "auto"]) == EXIT_OK
    assert captured["lexical_match"] == "auto"

    monkeypatch.setattr(evaluate, "run_smoke", lambda db, queries, k=5, model_name=None: {})
    assert main(["smoke", "--db", "x.db", "--match", "auto"]) == EXIT_USAGE
    capsys.readouterr()


# -- model missing: keyword fallback and exit 3 (finding 5) ------------------------------


def test_hybrid_search_falls_back_to_keywords_without_model_stack(
    home, corpus, capsys, monkeypatch
):
    indexed(capsys, corpus)
    _fake_vectors("onefind.db")
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)

    code, out, err = run(capsys, "search", "tomatoes")
    assert code == EXIT_OK
    assert "garden.txt" in out
    assert "showing keyword results instead" in err
    assert "Traceback" not in err

    code, _, err = run(capsys, "search", "tomatoes", "--mode", "semantic")
    assert code == EXIT_ENV
    assert err.startswith("error: semantic search needs the model stack")


def test_uncached_model_is_env_error_not_traceback(home, corpus, capsys, monkeypatch):
    import onefind.embed as embed

    class Offline:
        def __init__(self, name):
            raise OSError("We couldn't connect to 'https://huggingface.co'")

    monkeypatch.setattr(embed, "SentenceEmbedder", Offline)
    code, _, err = run(capsys, "index", corpus, "--embed", "--model", "x/y",
                       "--collection", "C", "--quiet")
    assert code == EXIT_ENV
    assert "could not load embedding model 'x/y' (OSError)" in err
    # the collection this run created is not left behind empty
    assert [entry["name"] for entry in Library(home).list()] == []
    assert not list(home.rglob("*.db"))

    indexed(capsys, corpus, "--collection", "Kept")
    _fake_vectors(Library(home).get("Kept")["db"])
    code, _, err = run(capsys, "index", corpus, "--embed", "--collection", "Kept", "--quiet")
    assert code == EXIT_ENV
    # an existing collection is never removed
    assert [entry["name"] for entry in Library(home).list()] == ["Kept"]

    code, out, err = run(capsys, "search", "tomatoes", "--collection", "Kept")
    assert code == EXIT_OK and "garden.txt" in out
    assert "showing keyword results instead" in err


def test_existing_empty_collection_is_not_removed_on_model_failure(
    home, corpus, capsys, monkeypatch
):
    import onefind.embed as embed

    def boom(name):
        raise OSError("offline")

    run(capsys, "collections", "create", "Empty")
    monkeypatch.setattr(embed, "SentenceEmbedder", boom)
    code, _, _ = run(capsys, "index", corpus, "--embed", "--collection", "empty", "--quiet")
    assert code == EXIT_ENV
    assert [entry["name"] for entry in Library(home).list()] == ["Empty"]


# -- status/doctor suggestions name their collection (finding 6) ---------------------------


def test_status_refresh_command_names_the_collection(home, corpus, capsys, tmp_path):
    indexed(capsys, corpus, "--collection", "My Notes")
    (corpus / "new.md").write_text("# New\nfresh zebra note", encoding="utf-8")
    code, out, _ = run(capsys, "status")
    assert code == EXIT_OK
    assert f'refresh: onefind index {corpus} --collection "My Notes"' in out

    # Following it from another folder refreshes the collection, not ./onefind.db.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    import os

    os.chdir(elsewhere)
    code, _, err = run(capsys, "index", corpus, "--collection", "My Notes", "--quiet")
    assert code == EXIT_OK, err
    assert not (elsewhere / "onefind.db").exists()
    code, out, _ = run(capsys, "status")
    assert "up to date" in out


def test_status_without_collection_suggests_absolute_db(home, corpus, capsys):
    indexed(capsys, corpus)
    (corpus / "new.md").write_text("# New\nfresh", encoding="utf-8")
    code, out, _ = run(capsys, "status")
    assert code == EXIT_OK
    assert f"--db {Path('onefind.db').absolute()}" in out


def test_doctor_and_status_fix_name_inactive_collection(home, corpus, capsys):
    indexed(capsys, corpus, "--collection", "A")
    indexed(capsys, corpus, "--collection", "X")
    assert Library(home).active()["name"] == "A"
    with Index.open(Library(home).get("X")["db"]) as index:
        index.conn.execute("DELETE FROM fts WHERE doc_id = 'garden.txt'")
        index.conn.commit()
    code, out, _ = run(capsys, "status", "--collection", "X", "--no-scan")
    assert code == EXIT_OK
    assert "Fix: onefind doctor --repair --collection X" in out
    code, out, _ = run(capsys, "doctor", "--collection", "X")
    assert code == EXIT_DATA
    assert "fix what can be fixed with: onefind doctor --repair --collection X" in out


def test_health_summary_default_keeps_old_text(home, corpus, capsys):
    indexed(capsys, corpus)
    with Index.open("onefind.db") as index:
        state, sentence = cli._health_summary(index)
    assert state == "ready"
    assert sentence.endswith("Add meaning search with: onefind index FOLDER --embed")


# -- collections list leaves no -wal/-shm (finding 7) -------------------------------------


def test_collections_list_leaves_no_wal_files(home, corpus, capsys):
    indexed(capsys, corpus, "--collection", "a")
    db = Path(Library(home).get("a")["db"])
    assert not Path(f"{db}-wal").exists()
    code, out, _ = run(capsys, "collections", "list")
    assert code == EXIT_OK and "a" in out
    assert not Path(f"{db}-wal").exists()
    assert not Path(f"{db}-shm").exists()
    assert cli._document_count(db) == 4
    assert not Path(f"{db}-shm").exists()


def test_document_count_falls_back_to_read_only(home, tmp_path, monkeypatch):
    import sqlite3

    db = tmp_path / "ro.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE documents (doc_id TEXT)")
    conn.execute("INSERT INTO documents VALUES ('a')")
    conn.commit()
    conn.close()
    real_connect = sqlite3.connect
    modes = []

    def fake_connect(target, *args, **kwargs):
        modes.append(str(target).rsplit("mode=", 1)[-1])
        if str(target).endswith("mode=rw"):
            raise sqlite3.OperationalError("attempt to write a readonly database")
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(cli.sqlite3, "connect", fake_connect)
    assert cli._document_count(db) == 1
    assert modes == ["rw", "ro"]
    assert cli._document_count(tmp_path / "missing.db") is None
