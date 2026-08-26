"""Tests for T-00 environment verification."""

import json

import pytest

from scrydb import envcheck
from scrydb.cli import EXIT_OK, EXIT_USAGE, main


def test_fts5_detected_in_bundled_sqlite():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    ok, detail = envcheck.fts5_available(conn)
    conn.close()
    assert ok, detail


def test_sqlite_vec_detected_when_installed():
    sqlite_vec = pytest.importorskip("sqlite_vec")
    assert sqlite_vec is not None
    import sqlite3

    conn = sqlite3.connect(":memory:")
    ok, detail = envcheck.vec_available(conn)
    conn.close()
    assert ok, detail


def test_collect_check_shape_and_pass():
    report = envcheck.collect_check()
    assert set(report) >= {"python", "sqlite", "vector", "model", "ok"}
    assert report["sqlite"]["version"]
    assert report["ok"] is True, report


def test_model_report_without_stack_is_graceful():
    info = envcheck.model_status(load_model=False)
    assert info["status"] in {"not_installed", "installed_not_loaded"}


def test_cli_check_exit_zero_with_json_output(capsys):
    code = main(["check", "--json"])
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert code == EXIT_OK
    assert parsed["ok"] is True


def test_planned_subcommands_are_explicit_stubs():
    for argv in (["index"], ["search"], ["eval"], ["serve"]):
        assert main(argv) == EXIT_USAGE
