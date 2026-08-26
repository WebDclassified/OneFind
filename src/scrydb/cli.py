"""scrydb command-line interface.

Exit codes (docs/03-app-flow.md):
0 success · 2 usage/validation · 3 environment · 4 data error
"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .envcheck import collect_check

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_ENV = 3


def _print_human(report: dict) -> None:
    def flag(ok: bool) -> str:
        return "OK  " if ok else "FAIL"

    print("scrydb check")
    print(f"  python     : {report['python']}")
    print(f"  sqlite     : {report['sqlite']['version']}")
    print(f"  fts5       : {flag(report['sqlite']['fts5']['ok'])} {report['sqlite']['fts5']['detail']}")
    print(f"  sqlite-vec : {flag(report['vector']['ok'])} {report['vector']['detail']}")

    model = report["model"]
    line = f"  model      : {model['status']}"
    if model.get("dimension"):
        line += f" (dim={model['dimension']})"
    if model.get("fix"):
        line += f" [{model['fix']}]"
    if model.get("error"):
        line += f" error={model['error']}"
    print(line)
    print(f"  overall    : {'PASS' if report['ok'] else 'FAIL'}")


def cmd_check(args: argparse.Namespace) -> int:
    report = collect_check(load_model=args.full)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_human(report)
    return EXIT_OK if report["ok"] else EXIT_ENV


def _not_yet(phase_hint: str) -> int:
    print(f"not implemented yet - planned in docs/06-engineering-plan.md ({phase_hint})")
    return EXIT_USAGE


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scrydb", description=__doc__)
    parser.add_argument("--version", action="version", version=f"scrydb {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="verify SQLite FTS5 + sqlite-vec + model stack")
    p_check.add_argument("--json", action="store_true", help="machine-readable report")
    p_check.add_argument(
        "--full", action="store_true", help="also download/load the embedding model (network)"
    )
    p_check.set_defaults(func=cmd_check)

    for name, hint in (
        ("index", "task T-02"),
        ("search", "tasks T-03/T-04/T-06"),
        ("eval", "task T-07"),
        ("serve", "task T-11"),
    ):
        p = sub.add_parser(name, help=f"(planned) see {hint}")
        p.set_defaults(func=lambda _args, hint=hint: _not_yet(hint))

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
