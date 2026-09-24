"""Tests for T-00 environment verification."""

import argparse
import json

import pytest

from onefind import envcheck
from onefind.cli import EXIT_OK, EXIT_USAGE, main


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


def test_all_advertised_subcommands_are_real():
    from onefind.cli import build_parser

    subparsers = next(
        action for action in build_parser()._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    assert set(subparsers.choices) == {
        "check", "index", "search", "eval", "sweep-alpha", "smoke", "serve"
    }


def test_real_subcommands_require_their_arguments():
    # every subcommand takes arguments; missing args -> argparse exit 2
    import pytest

    for argv in (
        ["index"], ["search"], ["eval"], ["sweep-alpha"], ["smoke"], ["serve"]
    ):
        with pytest.raises(SystemExit) as excinfo:
            main(argv)
        assert excinfo.value.code == EXIT_USAGE
