"""Evaluation harness (T-07/T-08): run all retrieval configurations over a
BEIR dataset and emit a markdown report with ranx metrics + latency.

Effectiveness is deterministic given fixed data/model; latency is wall-clock
per query, warm index, INCLUDING query encoding (documented definition).
"""

from __future__ import annotations

import datetime
import statistics
import time
from pathlib import Path

from . import datasets as beir
from .embed import DEFAULT_MODEL, SentenceEmbedder
from .errors import DataError, UsageError
from .ingest import Document
from .search import search as run_search
from .store import Index

METRICS = ["ndcg@10", "map", "mrr", "precision@10"]

CONFIGS = [  # (mode, precision, rerank)
    ("lexical", None, False),
    ("semantic", "float", False),
    ("semantic", "int8", False),
    ("semantic", "binary", False),
    ("hybrid", "float", False),
    ("hybrid", "int8", False),
    ("hybrid", "binary", False),
    ("hybrid", "float", True),
]


def _config_label(mode: str, precision: str | None, rerank: bool) -> str:
    base = mode if precision is None else f"{mode}({precision})"
    return f"{base}+rerank" if rerank else base


def _run_configuration(index: Index, queries: dict[str, str], mode: str,
                       precision: str | None, rerank: bool, k: int):
    """Returns (run dict qid->docid->score, latency seconds list)."""
    from .embed import compose_embed_text  # noqa: F401 - parity with ingest

    run: dict[str, dict[str, float]] = {}
    latencies: list[float] = []
    for qid, text in queries.items():
        start = time.perf_counter()
        hits = run_search(index, text, mode=mode, k=k,
                          precision=precision or "float", rerank=rerank)
        latencies.append(time.perf_counter() - start)
        run[qid] = {hit.doc_id: float(hit.score) for hit in hits}
    return run, latencies


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[idx] * 1000.0  # -> ms


def run_eval(source: str, db: str | Path, k: int = 10,
             max_docs: int | None = None, limit_queries: int | None = None,
             model_name: str = DEFAULT_MODEL,
             out_dir: str | Path = "benchmarks/reports") -> Path:
    """Full pipeline for one dataset; writes a markdown report, returns path."""
    try:
        import ranx
    except ImportError as exc:
        raise UsageError('evaluation needs the [eval] extra - pip install -e ".[eval]"') from exc

    model_name = model_name or DEFAULT_MODEL  # CLI may pass None explicitly

    folder = beir.load_local_or_registry(source)
    corpus_full = beir.load_corpus(folder)
    queries_all = beir.load_queries(folder)
    qrels_dict = beir.load_qrels(folder)

    if max_docs is not None:
        corpus_full = dict(list(corpus_full.items())[:max_docs])
    # keep only queries that have judgments (BEIR convention) + optional cap
    queries = {qid: t for qid, t in queries_all.items() if qid in qrels_dict}
    if limit_queries is not None:
        queries = dict(list(queries.items())[:limit_queries])
    if not queries:
        raise DataError("no judged queries to evaluate")

    stamp = datetime.date.today().isoformat()
    with Index.open(db) as index:
        print(f"loading embedding model: {model_name} ...")
        embedder = SentenceEmbedder(model_name)
        index.attach_embedder(embedder)

        docs = [
            Document(doc_id=did, title=meta["title"], body=meta["text"],
                     source=f"{source}:{did}")
            for did, meta in corpus_full.items()
        ]
        print(f"indexing {len(docs)} documents ...")
        index.add_documents(docs)

        results = []
        for mode, precision, rerank in CONFIGS:
            label = _config_label(mode, precision, rerank)
            run, latencies = _run_configuration(index, queries, mode, precision, rerank, k)
            metrics = ranx.evaluate(
                ranx.Qrels.from_dict(qrels_dict),
                ranx.Run.from_dict(run),
                METRICS,
            )
            results.append({
                "label": label,
                "metrics": metrics,
                "p50_ms": _percentile(latencies, 0.50),
                "p95_ms": _percentile(latencies, 0.95),
            })
            print(f"  {label:<22} nDCG@10={metrics['ndcg@10']:.4f} "
                  f"p95={results[-1]['p95_ms']:.1f}ms")

        stats = index.stats()

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    report = out_path / f"eval-{Path(source).name}-{stamp}.md"
    _write_report(report, source=Path(source).name, db=str(db), stats=stats,
                  model=model_name, num_docs=len(corpus_full), num_queries=len(queries),
                  k=k, results=results)
    return report


# ---- T-10: alpha sweep over linear fusion (vs RRF baseline) -----------------


