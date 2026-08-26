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
