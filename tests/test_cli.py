"""CLI defaults, safety gates, and user-facing failure contracts."""

from argparse import Namespace

import pytest

from onefind import cli
from onefind.errors import UsageError


def test_eval_uses_documented_dynamic_database_default(monkeypatch, tmp_path):
    captured = {}

    def fake_run_eval(source, **kwargs):
        captured["source"] = source
        captured.update(kwargs)
        return tmp_path / "report.md"

    monkeypatch.setattr("onefind.evaluate.run_eval", fake_run_eval)
    args = Namespace(
        dataset="scifact",
        db=None,
        k=10,
        max_docs=None,
        limit_queries=None,
        model=None,
        out="benchmarks/reports",
    )
    assert cli.cmd_eval(args) == cli.EXIT_OK
    assert captured["source"] == "scifact"
    assert captured["db"] == "data/scifact.db"


def test_remote_bind_requires_explicit_opt_in():
    args = Namespace(
        host="0.0.0.0",
        allow_remote=False,
        port=8080,
        db="demo.db",
        reset_token=None,
        log_level="warning",
    )
    with pytest.raises(UsageError, match="refusing to expose"):
        cli.cmd_serve(args)


def test_parser_exposes_free_smoke_command():
    args = cli.build_parser().parse_args(
        ["smoke", "--db", "demo.db", "--queries", "sample-data/queries.jsonl"]
    )
    assert args.func is cli.cmd_smoke
    assert args.k == 5


def test_candidate_depth_is_exposed_for_reranking():
    args = cli.build_parser().parse_args(
        ["search", "query", "--rerank", "--candidate-depth", "75"]
    )
    assert args.candidate_depth == 75
