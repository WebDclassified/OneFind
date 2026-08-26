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
from .errors import ScrydbError, UsageError

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


def cmd_index(args: argparse.Namespace) -> int:
    from .ingest import ingest_path
    from .store import Index

    with Index.open(args.db) as index:
        if args.embed:
            from .embed import DEFAULT_MODEL, SentenceEmbedder

            model_name = args.model or index.get_meta("model_name") or DEFAULT_MODEL
            print(f"loading embedding model: {model_name} ...")
            index.attach_embedder(SentenceEmbedder(model_name))
        stats = ingest_path(index, args.path)
        n_vectors = index.count("vectors") if args.embed else None
        bound_model = index.get_meta("model_name") if args.embed else None

    vectors = f", {n_vectors} vectors" if n_vectors is not None else ""
    extra = (
        f" [model: {bound_model}; float storage, app-side int8/bit]"
        if bound_model
        else " [lexical only - add --embed for semantic search]"
    )
    print(
        f"indexed {stats['files_indexed']} files "
        f"({stats['total_documents']} documents total{vectors}) -> {args.db}{extra}"
    )
    return EXIT_OK


def _print_hits(hits) -> None:
    for rank, hit in enumerate(hits, start=1):
        snippet_one_line = " ".join(hit.snippet.split()) if hit.snippet else ""
        print(f"{rank:>2}. [{hit.score:.4f}] {hit.doc_id} - {hit.title}")
        if snippet_one_line:
            print(f"    {snippet_one_line[:120]}")
    if not hits:
        print("no results")


def cmd_search(args: argparse.Namespace) -> int:
    from .search import search as run_search
    from .store import Index

    if not 1 <= args.k <= 50:
        raise UsageError("--k must be within 1..50")

    with Index.open(args.db) as index:
        if args.mode == "semantic":
            model_name = index.get_meta("model_name")
            if model_name is None:
                raise UsageError(
                    "this index has no embeddings; rebuild with 'scrydb index --embed'"
                )
            print(f"loading embedding model: {model_name} ...")
            from .embed import SentenceEmbedder

            index.attach_embedder(SentenceEmbedder(model_name))
        hits = run_search(
            index, args.query, mode=args.mode, k=args.k, precision=args.precision
        )
    _print_hits(hits)
    return EXIT_OK


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

    p_index = sub.add_parser("index", help="index a folder of .txt/.md files into a database")
    p_index.add_argument("path", help="corpus folder")
    p_index.add_argument("--db", default="scrydb.db", help="target SQLite file (default scrydb.db)")
    p_index.add_argument(
        "--embed",
        action="store_true",
        help="also compute embeddings (float+int8+bit vectors; needs [model] extra)",
    )
    p_index.add_argument("--model", default=None, help="embedding model name (default MiniLM)")
    p_index.set_defaults(func=cmd_index)

    p_search = sub.add_parser("search", help="query an index")
    p_search.add_argument("query")
    p_search.add_argument("--db", default="scrydb.db")
    p_search.add_argument(
        "--mode", choices=["lexical", "semantic", "hybrid"], default="hybrid"
    )
    p_search.add_argument(
        "--precision", choices=["float", "int8", "binary"], default="float",
        help="vector storage precision for semantic/hybrid search",
    )
    p_search.add_argument("--k", type=int, default=10, help="number of results (1..50)")
    p_search.set_defaults(func=cmd_search)

    for name, hint in (("eval", "task T-07"), ("serve", "task T-11")):
        p = sub.add_parser(name, help=f"(planned) see {hint}")
        p.set_defaults(func=lambda _a, hint=hint: _not_yet(hint))

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ScrydbError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    sys.exit(main())
