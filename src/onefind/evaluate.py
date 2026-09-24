"""Evaluation harness (T-07/T-08): run all retrieval configurations over a
BEIR dataset and emit a markdown report with ranx metrics + latency.

Effectiveness is deterministic given fixed data/model; latency is wall-clock
per query, warm index, INCLUDING query encoding (documented definition).
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
import uuid
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from . import datasets as beir
from .embed import DEFAULT_MODEL, SentenceEmbedder
from .errors import DataError, UsageError
from .ingest import Document
from .search import search as run_search
from .store import Index

METRICS = ["ndcg@10", "map@10", "mrr@10", "precision@10"]

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


def run_smoke(db: str | Path, queries_path: str | Path, k: int = 5,
               model_name: str = DEFAULT_MODEL) -> dict:
    """Evaluate the bundled or custom JSONL gold queries against an index."""
    if not 1 <= k <= 50:
        raise UsageError("k must be within 1..50")
    query_file = Path(queries_path)
    if not query_file.is_file():
        raise DataError(f"smoke query file not found: {query_file}")
    cases: list[dict] = []
    for line_number, line in enumerate(query_file.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DataError(f"invalid smoke JSON at line {line_number}: {exc.msg}") from exc
        query = str(row.get("query") or "").strip()
        relevant = [str(value) for value in row.get("relevant") or []]
        mode = str(row.get("mode") or "hybrid")
        if not query:
            raise DataError(f"empty smoke query at line {line_number}")
        if mode not in {"lexical", "semantic", "hybrid"}:
            raise DataError(f"invalid smoke mode at {line_number}: {mode}")
        cases.append({"query": query, "relevant": relevant, "mode": mode})

    if not cases:
        raise DataError("smoke query file contains no cases")

    rows: list[dict] = []
    with Index.open(db) as index:
        if any(case["mode"] != "lexical" for case in cases):
            index.attach_embedder(SentenceEmbedder(model_name or DEFAULT_MODEL))
        for case in cases:
            hits = run_search(
                index,
                case["query"],
                mode=case["mode"],
                k=k,
                precision="float",
            )
            ids = [hit.doc_id for hit in hits]
            if case["relevant"]:
                rank = next(
                    (position for position, doc_id in enumerate(ids, start=1)
                     if doc_id in case["relevant"]),
                    None,
                )
            else:
                rank = 1 if not ids else 0
            rows.append(
                {
                    "query": case["query"],
                    "mode": case["mode"],
                    "expected": case["relevant"],
                    "rank": rank,
                    "returned": ids,
                }
            )

    answerable = [row for row in rows if row["expected"]]
    no_answer = [row for row in rows if not row["expected"]]
    hit_at_k = sum(row["rank"] is not None and row["rank"] <= min(3, k) for row in answerable)
    mrr = sum(1.0 / row["rank"] for row in answerable if row["rank"] is not None)
    no_answer_ok = sum(row["rank"] == 1 for row in no_answer)
    return {
        "cases": len(rows),
        "answerable": len(answerable),
        "no_answer": len(no_answer),
        "hit_at_3": hit_at_k / len(answerable) if answerable else 0.0,
        "mrr_at_k": mrr / len(answerable) if answerable else 0.0,
        "no_answer_accuracy": no_answer_ok / len(no_answer) if no_answer else 1.0,
        "rows": rows,
    }


def _id_digest(values) -> str:
    payload = "\n".join(sorted(str(value) for value in values)).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _remove_sqlite_files(path: Path) -> None:
    path.unlink(missing_ok=True)
    Path(str(path) + "-wal").unlink(missing_ok=True)
    Path(str(path) + "-shm").unlink(missing_ok=True)


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
    if not 1 <= k <= 50:
        raise UsageError("k must be within 1..50")
    if max_docs is not None and max_docs < 1:
        raise UsageError("max-docs must be at least 1")
    if limit_queries is not None and limit_queries < 1:
        raise UsageError("limit-queries must be at least 1")

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
    qrels_dict = {qid: judgments for qid, judgments in qrels_dict.items() if qid in queries}

    stamp = datetime.date.today().isoformat()
    target = Path(db)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(f".{target.name}.{uuid.uuid4().hex}.building")
    _remove_sqlite_files(staging)

    try:
        with Index.open(staging) as index:
            print(f"loading embedding model: {model_name} ...")
            embedder = SentenceEmbedder(model_name)
            index.attach_embedder(embedder)

            docs = [
                Document(
                    doc_id=doc_id,
                    title=metadata["title"],
                    body=metadata["text"],
                    source=f"{source}:{doc_id}",
                )
                for doc_id, metadata in corpus_full.items()
            ]
            print(f"indexing {len(docs)} documents ...")
            index.add_documents(docs)

            results = []
            for mode, precision, rerank in CONFIGS:
                label = _config_label(mode, precision, rerank)
                run, latencies = _run_configuration(
                    index, queries, mode, precision, rerank, k
                )
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
                print(
                    f"  {label:<22} nDCG@10={metrics['ndcg@10']:.4f} "
                    f"p95={results[-1]['p95_ms']:.1f}ms"
                )

            manifest = {
                "schema_version": 2,
                "source": str(source),
                "dataset_files": beir.dataset_fingerprint(folder),
                "documents": len(corpus_full),
                "document_ids": _id_digest(corpus_full),
                "queries": len(queries),
                "query_ids": _id_digest(queries),
                "qrels": len(qrels_dict),
                "model": model_name,
                "model_revision": getattr(embedder, "revision", "unpinned"),
                "k": k,
                "metrics": METRICS,
                "configurations": [list(config) for config in CONFIGS],
            }
            index.set_meta("eval_manifest", json.dumps(manifest, sort_keys=True))
            stats = index.stats()

        # Publish only a fully built, manifest-bound database. Until this point,
        # failures leave any existing target untouched.
        Path(str(target) + "-wal").unlink(missing_ok=True)
        Path(str(target) + "-shm").unlink(missing_ok=True)
        os.replace(staging, target)
    finally:
        _remove_sqlite_files(staging)

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    report = out_path / f"eval-{Path(source).name}-{stamp}.md"
    _write_report(
        report,
        source=Path(source).name,
        db=str(target),
        stats=stats,
        model=model_name,
        num_docs=len(corpus_full),
        num_queries=len(queries),
        k=k,
        results=results,
        manifest=manifest,
    )
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
    if not 1 <= k <= 50:
        raise UsageError("k must be within 1..50")
    if precision not in {"float", "int8", "binary"}:
        raise UsageError("precision must be float, int8, or binary")
    alphas = list(alphas) if alphas is not None else [i / 10.0 for i in range(11)]
    if not alphas or not all(0.0 <= alpha <= 1.0 for alpha in alphas):
        raise UsageError("provide one or more alphas within [0, 1]")

    folder = beir.load_local_or_registry(source)
    corpus = beir.load_corpus(folder)
    queries_all = beir.load_queries(folder)
    qrels_dict = beir.load_qrels(folder)
    queries = {qid: t for qid, t in queries_all.items() if qid in qrels_dict}
    if not queries:
        raise DataError("no judged queries to evaluate")

    rows: list[dict] = []
    with Index.open(db) as index:
        manifest_raw = index.get_meta("eval_manifest")
        if not manifest_raw:
            raise DataError(
                "index has no evaluation manifest; rebuild it with 'onefind eval'"
            )
        manifest = json.loads(manifest_raw)
        current_files = beir.dataset_fingerprint(folder)
        if manifest.get("source") != str(source):
            raise DataError(f"index was built for source {manifest.get('source')!r}, not {source!r}")
        if manifest.get("dataset_files") != current_files:
            raise DataError("dataset files changed since this index was built")
        if manifest.get("document_ids") != _id_digest(corpus):
            raise DataError("index document selection does not match the current dataset")

        indexed_ids = {
            str(row[0]) for row in index.conn.execute("SELECT doc_id FROM documents")
        }
        expected_ids = set(corpus)
        if indexed_ids != expected_ids:
            missing = len(expected_ids - indexed_ids)
            extra = len(indexed_ids - expected_ids)
            raise DataError(
                f"index corpus does not match dataset '{source}' "
                f"({missing} missing, {extra} extra documents); rebuild it with "
                f"'onefind eval {source} --db {db}'"
            )

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
                 k=k, num_queries=len(queries), rows=rows, rrf=rrf_metrics,
                 manifest=manifest)
    return report


def _write_sweep(path: Path, **ctx) -> None:
    runtime = _runtime_metadata()
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
        f"- Model: `{ctx['model']}` @ `{ctx['manifest'].get('model_revision', 'unavailable')}` · "
        f"judged queries: {ctx['num_queries']} · k: {ctx['k']}",
        f"- Code: `{runtime['commit']}` · worktree: {'dirty' if runtime['dirty'] else 'clean'}",
        f"- Dataset corpus: `{ctx['manifest']['dataset_files']['corpus']}`",
        f"- Dataset queries: `{ctx['manifest']['dataset_files']['queries']}`",
        f"- Dataset qrels: `{ctx['manifest']['dataset_files']['qrels']}`",
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
        "| Configuration | nDCG@10 | MAP@10 | MRR@10 | P@10 |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        m = r["metrics"]
        lines.append(
            f"| {r['label']} | {m['ndcg@10']:.4f} | {m['map@10']:.4f} | "
            f"{m['mrr@10']:.4f} | {m['precision@10']:.4f} |"
        )
    m = rrf
    lines.append(
        f"| RRF baseline | {m['ndcg@10']:.4f} | {m['map@10']:.4f} | "
        f"{m['mrr@10']:.4f} | {m['precision@10']:.4f} |"
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"report written: {path}")


def _runtime_metadata() -> dict[str, str]:
    packages: dict[str, str] = {}
    for package in ("onefind", "numpy", "sentence-transformers", "sqlite-vec", "ranx"):
        try:
            packages[package] = version(package)
        except PackageNotFoundError:
            packages[package] = "not-installed"
    try:
        git = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        commit = git.stdout.strip() if git.returncode == 0 else "unavailable"
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        dirty = status.returncode != 0 or bool(status.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        commit = "unavailable"
        dirty = True
    return {
        "python": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "commit": commit,
        "dirty": dirty,
        "packages": packages,
    }


def _write_report(path: Path, **ctx) -> None:
    runtime = _runtime_metadata()
    manifest = ctx["manifest"]
    package_versions = ", ".join(
        f"{name}={package_version}" for name, package_version in runtime["packages"].items()
    )
    lines = [
        f"# Evaluation — {ctx['source']}",
        "",
        f"- Date: {datetime.date.today().isoformat()}",
        f"- Model: `{ctx['model']}` · docs: "
        f"{ctx['num_docs']} · judged queries: {ctx['num_queries']} · k: {ctx['k']}",
        f"- Model revision: `{manifest['model_revision']}`",
        f"- Code: `{runtime['commit']}` · worktree: {'dirty' if runtime['dirty'] else 'clean'} · "
        f"Python {runtime['python']} · {runtime['platform']}",
        f"- Packages: {package_versions}",
        f"- Dataset corpus: `{manifest['dataset_files']['corpus']}`",
        f"- Dataset queries: `{manifest['dataset_files']['queries']}`",
        f"- Dataset qrels: `{manifest['dataset_files']['qrels']}`",
        f"- DB: `{ctx['db']}` ({ctx['stats'].get('vectors', '?')} vectors)",
        "- Latency: warm index, per query, includes query encoding (ms).",
        "",
        "| Configuration | nDCG@10 | MAP@10 | MRR@10 | P@10 | p50 ms | p95 ms |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in ctx["results"]:
        m = row["metrics"]
        lines.append(
            f"| {row['label']} | {m['ndcg@10']:.4f} | {m['map@10']:.4f} | "
            f"{m['mrr@10']:.4f} | {m['precision@10']:.4f} | {row['p50_ms']:.1f} | "
            f"{row['p95_ms']:.1f} |"
        )
    lines += [
        "",
        "## Notes",
        "- int8/binary precisions are computed application-side over the stored "
        "float vectors (ADR-7); see docs/02.",
        "- Float uses sqlite-vec cosine KNN; int8/binary use application-side "
        "ranking over stored float vectors (ADR-7).",
        "- Hybrid uses RRF k=60 and leg depth = k. With `+rerank`, each leg "
        "retrieves candidate-depth (default 50), and the complete fused pool "
        "is scored by full-precision cosine.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"report written: {path}")