def run_alpha_sweep(source: str, db: str | Path, alphas: list[float] | None = None,
                    k: int = 10, model_name: str = DEFAULT_MODEL,
                    precision: str = "float",
                    out_dir: str | Path = "benchmarks/reports") -> Path:
    """Reuse an already-indexed database; sweep alpha in linear fusion.

    Compares to the RRF hybrid baseline by re-evaluating once with the
    existing hybrid(float) configuration.
    """
    try:
        import ranx
    except ImportError as exc:
        raise UsageError('evaluation needs the [eval] extra - pip install -e ".[eval]"') from exc

    model_name = model_name or DEFAULT_MODEL
    alphas = list(alphas) if alphas is not None else [i / 10.0 for i in range(11)]
    if not 0.0 <= min(alphas) <= 1.0 or not 0.0 <= max(alphas) <= 1.0:
        raise UsageError("all alphas must be in [0, 1]")

    folder = beir.load_local_or_registry(source)
    queries_all = beir.load_queries(folder)
    qrels_dict = beir.load_qrels(folder)
    queries = {qid: t for qid, t in queries_all.items() if qid in qrels_dict}
    if not queries:
        raise DataError("no judged queries to evaluate")

    rows: list[dict] = []
    with Index.open(db) as index:
        from .embed import SentenceEmbedder
        print(f"loading embedding model: {model_name} ...")
        index.attach_embedder(SentenceEmbedder(model_name))

        for alpha in alphas:
            per_query_run: dict[str, dict[str, float]] = {}
            for qid, text in queries.items():
                hits = run_search(
                    index, text, mode="hybrid", k=k, precision=precision,
                    fusion="linear", alpha=alpha,
                )
                per_query_run[qid] = {h.doc_id: float(h.score) for h in hits}
            metrics = ranx.evaluate(
                ranx.Qrels.from_dict(qrels_dict),
                ranx.Run.from_dict(per_query_run),
                METRICS,
            )
            rows.append({"alpha": alpha, "label": f"linear(α={alpha:.1f})",
                         "metrics": metrics})
            print(f"  linear(alpha={alpha:.1f}) nDCG@10={metrics['ndcg@10']:.4f}")

        # RRF baseline computed against the same db
        per_query_run = {
            qid: {h.doc_id: float(h.score) for h in run_search(
                index, text, mode="hybrid", k=k, precision=precision, fusion="rrf"
            )}
            for qid, text in queries.items()
        }
        rrf_metrics = ranx.evaluate(
            ranx.Qrels.from_dict(qrels_dict),
            ranx.Run.from_dict(per_query_run),
            METRICS,
        )
        print(f"  RRF baseline         nDCG@10={rrf_metrics['ndcg@10']:.4f}")

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    report = out_path / f"alpha-sweep-{Path(source).name}-{datetime.date.today().isoformat()}.md"
    _write_sweep(report, source=Path(source).name, model=model_name, precision=precision,
                 k=k, num_queries=len(queries), rows=rows, rrf=rrf_metrics)
    return report


def _write_sweep(path: Path, **ctx) -> None:
    rows = ctx["rows"]
    rrf = ctx["rrf"]
    best = max(rows, key=lambda r: r["metrics"]["ndcg@10"])
    chart_lines = []
    width = 40
    for r in rows + [{"label": "RRF baseline", "metrics": rrf}]:
        bar = "=" * max(1, int(round(r["metrics"]["ndcg@10"] * width)))
        chart_lines.append(f"{r['label']:<18} | {bar:<{width}} {r['metrics']['ndcg@10']:.4f}")

    lines = [
        f"# Alpha sweep — {ctx['source']} ({ctx['precision']} precision)",
        "",
        f"- Date: {datetime.date.today().isoformat()}",
        f"- Model: `{ctx['model']}` · judged queries: {ctx['num_queries']} · k: {ctx['k']}",
        f"- Linear fusion min-max-normalizes each leg's scores to [0, 1] within the",
        f"  retrieved k, then blends: alpha * semantic + (1 - alpha) * lexical.",
        f"- RRF baseline = the existing `hybrid({ctx['precision']})` config.",
        "",
        "## nDCG@10 by alpha",
        "",
        "```",
        *chart_lines,
        "```",
        "",
        f"Best alpha: **{best['alpha']:.1f}** (nDCG@10 = {best['metrics']['ndcg@10']:.4f});",
        f"RRF baseline nDCG@10 = {rrf['ndcg@10']:.4f}.",
        "",
        "## Full metrics",
        "",
        "| Configuration | nDCG@10 | AP | RR | P@10 |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        m = r["metrics"]
        lines.append(
            f"| {r['label']} | {m['ndcg@10']:.4f} | {m['map']:.4f} | "
            f"{m['mrr']:.4f} | {m['precision@10']:.4f} |"
        )
    m = rrf
    lines.append(
        f"| RRF baseline | {m['ndcg@10']:.4f} | {m['map']:.4f} | "
        f"{m['mrr']:.4f} | {m['precision@10']:.4f} |"
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"report written: {path}")


def _write_report(path: Path, **ctx) -> None:
    lines = [
        f"# Evaluation — {ctx['source']}",
        "",
        f"- Date: {datetime.date.today().isoformat()}",
        f"- Model: `{ctx['model']}` · docs: {ctx['num_docs']} · judged queries: "
        f"{ctx['num_queries']} · k: {ctx['k']}",
        f"- DB: `{ctx['db']}` ({ctx['stats'].get('vectors', '?')} vectors)",
        "- Latency: warm index, per query, includes query encoding (ms).",
        "",
        "| Configuration | nDCG@10 | AP | RR | P@10 | p50 ms | p95 ms |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in ctx["results"]:
        m = row["metrics"]
        lines.append(
            f"| {row['label']} | {m['ndcg@10']:.4f} | {m['map']:.4f} | "
            f"{m['mrr']:.4f} | {m['precision@10']:.4f} | {row['p50_ms']:.1f} | "
            f"{row['p95_ms']:.1f} |"
        )
    lines += [
        "",
        "## Notes",
        "- int8/binary precisions are computed application-side over the stored "
        "float vectors (ADR-7); see docs/02.",
        "- Hybrid uses RRF k=60, leg depth = k (ADR-8). `+rerank` rescores fused "
        "candidates by full-precision cosine.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"report written: {path}")
