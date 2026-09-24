"""OneFind command-line interface.

Exit codes (docs/03-app-flow.md):
0 success · 2 usage/validation · 3 environment · 4 data error
"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .envcheck import collect_check
from .errors import OneFindError, UsageError

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_ENV = 3


def _print_human(report: dict) -> None:
    def flag(ok: bool) -> str:
        return "OK  " if ok else "FAIL"

    print("onefind check")
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
    passed = report["ok"]
    if args.full and report["model"].get("status") == "error":
        passed = False
    return EXIT_OK if passed else EXIT_ENV


def cmd_eval(args: argparse.Namespace) -> int:
    from .evaluate import run_eval

    report = run_eval(
        args.dataset,
        db=args.db or f"data/{args.dataset}.db",
        k=args.k,
        max_docs=args.max_docs,
        limit_queries=args.limit_queries,
        model_name=args.model,
        out_dir=args.out,
    )
    print(f"done -> {report}")
    return EXIT_OK


def cmd_sweep_alpha(args: argparse.Namespace) -> int:
    from .evaluate import run_alpha_sweep

    alphas = (
        [float(x) for x in args.alphas.split(",")]
        if args.alphas
        else [i / 10.0 for i in range(11)]
    )
    report = run_alpha_sweep(
        args.dataset,
        db=args.db,
        alphas=alphas,
        k=args.k,
        model_name=args.model,
        precision=args.precision,
        out_dir=args.out,
    )
    print(f"done -> {report}")
    return EXIT_OK


def cmd_smoke(args: argparse.Namespace) -> int:
    from .evaluate import run_smoke

    result = run_smoke(
        args.db,
        args.queries,
        k=args.k,
        model_name=args.model,
    )
    print(f"smoke cases       : {result['cases']}")
    print(f"answerable cases  : {result['answerable']}")
    print(f"hit@3             : {result['hit_at_3']:.3f}")
    print(f"MRR@{args.k}          : {result['mrr_at_k']:.3f}")
    print(f"no-answer accuracy: {result['no_answer_accuracy']:.3f}")
    for row in result["rows"]:
        outcome = "—" if row["rank"] is None else str(row["rank"])
        print(f"  {row['mode']:<8} rank={outcome:>2}  {row['query']}")
    return EXIT_OK


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
        snippet_one_line = (
            " ".join(
                hit.snippet.replace("", "[")
                .replace("", "]")
                .replace("…", "...")
                .split()
            )
            if hit.snippet
            else ""
        )
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
        if args.mode in ("semantic", "hybrid"):
            model_name = index.get_meta("model_name")
            if model_name is None:
                raise UsageError(
                    "this index has no embeddings; rebuild with 'onefind index --embed'"
                )
            print(f"loading embedding model: {model_name} ...")
            from .embed import SentenceEmbedder

            index.attach_embedder(SentenceEmbedder(model_name))
        hits = run_search(
            index, args.query, mode=args.mode, k=args.k,
            precision=args.precision, rrf_k=args.rrf_k, rerank=args.rerank,
            fusion=args.fusion, alpha=args.alpha,
            candidate_depth=args.candidate_depth,
        )
    _print_hits(hits)
    return EXIT_OK


def cmd_serve(args: argparse.Namespace) -> int:
    loopback_hosts = {"127.0.0.1", "localhost", "::1"}
    if args.host not in loopback_hosts and not args.allow_remote:
        raise UsageError(
            f"refusing to expose the unauthenticated demo on {args.host}; "
            "use --allow-remote only behind a trusted firewall or reverse proxy"
        )
    try:
        import uvicorn
    except ImportError as exc:
        raise UsageError('serve needs the [serve] extra - pip install -e ".[serve]"') from exc

    from .serve import create_app

    allowed_hosts = (
        ["localhost", "127.0.0.1", "::1"]
        if args.host in {"localhost", "127.0.0.1", "::1"}
        else ["*"]
    )
    app = create_app(
        args.db,
        reset_token=args.reset_token,
        allowed_hosts=allowed_hosts,
    )
    print(f"onefind serving on http://{args.host}:{args.port} (db: {args.db})")
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return EXIT_OK


def _not_yet(phase_hint: str) -> int:
    print(f"not implemented yet - planned in docs/06-engineering-plan.md ({phase_hint})")
    return EXIT_USAGE


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="onefind", description=__doc__)
    parser.add_argument("--version", action="version", version=f"onefind {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="verify SQLite FTS5 + sqlite-vec + model stack")
    p_check.add_argument("--json", action="store_true", help="machine-readable report")
    p_check.add_argument(
        "--full", action="store_true", help="also download/load the embedding model (network)"
    )
    p_check.set_defaults(func=cmd_check)

    p_index = sub.add_parser("index", help="index a folder of .txt/.md files into a database")
    p_index.add_argument("path", help="corpus folder")
    p_index.add_argument("--db", default="onefind.db", help="target SQLite file (default onefind.db)")
    p_index.add_argument(
        "--embed",
        action="store_true",
        help="also compute embeddings (float+int8+bit vectors; needs [model] extra)",
    )
    p_index.add_argument("--model", default=None, help="embedding model name (default MiniLM)")
    p_index.set_defaults(func=cmd_index)

    p_search = sub.add_parser("search", help="query an index")
    p_search.add_argument("query")
    p_search.add_argument("--db", default="onefind.db")
    p_search.add_argument(
        "--mode", choices=["lexical", "semantic", "hybrid"], default="hybrid"
    )
    p_search.add_argument(
        "--precision", choices=["float", "int8", "binary"], default="float",
        help="vector storage precision for semantic/hybrid search",
    )
    p_search.add_argument(
        "--rrf-k", type=int, default=60, dest="rrf_k",
        help="Reciprocal Rank Fusion constant (default 60)",
    )
    p_search.add_argument(
        "--rerank", action="store_true",
        help="rescore hybrid candidates by full-precision cosine",
    )
    p_search.add_argument(
        "--candidate-depth", type=int, default=50, dest="candidate_depth",
        help="per-leg candidate depth used when --rerank is enabled (default 50)",
    )
    p_search.add_argument(
        "--fusion", choices=["rrf", "linear"], default="rrf",
        help="hybrid fusion strategy (default rrf)",
    )
    p_search.add_argument(
        "--alpha", type=float, default=0.5,
        help="linear-fusion weight on semantic (0=lexical only, 1=semantic only)",
    )
    p_search.add_argument("--k", type=int, default=10, help="number of results (1..50)")
    p_search.set_defaults(func=cmd_search)

    p_eval = sub.add_parser("eval", help="run all retrieval configurations on a BEIR dataset")
    p_eval.add_argument(
        "dataset",
        help="registry name (scifact, nfcorpus) or a local folder with "
             "corpus/queries/qrels files",
    )
    p_eval.add_argument("--db", default=None, help="index file (default data/<name>.db)")
    p_eval.add_argument("--k", type=int, default=10)
    p_eval.add_argument("--max-docs", type=int, default=None, dest="max_docs")
    p_eval.add_argument("--limit-queries", type=int, default=None, dest="limit_queries")
    p_eval.add_argument("--model", default=None, help="embedding model override")
    p_eval.add_argument("--out", default="benchmarks/reports")
    p_eval.set_defaults(func=cmd_eval)

    p_sweep = sub.add_parser(
        "sweep-alpha",
        help="sweep alpha over linear hybrid fusion on an existing index",
    )
    p_sweep.add_argument("dataset", help="registry name (scifact, nfcorpus) or local folder")
    p_sweep.add_argument("--db", required=True, help="path to an already-built index")
    p_sweep.add_argument("--alphas", default=None, help="comma-separated alphas (default 0,0.1..1)")
    p_sweep.add_argument("--k", type=int, default=10)
    p_sweep.add_argument(
        "--precision", choices=["float", "int8", "binary"], default="float"
    )
    p_sweep.add_argument("--model", default=None)
    p_sweep.add_argument("--out", default="benchmarks/reports")
    p_sweep.set_defaults(func=cmd_sweep_alpha)

    p_smoke = sub.add_parser(
        "smoke", help="evaluate JSONL gold queries against an existing local index"
    )
    p_smoke.add_argument("--db", required=True, help="SQLite index to test")
    p_smoke.add_argument(
        "--queries", default="sample-data/queries.jsonl",
        help="JSONL cases with query, relevant, and optional mode fields",
    )
    p_smoke.add_argument("--k", type=int, default=5)
    p_smoke.add_argument("--model", default=None)
    p_smoke.set_defaults(func=cmd_smoke)

    p_serve = sub.add_parser("serve", help="run the local demo web app (T-11)")
    p_serve.add_argument("--db", required=True, help="SQLite index file to serve")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8080)
    p_serve.add_argument(
        "--allow-remote",
        action="store_true",
        help="permit a non-loopback bind for the unauthenticated demo",
    )
    p_serve.add_argument("--reset-token", default=None, dest="reset_token",
                         help="token required to POST /api/reset (random if omitted)")
    p_serve.add_argument("--log-level", default="warning", dest="log_level")
    p_serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except OneFindError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    sys.exit(main())
