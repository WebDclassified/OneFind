"""OneFind command-line interface.

Exit codes (docs/03-app-flow.md):
0 success, 2 usage/validation, 3 environment, 4 data error, 130 cancelled.

Which collection a command works on: --db PATH wins, then --collection NAME
(from the library, see 'onefind collections'), then onefind.db in the
current folder. Commands that only read or maintain an existing collection
fall back to the library's active collection when there is no onefind.db
here.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import shutil
import signal
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, TextIO

from . import __version__
from .envcheck import collect_check
from .errors import DataError, EnvError, OneFindError, OperationCancelled, UsageError

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_ENV = 3
EXIT_DATA = 4
EXIT_CANCELLED = 130

DEFAULT_DB = "onefind.db"
# How many skipped files / duplicate groups the index summary names.
_LIST_LIMIT = 10


# -- small formatting helpers ---------------------------------------------------


def _note(message: str) -> None:
    """A side remark for humans; stderr so stdout stays parseable."""
    print(message, file=sys.stderr)


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def _human_bytes(size: int | None) -> str:
    if size is None:
        return "?"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"  # pragma: no cover - loop always returns


def _parse_iso(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        moment = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment


def _when(value: str | None) -> str:
    """Local ``YYYY-MM-DD HH:MM (3 hours ago)`` for an ISO timestamp."""
    moment = _parse_iso(value)
    if moment is None:
        return "never"
    local = moment.astimezone()
    seconds = (datetime.datetime.now(datetime.timezone.utc) - moment).total_seconds()
    if seconds < 0:
        ago = "just now"
    elif seconds < 90:
        ago = "just now"
    elif seconds < 3600:
        ago = f"{int(seconds // 60)} min ago"
    elif seconds < 86400 * 2:
        ago = f"{_plural(int(seconds // 3600), 'hour')} ago"
    else:
        ago = f"{_plural(int(seconds // 86400), 'day')} ago"
    return f"{local.strftime('%Y-%m-%d %H:%M')} ({ago})"


def _quote(text: str) -> str:
    """Quote a path/name for a copy-pasteable command suggestion."""
    return f'"{text}"' if any(ch.isspace() for ch in text) or not text else text


# -- collection resolution -------------------------------------------------------


@dataclass
class _Target:
    """The database a command works on, plus its library name when known."""

    db: Path
    name: str | None = None


def _library(args: argparse.Namespace):
    from .library import Library

    return Library(getattr(args, "home", None))


def _safe_active(args: argparse.Namespace) -> dict | None:
    try:
        return _library(args).active()
    except (OneFindError, OSError):
        return None


def _known_names(args: argparse.Namespace) -> list[str]:
    try:
        return [entry["name"] for entry in _library(args).list()]
    except (OneFindError, OSError):
        return []


def _name_for_db(args: argparse.Namespace, db: Path) -> str | None:
    try:
        entry = _library(args).find_by_db(db)
    except (OneFindError, OSError):
        return None
    return entry["name"] if entry else None


def _resolve_target(
    args: argparse.Namespace,
    *,
    must_exist: bool,
    create_collection: bool = False,
    allow_active: bool = False,
) -> _Target:
    """Pick the database: ``--db`` > ``--collection`` > ``onefind.db``.

    ``create_collection`` creates an unknown ``--collection`` (index);
    ``allow_active`` falls back to the active collection when neither option
    is given and ``onefind.db`` does not exist here.
    """
    db = getattr(args, "db", None)
    name = getattr(args, "collection", None)
    if db:
        target = _Target(Path(db))
    elif name:
        library = _library(args)
        try:
            entry = library.get(name)
        except DataError:
            if not create_collection:
                known = _known_names(args)
                hint = f" (known: {', '.join(known)})" if known else ""
                raise DataError(
                    f"no collection named {name!r}{hint}; see 'onefind collections list' "
                    f"or create it with 'onefind collections create {_quote(name)}'"
                ) from None
            entry = library.create(name)
            _note(f"created collection {entry['name']!r} at {entry['db']}")
        target = _Target(Path(entry["db"]), entry["name"])
    else:
        target = _Target(Path(DEFAULT_DB))
        if allow_active and not target.db.exists():
            active = _safe_active(args)
            if active is not None:
                _note(f"using the active collection {active['name']!r} (no {DEFAULT_DB} here)")
                target = _Target(Path(active["db"]), active["name"])

    if must_exist and not target.db.is_file():
        known = _known_names(args)
        if target.name:
            raise DataError(
                f"the file of collection {target.name!r} is missing: {target.db} "
                f"(re-index into it, or remove it with 'onefind collections remove "
                f"{_quote(target.name)}')"
            )
        hint = (
            f", or choose a collection with --collection NAME (known: {', '.join(known)})"
            if known
            else ""
        )
        raise DataError(
            f"no OneFind collection at {target.db}; index a folder first "
            f"('onefind index FOLDER --db {_quote(str(target.db))}'){hint}"
        )
    return target


def _label(target: _Target) -> str:
    return f"{target.name!r} ({target.db})" if target.name else str(target.db)


def _open_index(target: _Target):
    from .store import Index

    return Index.open(target.db)


def _target_opts(target: _Target, name: str | None = None) -> str:
    """`` --collection NAME`` (or `` --db PATH``) for a suggested command.

    Suggestions must name the collection they were printed for: without it
    'onefind index' writes to ./onefind.db and 'onefind doctor' may check the
    active collection instead.
    """
    name = name or target.name
    if name:
        return f" --collection {_quote(name)}"
    return f" --db {_quote(str(target.db.absolute()))}"


def _load_embedder(model_name: str):
    """Load the sentence-transformers model, or raise EnvError (exit 3)."""
    from .embed import SentenceEmbedder

    try:
        return SentenceEmbedder(model_name)
    except ImportError as exc:
        raise EnvError(f"semantic search needs the model stack: {exc}") from None
    except (OSError, RuntimeError, ValueError) as exc:
        raise EnvError(
            f"could not load embedding model {model_name!r} ({exc.__class__.__name__}); "
            "connect to the internet once so it can download, or use --mode lexical"
        ) from None


# -- check / eval / sweep / smoke -------------------------------------------------


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

    # Same keyword matching as the app and 'onefind search': "auto" for
    # lexical/hybrid cases ("all" for semantic) unless --match says otherwise.
    match = getattr(args, "match", None) or "auto"
    result = run_smoke(
        args.db,
        args.queries,
        k=args.k,
        model_name=args.model,
        lexical_match=match,
    )
    print(f"smoke cases       : {result['cases']}")
    print(f"answerable cases  : {result['answerable']}")
    print(f"hit@3             : {result['hit_at_3']:.3f}")
    print(f"MRR@{args.k}          : {result['mrr_at_k']:.3f}")
    print(f"no-answer accuracy: {result['no_answer_accuracy']:.3f}")
    print(f"keyword matching  : {result.get('lexical_match', match)}")
    for row in result["rows"]:
        outcome = "—" if row["rank"] is None else str(row["rank"])
        print(f"  {row['mode']:<8} rank={outcome:>2}  {row['query']}")
    return EXIT_OK


# -- index -------------------------------------------------------------------------


class ProgressPrinter:
    """Render ingest progress events on a stream (stderr by default).

    On a terminal one line is rewritten in place (throttled to ~10 updates a
    second); otherwise a plain line is written when the phase changes and at
    most every ``interval`` seconds, so logs stay short.
    """

    LABELS = {
        "scan": "Scanning",
        "read": "Reading",
        "write": "Saving",
        "embed": "Embedding",
        "prune": "Cleaning up",
        "model": "Loading model",
    }

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        tty: bool | None = None,
        interval: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.stream = stream if stream is not None else sys.stderr
        if tty is None:
            try:
                tty = bool(self.stream.isatty())
            except (AttributeError, ValueError, OSError):
                tty = False
        self.tty = tty
        self.interval = interval if interval is not None else (0.1 if tty else 2.0)
        self._clock = clock
        self._phase: str | None = None
        self._last = float("-inf")
        self._width = 0

    @classmethod
    def format(cls, event: dict) -> str:
        phase = str(event.get("phase") or "")
        message = str(event.get("message") or cls.LABELS.get(phase, phase.title() or "Working"))
        done, total = event.get("done"), event.get("total")
        if isinstance(total, int) and total > 0 and isinstance(done, int):
            percent = min(100, int(done * 100 / total))
            return f"[{done}/{total} {percent:>3}%] {message}"
        return message

    def __call__(self, event: dict) -> None:
        phase = event.get("phase")
        if phase == "done":
            return  # the caller prints a full summary
        now = self._clock()
        if phase == self._phase and now - self._last < self.interval:
            return
        self._phase, self._last = phase, now
        line = self.format(event)
        try:
            if self.tty:
                columns = shutil.get_terminal_size((80, 20)).columns
                line = line[: max(10, columns - 1)]
                pad = max(0, self._width - len(line))
                self.stream.write("\r" + line + " " * pad)
                self._width = len(line)
            else:
                self.stream.write(line + "\n")
            self.stream.flush()
        except (OSError, ValueError):
            pass  # progress is cosmetic; never fail the operation over it

    def finish(self) -> None:
        """Clear the in-place line so the summary starts on a clean row."""
        if self.tty and self._width:
            try:
                self.stream.write("\r" + " " * self._width + "\r")
                self.stream.flush()
            except (OSError, ValueError):
                pass
        self._width = 0


class _Silent:
    def __call__(self, event: dict) -> None:
        pass

    def finish(self) -> None:
        pass


@contextlib.contextmanager
def _ctrl_c_cancels(flag: threading.Event, what: str = "indexing") -> Iterator[None]:
    """First Ctrl+C asks the operation to stop cleanly; a second one aborts."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def handler(_signum, _frame) -> None:
        if flag.is_set():
            raise KeyboardInterrupt
        flag.set()
        _note(f"\nstopping {what} after the current step... (press Ctrl+C again to stop now)")

    try:
        previous = signal.signal(signal.SIGINT, handler)
    except (ValueError, OSError):  # embedded interpreters may forbid handlers
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def _report_cancelled(index, target: _Target) -> None:
    run = None
    try:
        run = index.last_run()
        total = index.count("documents")
    except Exception:  # noqa: BLE001 - reporting must not mask the cancel
        total = None
    kept = ""
    if run and run.get("status") == "cancelled":
        kept = (
            f" Kept what was already saved: {run['added']} added, {run['updated']} updated"
            f", {run['removed']} removed."
        )
    collection = f" The collection now holds {_plural(total, 'document')}." if total is not None else ""
    _note(
        f"cancelled: indexing stopped early.{kept}{collection} "
        "Run the same command again to finish - unchanged files are skipped quickly."
    )


def _vector_line(index) -> tuple[str, int | None]:
    """Plain description of the vector state and the vector count (or None)."""
    model = index.get_meta("model_name")
    if model is None:
        return "none - keyword search only (add --embed for meaning search)", None
    if not index.vec_ready:
        return "stored, but sqlite-vec cannot be loaded here (run 'onefind check')", None
    try:
        health = index.health()
    except sqlite3.Error as exc:
        return f"cannot be read ({exc})", None
    vectors = int(health["vectors"] or 0)
    missing = int(health["missing_vectors"] or 0)
    if missing:
        return (
            f"{missing} of {health['chunks']} notes have no meaning vector yet - "
            "rerun with --embed (or 'onefind doctor --repair') so semantic search sees them",
            vectors,
        )
    return f"{vectors} of {health['chunks']} notes ({model})", vectors


def _print_index_summary(index, stats: dict, target: _Target, args) -> None:
    vector_text, n_vectors = _vector_line(index)
    bound_model = index.get_meta("model_name")
    vectors = f", {n_vectors} vectors" if n_vectors is not None else ""
    extra = (
        f" [model: {bound_model}; float storage, app-side int8/bit]"
        if bound_model
        else " [lexical only - add --embed for semantic search]"
    )
    print(
        f"indexed {stats['files_indexed']} files "
        f"({stats['total_documents']} documents total{vectors}) -> {target.db}{extra}"
    )
    skipped = stats.get("skipped") or []
    changes = [
        f"{stats.get('added', 0)} added",
        f"{stats.get('updated', 0)} updated",
        f"{stats.get('unchanged', 0)} unchanged",
        f"{stats.get('removed', 0)} removed",
        f"{len(skipped)} skipped",
    ]
    print(f"  changes    : {', '.join(changes)}")
    if skipped:
        reasons: dict[str, int] = {}
        for item in skipped:
            reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
        print(
            "  skipped    : "
            + ", ".join(f"{count} {reason}" for reason, count in sorted(reasons.items()))
        )
        for item in skipped[:_LIST_LIMIT]:
            print(f"               {item['path']} ({item['reason']})")
        if len(skipped) > _LIST_LIMIT:
            print(f"               ... and {len(skipped) - _LIST_LIMIT} more")
    duplicates = stats.get("duplicates") or []
    if duplicates:
        print(f"  duplicates : {_plural(len(duplicates), 'group')} of identical files")
        for group in duplicates[:3]:
            print(f"               {' = '.join(group[:4])}{' ...' if len(group) > 4 else ''}")
    root_id = stats.get("root_id")
    if root_id is not None:
        root = next((r for r in index.roots() if r["root_id"] == root_id), None)
        if root and root["exclude"]:
            print(f"  excluding  : {', '.join(root['exclude'])} (remembered for this folder)")
    print(f"  vectors    : {vector_text}")
    if target.name:
        print(f"  collection : {target.name}")
    if (
        not getattr(args, "prune", False)
        and root_id is not None
        and Path(args.path).is_dir()
    ):
        # Documents of this folder that no file supports any more (deleted,
        # moved, excluded, or no longer indexable). Unreadable files keep
        # their documents on purpose, so they do not count.
        root = next((r for r in index.roots() if r["root_id"] == root_id), None)
        kept_unreadable = sum(1 for item in skipped if item["reason"] == "unreadable")
        stale = (root["documents"] if root else 0) - stats["files_indexed"] - kept_unreadable
        if stale > 0:
            print(
                f"  note       : {_plural(stale, 'indexed note')} "
                f"{'no longer matches' if stale == 1 else 'no longer match'} a file here "
                "(deleted, moved or excluded); add --prune to remove them"
            )


def cmd_index(args: argparse.Namespace) -> int:
    from .ingest import ingest_path

    # A collection this run creates is forgotten again when the run fails
    # before indexing anything (e.g. the embedding model cannot be loaded).
    created: str | None = None
    if not getattr(args, "db", None) and getattr(args, "collection", None):
        try:
            _library(args).get(args.collection)
        except DataError:
            created = args.collection
        except (OneFindError, OSError):
            pass
    target = _resolve_target(args, must_exist=False, create_collection=True)
    if created is not None:
        created = target.name
    if not getattr(args, "db", None) and not getattr(args, "collection", None):
        if not target.db.exists():
            active = _safe_active(args)
            if active is not None:
                _note(
                    f"note: indexing into ./{DEFAULT_DB}; use --collection "
                    f"{_quote(active['name'])} to index into your active collection"
                )
    printer: ProgressPrinter | _Silent = (
        _Silent() if getattr(args, "quiet", False) else ProgressPrinter()
    )
    cancel = threading.Event()

    try:
        return _index_into(args, target, printer, cancel, ingest_path)
    except EnvError:
        if created is not None:
            _forget_empty_collection(args, created)
        raise


def _forget_empty_collection(args: argparse.Namespace, name: str) -> None:
    """Undo a collection this run created when nothing was indexed into it."""
    try:
        entry = _library(args).get(name)
    except (OneFindError, OSError):
        return
    if _document_count(entry["db"]) != 0:
        return
    try:
        _library(args).remove(name, delete_file=True)
    except (OneFindError, OSError):
        return
    _note(f"removed the empty collection {name!r} this run created")


def _index_into(args, target: _Target, printer, cancel: threading.Event, ingest_path) -> int:
    with _open_index(target) as index:
        try:
            with _ctrl_c_cancels(cancel):
                if args.embed:
                    from .embed import DEFAULT_MODEL

                    model_name = args.model or index.get_meta("model_name") or DEFAULT_MODEL
                    print(f"loading embedding model: {model_name} ...")
                    embedder = _load_embedder(model_name)
                    if cancel.is_set():  # Ctrl+C while the model was loading
                        raise OperationCancelled("indexing was cancelled")
                    index.attach_embedder(embedder, progress=printer, cancel=cancel.is_set)
                stats = ingest_path(
                    index,
                    args.path,
                    exclude=getattr(args, "exclude", None),
                    prune=bool(getattr(args, "prune", False)),
                    incremental=not getattr(args, "full", False),
                    progress=printer,
                    cancel=cancel.is_set,
                )
        except (OperationCancelled, KeyboardInterrupt):
            printer.finish()
            _report_cancelled(index, target)
            return EXIT_CANCELLED
        except BaseException:
            printer.finish()
            raise
        printer.finish()
        _print_index_summary(index, stats, target, args)
    return EXIT_OK


# -- search --------------------------------------------------------------------------


def _one_line(snippet: str) -> str:
    from .snippets import ELLIPSIS, FTS_END, FTS_START

    return " ".join(
        snippet.replace(FTS_START, "[")
        .replace(FTS_END, "]")
        .replace(ELLIPSIS, "...")
        .split()
    )


def _print_hits(hits, *, explain: bool = False) -> None:
    for rank, hit in enumerate(hits, start=1):
        snippet_one_line = _one_line(hit.snippet) if hit.snippet else ""
        print(f"{rank:>2}. [{hit.score:.4f}] {hit.doc_id} - {hit.title}")
        if snippet_one_line:
            print(f"    {snippet_one_line[:120]}")
        if explain and hit.explain:
            confidence = hit.explain.get("confidence")
            if confidence:
                print(f"    why ({confidence} match):")
            else:
                print("    why:")
            for reason in hit.explain.get("reasons") or []:
                print(f"      - {reason}")
            missing = hit.explain.get("missing_terms") or []
            if missing:
                print(f"      - Not found in this note: {', '.join(missing)}")
    if not hits:
        print("no results")


def _search_filters(args: argparse.Namespace):
    from .search import SearchFilters

    since = getattr(args, "since", None)
    if since is not None:
        try:
            datetime.date.fromisoformat(since.strip())
        except ValueError:
            raise UsageError(f"--since must be a date like 2026-01-31 (got {since!r})") from None
    filters = SearchFilters(
        folders=tuple(getattr(args, "folder", None) or ()),
        exts=tuple(getattr(args, "type", None) or ()),
        tags=tuple(getattr(args, "tag", None) or ()),
        modified_after=since.strip() if since else None,
    )
    return None if filters.is_empty() else filters


def _describe_filters(args: argparse.Namespace) -> str:
    parts = [f"folder={value}" for value in getattr(args, "folder", None) or ()]
    parts += [f"type={value}" for value in getattr(args, "type", None) or ()]
    parts += [f"tag={value}" for value in getattr(args, "tag", None) or ()]
    if getattr(args, "since", None):
        parts.append(f"since={args.since}")
    return ", ".join(parts)


def cmd_search(args: argparse.Namespace) -> int:
    from .search import search_detailed
    from .store import Index

    if not 1 <= args.k <= 50:
        raise UsageError("--k must be within 1..50")
    filters = _search_filters(args)
    explain = bool(getattr(args, "explain", False))
    target = _resolve_target(args, must_exist=True, allow_active=True)

    mode = args.mode
    with Index.open(target.db) as index:
        if mode in ("semantic", "hybrid"):
            model_name = index.get_meta("model_name")
            if model_name is None:
                raise UsageError(
                    "this index has no embeddings; rebuild with 'onefind index --embed' "
                    "or search with --mode lexical"
                )
            print(f"loading embedding model: {model_name} ...")
            try:
                embedder = _load_embedder(model_name)
            except EnvError as exc:
                if mode != "hybrid":
                    raise
                # Keyword search never needs the model (docs/10: lexical
                # search still works when meaning search is unavailable).
                _note(f"note: {exc}; showing keyword results instead")
                mode = "lexical"
            else:
                index.attach_embedder(embedder)
        # Same keyword matching as the app (serve.py): all-words hits first,
        # then partial matches when too few notes contain every word.
        lexical_match = getattr(args, "match", None) or (
            "auto" if mode in ("lexical", "hybrid") else "all"
        )
        outcome = search_detailed(
            index, args.query, mode=mode, k=args.k,
            precision=args.precision, rrf_k=args.rrf_k, rerank=args.rerank,
            fusion=args.fusion, alpha=args.alpha,
            candidate_depth=args.candidate_depth,
            filters=filters, explain=explain, lexical_match=lexical_match,
        )
    if outcome.hits and outcome.relaxed and not outcome.strict_count:
        _note("note: no note contains all of your keywords - showing partial matches")
    _print_hits(outcome.hits, explain=explain)
    if filters is not None and not outcome.hits:
        print(f"(filters: {_describe_filters(args)} - try removing a filter)")
    elif outcome.abstained:
        print("note: these are all weak matches - try other words or another --mode")
    return EXIT_OK


# -- collections -------------------------------------------------------------------


def _connect_for_read(path: Path) -> sqlite3.Connection:
    """Open an existing database for reading without leaving -wal/-shm behind.

    A ``mode=ro`` connection to a WAL database creates -wal/-shm but can
    neither checkpoint nor delete them on close. ``mode=rw`` never creates a
    missing file and never writes for a SELECT; as the last connection to
    close it checkpoints and removes them. ``mode=ro`` stays the fallback
    for read-only files or media.
    """
    uri = path.resolve().as_uri()
    try:
        return sqlite3.connect(f"{uri}?mode=rw", uri=True)
    except sqlite3.OperationalError:
        return sqlite3.connect(f"{uri}?mode=ro", uri=True)


def _document_count(db: str | Path) -> int | None:
    """Document count without modifying the file; None when it cannot be read."""
    path = Path(db)
    if not path.is_file():
        return None
    try:
        conn = _connect_for_read(path)
    except (sqlite3.Error, ValueError, OSError):
        return None
    try:
        return int(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def cmd_collections_list(args: argparse.Namespace) -> int:
    library = _library(args)
    items = library.list()
    if getattr(args, "json", False):
        rows = [{**item, "documents": _document_count(item["db"])} for item in items]
        print(json.dumps({"home": str(library.home), "collections": rows}, indent=2))
        return EXIT_OK
    if not items:
        print("no collections yet")
        print(
            "  create one with: onefind collections create NAME  "
            "(or: onefind index FOLDER --collection NAME)"
        )
        print(f"  library folder : {library.home}")
        return EXIT_OK
    width = max(len("NAME"), *(len(item["name"]) for item in items))
    print(f"  {'NAME':<{width}}  {'DOCUMENTS':>9}  FILE")
    for item in items:
        count = _document_count(item["db"])
        documents = "-" if count is None else str(count)
        marker = "*" if item["active"] else " "
        missing = "" if item["exists"] else "  (file missing)"
        print(f"{marker} {item['name']:<{width}}  {documents:>9}  {item['db']}{missing}")
    print(f"(* = active; library folder: {library.home})")
    return EXIT_OK


def cmd_collections_create(args: argparse.Namespace) -> int:
    library = _library(args)
    db_path = getattr(args, "db", None)
    if db_path and Path(db_path).expanduser().exists():
        entry = library.register(args.name, db_path)
        print(f"added existing collection {entry['name']!r} -> {entry['db']}")
    else:
        entry = library.create(args.name, db_path)
        print(f"created collection {entry['name']!r} -> {entry['db']}")
    if entry["active"]:
        print("it is the active collection")
    print(f"next: onefind index FOLDER --collection {_quote(entry['name'])}")
    return EXIT_OK


def cmd_collections_use(args: argparse.Namespace) -> int:
    entry = _library(args).set_active(args.name)
    print(f"active collection: {entry['name']!r} -> {entry['db']}")
    if not entry["exists"]:
        _note("warning: its database file is missing; re-index into it before searching")
    return EXIT_OK


def cmd_collections_remove(args: argparse.Namespace) -> int:
    library = _library(args)
    entry = library.get(args.name)
    if args.delete_file and not args.yes:
        raise UsageError(
            f"--delete-file permanently deletes {entry['db']}; add --yes to confirm "
            "(consider 'onefind backup --collection NAME' first)"
        )
    library.remove(entry["name"], delete_file=bool(args.delete_file))
    if args.delete_file:
        print(f"removed collection {entry['name']!r} and deleted {entry['db']}")
    else:
        print(f"removed collection {entry['name']!r} from the list; the file is kept at {entry['db']}")
    active = library.active()
    if entry["active"] and active is not None:
        print(f"active collection is now {active['name']!r}")
    return EXIT_OK


def cmd_collections_rename(args: argparse.Namespace) -> int:
    entry = _library(args).rename(args.name, args.new_name)
    print(f"renamed {args.name!r} to {entry['name']!r} (file unchanged: {entry['db']})")
    return EXIT_OK


# -- maintenance ---------------------------------------------------------------------


def cmd_backup(args: argparse.Namespace) -> int:
    if getattr(args, "list", False):
        return _list_backups(args)
    target = _resolve_target(args, must_exist=True, allow_active=True)
    name = target.name or _name_for_db(args, target.db)
    library = _library(args)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    if args.out:
        dest = Path(args.out).expanduser()
        # A trailing slash, or a new path without an extension, names a folder:
        # never write a backup file called "Backups" with no .db extension.
        as_folder = dest.is_dir() or str(args.out).endswith(("/", "\\")) or (
            not dest.exists() and not dest.suffix
        )
        if as_folder:
            dest.mkdir(parents=True, exist_ok=True)
            dest = dest / f"{target.db.stem}-{stamp}.db"
    elif name:
        dest = library.backup_path_for(name)
    else:
        dest = target.db.with_name(f"{target.db.stem}-backup-{stamp}.db")
    with _open_index(target) as index:
        info = index.backup_to(dest, overwrite=bool(getattr(args, "overwrite", False)))
        documents = index.count("documents")
    print(f"backup written: {info['path']} ({_human_bytes(info['bytes'])}, "
          f"{_plural(documents, 'document')})")
    restored = f"{name or target.db.stem} restored"
    print(f"restore it with: onefind restore {_quote(info['path'])} --as {_quote(restored)}")
    return EXIT_OK


def _list_backups(args: argparse.Namespace) -> int:
    library = _library(args)
    name = getattr(args, "collection", None)
    items = library.list_backups(name)
    if not items:
        print(f"no backups in {library.backups_dir}")
        return EXIT_OK
    for item in items:
        print(f"{_when(item['created_at']):<36} {_human_bytes(item['bytes']):>10}  {item['path']}")
    return EXIT_OK


def _free_collection_path(library, slug: str) -> Path:
    directory = library.collections_dir
    candidate = directory / f"{slug}.db"
    counter = 2
    registered = set()
    with contextlib.suppress(OneFindError, OSError):
        registered = {str(Path(entry["db"]).resolve()).casefold() for entry in library.list()}

    def taken(path: Path) -> bool:
        return (
            any(Path(f"{path}{ext}").exists() for ext in ("", "-wal", "-shm"))
            or str(path.resolve()).casefold() in registered
        )

    while taken(candidate):
        candidate = directory / f"{slug}-{counter}.db"
        counter += 1
    return candidate


def cmd_restore(args: argparse.Namespace) -> int:
    from .library import slugify, validate_name
    from .store import Index

    library = _library(args)
    name = validate_name(args.as_name)
    for entry in library.list():
        if entry["name"].casefold() == name.casefold():
            raise UsageError(
                f"a collection named {entry['name']!r} already exists; "
                "pick another name for --as"
            )
    source = Path(args.backup).expanduser()
    if not source.is_file() and (library.backups_dir / args.backup).is_file():
        source = library.backups_dir / args.backup
    if not source.is_file():
        raise DataError(
            f"backup file not found: {source} (list backups with 'onefind backup --list')"
        )
    dest = _free_collection_path(library, slugify(name))
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DataError(f"cannot create folder {dest.parent}: {exc}") from exc

    def _cleanup() -> None:
        for ext in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                Path(f"{dest}{ext}").unlink(missing_ok=True)

    try:
        # The backup is copied, never opened in place: Index.open may upgrade
        # an older file and the user's backup must stay exactly as it was.
        src = sqlite3.connect(source)
        try:
            out = sqlite3.connect(dest)
            try:
                src.backup(out)
            finally:
                out.close()
        finally:
            src.close()
        with Index.open(dest) as index:
            documents = index.count("documents")
        entry = library.register(name, dest)
    except sqlite3.DatabaseError as exc:
        _cleanup()
        raise DataError(f"{source} is not a OneFind backup: {exc}") from exc
    except BaseException:
        _cleanup()
        raise
    print(
        f"restored {_plural(documents, 'document')} from {source} into the new collection "
        f"{entry['name']!r} -> {entry['db']}"
    )
    if entry["active"]:
        print("it is now the active collection")
    else:
        print(f"switch to it with: onefind collections use {_quote(entry['name'])}")
    return EXIT_OK


def _health_summary(index, opts: str = "") -> tuple[str, str]:
    """(state, one plain sentence) for a collection.

    ``opts`` (from :func:`_target_opts`) is added to every suggested command
    so it acts on this collection, not ./onefind.db or the active one.
    """
    try:
        documents = index.count("documents")
    except sqlite3.Error as exc:
        return (
            "needs attention",
            f"The collection cannot be read ({exc}). Run 'onefind doctor{opts}'.",
        )
    if documents == 0:
        return "empty", f"Empty - index a folder to start: onefind index FOLDER{opts}"
    has_vectors = index.get_meta("model_name") is not None
    if has_vectors and not index.vec_ready:
        return (
            "needs attention",
            "Meaning-search vectors are stored but sqlite-vec cannot be loaded here; "
            "keyword search still works. Run 'onefind check' for the fix.",
        )
    try:
        health = index.health()
    except sqlite3.Error as exc:
        return "needs attention", f"Health check failed ({exc}). Run 'onefind doctor{opts}'."
    if not (health["documents"] == health["chunks"] == health["fts"]):
        return (
            "needs attention",
            "The keyword index is out of step with the documents. "
            f"Fix: onefind doctor --repair{opts}",
        )
    if has_vectors and health["orphan_vectors"]:
        return (
            "needs attention",
            f"{_plural(health['orphan_vectors'], 'stored vector')} belong to removed notes. "
            f"Fix: onefind doctor --repair{opts}",
        )
    if has_vectors and health["missing_vectors"]:
        return (
            "partly ready",
            f"{_plural(health['missing_vectors'], 'note')} have no meaning vector yet, so "
            "semantic and hybrid search skip them; keyword search covers everything. "
            f"Fix: onefind doctor --repair{opts} (or re-index with --embed)",
        )
    if has_vectors:
        return "ready", f"Ready - {_plural(documents, 'note')} searchable by keyword and meaning."
    return (
        "ready",
        f"Ready - {_plural(documents, 'note')} searchable by keyword. "
        f"Add meaning search with: onefind index FOLDER{opts} --embed",
    )


def _describe_changes(scan: dict, opts: str = "") -> str:
    if not scan["exists"]:
        return "folder not found - it may have moved or been deleted"
    parts = []
    if scan["new"]:
        parts.append(f"{scan['new']} new")
    if scan["changed"]:
        parts.append(f"{scan['changed']} changed")
    if scan["deleted"]:
        parts.append(f"{scan['deleted']} deleted")
    if not parts:
        return "up to date" + (" (scan stopped early: very large folder)" if scan["truncated"] else "")
    fix = f"onefind index {_quote(scan['root'])}{opts}" + (
        " --prune" if scan["deleted"] else ""
    )
    return f"{', '.join(parts)} since the last index - refresh: {fix}"


def cmd_status(args: argparse.Namespace) -> int:
    from .ingest import scan_changes

    target = _resolve_target(args, must_exist=True, allow_active=True)
    name = target.name or _name_for_db(args, target.db)
    opts = _target_opts(target, name)
    with _open_index(target) as index:
        state, sentence = _health_summary(index, opts)
        documents = index.count("documents")
        model = index.get_meta("model_name")
        vectors: int | None = None
        if model is not None and index.vec_ready:
            with contextlib.suppress(sqlite3.Error):
                vectors = index.count("vectors")
        roots = index.roots()
        for root in roots:
            root["changes"] = None
            if not getattr(args, "no_scan", False):
                try:
                    root["changes"] = scan_changes(index, root["path"])
                except (OneFindError, OSError) as exc:
                    root["changes"] = {"error": str(exc)}
        last_run = index.last_run()
        last_indexed = index.last_indexed_at()
        from .watcher import get_preference

        auto_sync = "on" if get_preference(index) else "off"
        passages = _passage_state(index)
    try:
        size = target.db.stat().st_size
    except OSError:
        size = None
    stale = any(
        r["changes"] and (not r["changes"].get("exists", True) or any(
            r["changes"].get(key) for key in ("new", "changed", "deleted")
        ))
        for r in roots
    )

    if getattr(args, "json", False):
        print(json.dumps({
            "collection": name,
            "db": str(target.db),
            "db_bytes": size,
            "documents": documents,
            "vectors": vectors,
            "model": model,
            "state": state,
            "message": sentence,
            "stale": stale,
            "roots": roots,
            "last_run": last_run,
            "last_indexed_at": last_indexed,
            "auto_sync": auto_sync,
            "chunking": passages["chunking"],
            "long_notes": passages["long_notes"],
            "vectors_need_rebuild": passages["rebuild"],
        }, indent=2))
        return EXIT_OK

    print(f"collection : {name or target.db.name}")
    print(f"  file     : {target.db} ({_human_bytes(size)})")
    vector_text = (
        "none (keyword search only)"
        if model is None
        else f"{vectors if vectors is not None else '?'} ({model})"
    )
    print(f"  notes    : {documents}, vectors: {vector_text}")
    print(f"  health   : {state} - {sentence}")
    print(
        f"  auto-sync: {auto_sync}"
        + (" (the web app keeps this collection up to date)" if auto_sync == "on" else "")
    )
    print(f"  passages : {_passage_line(passages)}")
    hint = _passage_hint(passages, opts)
    if hint:
        print(f"             {hint}")
    if roots:
        print("  folders  :")
        for root in roots:
            print(f"    {root['path']}")
            print(
                f"      {_plural(root['documents'], 'document')}, "
                f"indexed {_when(root['last_indexed_at'])}"
            )
            changes = root["changes"]
            if changes is None:
                continue
            if "error" in changes:
                print(f"      could not scan: {changes['error']}")
            else:
                print(f"      {_describe_changes(changes, opts)}")
    else:
        print(f"  folders  : none registered (index a folder with: onefind index FOLDER{opts})")
    if last_run:
        counts = ", ".join(
            f"{last_run[key]} {key}"
            for key in ("added", "updated", "unchanged", "removed", "skipped")
        )
        finished = last_run["finished_at"] or last_run["started_at"]
        print(f"  last run : {last_run['status']} {_when(finished)} - {counts}")
        if last_run.get("error") and last_run["status"] != "succeeded":
            print(f"             {str(last_run['error'])[:200]}")
    else:
        print("  last run : never")
    return EXIT_OK


def _print_integrity(report: dict) -> None:
    print(f"  database file : {'ok' if report['sqlite'] == 'ok' else 'DAMAGED'}")
    print(f"  keyword index : {'ok' if report['fts'] == 'ok' else 'DAMAGED'}")
    health = report.get("health")
    if health is None:
        print("  vectors       : cannot be checked here")
    elif health.get("vectors") is None:
        print("  vectors       : not built (keyword search only)")
    else:
        missing = health.get("missing_vectors") or 0
        detail = "all present" if not missing else f"{missing} missing"
        print(f"  vectors       : {health['vectors']} for {health['chunks']} notes ({detail})")
    if report["problems"]:
        print("problems:")
        for problem in report["problems"]:
            print(f"  - {problem}")
    if report["suggestions"]:
        print("suggestions:")
        for suggestion in report["suggestions"]:
            print(f"  - {suggestion}")


def cmd_doctor(args: argparse.Namespace) -> int:
    target = _resolve_target(args, must_exist=True, allow_active=True)
    with _open_index(target) as index:
        print(f"checking {_label(target)}")
        report = index.integrity_check()
        if args.repair:
            health = report.get("health") or {}
            model = index.get_meta("model_name")
            if model and health.get("missing_vectors") and index.vec_ready:
                from .jobs import model_stack_available

                if model_stack_available():
                    try:
                        from .embed import SentenceEmbedder

                        print(f"loading embedding model: {model} ...")
                        index.attach_embedder(SentenceEmbedder(model))
                    except Exception as exc:  # noqa: BLE001 - repair the rest anyway
                        _note(f"warning: could not load the embedding model ({exc}); "
                              "missing vectors stay missing")
                else:
                    _note("note: the embedding model is not installed, so missing vectors "
                          "cannot be rebuilt (pip install -e \".[model]\")")
            result = index.repair()
            print(
                "repaired: rebuilt the keyword index for "
                f"{_plural(result['fts_rebuilt'], 'note')}, fixed {result['chunks_fixed']} "
                f"search rows, removed {result['orphan_rows_removed']} leftovers and "
                f"{_plural(result['orphan_vectors_removed'], 'orphan vector')}, "
                f"embedded {_plural(result['vectors_embedded'], 'missing vector')}"
            )
            report = index.integrity_check()
        _print_integrity(report)
        passages = _passage_state(index)
    opts = _target_opts(target, target.name or _name_for_db(args, target.db))
    hint = _passage_hint(passages, opts)
    if hint:
        # Not damage: the index is consistent, but meaning search is limited.
        print(f"  passages      : {hint}")
    if report["ok"]:
        print("no problems found")
        return EXIT_OK
    if not args.repair:
        print(f"fix what can be fixed with: onefind doctor --repair{opts}")
    return EXIT_DATA


def cmd_compact(args: argparse.Namespace) -> int:
    target = _resolve_target(args, must_exist=True, allow_active=True)
    with _open_index(target) as index:
        result = index.compact()
    saved = max(0, result["before"] - result["after"])
    print(
        f"compacted {_label(target)}: {_human_bytes(result['before'])} -> "
        f"{_human_bytes(result['after'])} (saved {_human_bytes(saved)})"
    )
    return EXIT_OK


def cmd_remove(args: argparse.Namespace) -> int:
    target = _resolve_target(args, must_exist=True, allow_active=True)
    wanted = list(dict.fromkeys(args.doc_ids))
    with _open_index(target) as index:
        found = index.get_documents(wanted)
        missing = [doc_id for doc_id in wanted if doc_id not in found]
        if not found:
            raise DataError(
                f"none of these documents are in {_label(target)}: {', '.join(wanted)}"
            )
        root_ids: list[int | None] = []
        for doc_id in wanted:
            if doc_id in found:
                record = index.get_document(doc_id) or {}
                if record.get("root_id") not in root_ids:
                    root_ids.append(record.get("root_id"))
        roots = {root["root_id"]: root for root in index.roots()}
        removed = index.remove_documents([doc_id for doc_id in wanted if doc_id in found])
    print(f"removed {_plural(removed, 'document')} from the index; files on disk were not touched")
    if missing:
        _note(f"not found: {', '.join(missing)}")
    opts = _target_opts(target, target.name or _name_for_db(args, target.db))
    commands = []
    for root_id in root_ids:
        root = roots.get(root_id)
        folder = _quote(root["path"]) if root else "FOLDER"
        keep = "".join(
            f" --exclude {_quote(pattern)}"
            for pattern in dict.fromkeys(root["exclude"] if root else [])
        )
        command = f"onefind index {folder}{opts}{keep} --exclude PATTERN"
        if command not in commands:
            commands.append(command)
    print(
        "note: re-indexing their folder adds them back unless you exclude them. "
        "--exclude replaces the folder's saved patterns, so repeat the ones to keep:"
    )
    for command in commands:
        print(f"  {command}")
    return EXIT_OK


# -- passages / rebuild-vectors (docs/11 section 9) ---------------------------------------


def _passage_state(index) -> dict:
    """Passage setting, long-note count and pending rebuild of a collection."""
    try:
        rebuild = index.vectors_need_rebuild()
        return {
            "chunking": index.chunking_config(),
            "long_notes": int(rebuild.get("long_notes") or 0),
            "rebuild": rebuild,
            "vectors": index.has_vectors(),
        }
    except (OneFindError, sqlite3.Error):
        return {"chunking": None, "long_notes": 0, "rebuild": None, "vectors": index.has_vectors()}


def _passage_line(state: dict) -> str:
    config = state.get("chunking")
    long_notes = int(state.get("long_notes") or 0)
    if config:
        split = (
            f"; {_plural(long_notes, 'long note')} split into passages" if long_notes else ""
        )
        return (
            f"on ({config['max_words']} words, {config['overlap_words']} overlap{split})"
        )
    if long_notes:
        return (
            f"off - one vector per note; meaning search reads only the beginning of "
            f"{_plural(long_notes, 'note')} longer than 200 words"
        )
    return "off - one vector per note"


def _passage_hint(state: dict | None, opts: str) -> str:
    """A one-line notice when a vector rebuild is needed or recommended ('' otherwise)."""
    rebuild = (state or {}).get("rebuild") or {}
    if rebuild.get("needed"):
        what = "vectors" if (state or {}).get("vectors") else "search passages"
        return (
            f"rebuild needed: {_plural(int(rebuild.get('documents') or 0), 'note')} still "
            f"follow an older passage setting. Fix: onefind rebuild-vectors{opts} "
            f"(updates the {what})"
        )
    if rebuild.get("recommended") and (state or {}).get("vectors"):
        long_notes = int(rebuild.get("long_notes") or 0)
        return (
            f"recommended: {_plural(long_notes, 'note')} {'is' if long_notes == 1 else 'are'} "
            "longer than one passage; split them so meaning search reads all of each: "
            f"onefind rebuild-vectors{opts} --chunking default"
        )
    return ""


def cmd_rebuild_vectors(args: argparse.Namespace) -> int:
    """Re-split long notes into passages and rebuild their vectors."""
    target = _resolve_target(args, must_exist=True, allow_active=True)
    name = target.name or _name_for_db(args, target.db)
    opts = _target_opts(target, name)
    printer: ProgressPrinter | _Silent = (
        _Silent() if getattr(args, "quiet", False) else ProgressPrinter()
    )
    cancel = threading.Event()
    with _open_index(target) as index:
        try:
            with _ctrl_c_cancels(cancel, "rebuilding vectors"):
                if index.has_vectors():
                    if not index.vec_ready:
                        raise EnvError(
                            "sqlite-vec cannot be loaded here, so the vectors of "
                            f"{_label(target)} cannot be rebuilt; run 'onefind check' for the fix"
                        )
                    from .embed import DEFAULT_MODEL

                    model_name = index.get_meta("model_name") or DEFAULT_MODEL
                    print(f"loading embedding model: {model_name} ...")
                    embedder = _load_embedder(model_name)
                    if cancel.is_set():  # Ctrl+C while the model was loading
                        raise OperationCancelled("rebuilding vectors was cancelled")
                    index.attach_embedder(embedder, progress=printer, cancel=cancel.is_set)
                if args.chunking != "keep":
                    setting = index.set_chunking(None if args.chunking == "off" else "default")
                    print(
                        "passage search: "
                        + (f"on ({setting['max_words']} words per passage)" if setting else "off")
                    )
                result = index.rebuild_vectors(progress=printer, cancel=cancel.is_set)
        except (OperationCancelled, KeyboardInterrupt):
            printer.finish()
            _note(
                "cancelled: rebuilding vectors stopped early. Notes finished so far keep their "
                f"new passages; run 'onefind rebuild-vectors{opts}' again to finish."
            )
            return EXIT_CANCELLED
        except BaseException:
            printer.finish()
            raise
        printer.finish()
        rebuilt = int(result.get("rebuilt") or 0)
        config = result.get("chunking")
        if rebuilt:
            vectors = (
                f", {_plural(int(result.get('vectors_embedded') or 0), 'vector')} built"
                if index.has_vectors()
                else " (keyword-only collection: no vectors to build)"
            )
            print(
                f"rebuilt {_plural(rebuilt, 'note')} into "
                f"{_plural(int(result.get('passages') or 0), 'passage')}{vectors} -> {_label(target)}"
            )
        else:
            print(f"vectors are up to date: no note needed new passages ({_label(target)})")
        if result.get("skipped"):
            _note(
                f"note: {_plural(int(result['skipped']), 'note')} changed while rebuilding "
                "and already use the current setting"
            )
        print(
            "  passages : "
            + (
                f"on ({config['max_words']} words, {config['overlap_words']} overlap)"
                if config
                else "off (one vector per note)"
            )
        )
    return EXIT_OK


# -- folders (the collection's registered roots) --------------------------------------------


def _find_folder(index, wanted: str) -> dict:
    """A registered folder by id (as listed) or path; DataError (exit 4) when unknown."""
    text = str(wanted).strip()
    roots = index.roots()
    if text.isdigit():
        for root in roots:
            if root["root_id"] == int(text):
                return root
    candidates = [text]
    try:
        candidates.append(str(Path(text).expanduser().resolve()))
    except (OSError, RuntimeError, ValueError):
        pass

    def key(value: str) -> str:
        cleaned = value.replace("\\", "/").rstrip("/")
        return cleaned.casefold() if sys.platform == "win32" else cleaned

    for candidate in candidates:
        for root in roots:
            if key(root["path"]) == key(candidate):
                return root
    listed = ", ".join(f"{root['root_id']}: {root['path']}" for root in roots) or "none"
    raise DataError(
        f"{text!r} is not a folder of this collection (folders: {listed}); "
        "see 'onefind folders list'"
    )


def cmd_folders_list(args: argparse.Namespace) -> int:
    target = _resolve_target(args, must_exist=True, allow_active=True)
    name = target.name or _name_for_db(args, target.db)
    opts = _target_opts(target, name)
    with _open_index(target) as index:
        roots = index.roots()
    from .watcher import is_network_root

    for root in roots:
        network = is_network_root(root["path"])
        root["network"] = network
        # Never touch a network share just to list folders.
        root["exists"] = None if network else Path(root["path"]).is_dir()
    if getattr(args, "json", False):
        print(json.dumps({"collection": name, "folders": roots}, indent=2))
        return EXIT_OK
    if not roots:
        print(f"{_label(target)} has no folders yet; add one with: onefind index FOLDER{opts}")
        return EXIT_OK
    print(f"folders of {name or target.db.name}:")
    for root in roots:
        state = (
            "network folder (not checked)"
            if root["network"]
            else "missing - moved or deleted?"
            if not root["exists"]
            else "ok"
        )
        print(f"  [{root['root_id']}] {root['path']}")
        details = [
            _plural(root["documents"], "note"),
            f"indexed {_when(root['last_indexed_at'])}",
            state,
        ]
        if root.get("files_only"):
            details.append("single files only")
        print(f"      {', '.join(details)}")
        if root["exclude"]:
            print(f"      excluding: {', '.join(root['exclude'])}")
    missing = [root for root in roots if root["exists"] is False]
    if missing:
        print(
            "a folder moved? point it at its new place (notes keep their ids): "
            f"onefind folders relocate OLD NEW{opts}"
        )
        print(f"gone for good? forget it: onefind folders remove FOLDER{opts}")
    return EXIT_OK


def cmd_folders_remove(args: argparse.Namespace) -> int:
    target = _resolve_target(args, must_exist=True, allow_active=True)
    name = target.name or _name_for_db(args, target.db)
    opts = _target_opts(target, name)
    keep = bool(getattr(args, "keep_documents", False))
    with _open_index(target) as index:
        root = _find_folder(index, args.folder)
        report = index.forget_root(root["root_id"], delete_documents=not keep)
        total = index.count("documents")
    if keep:
        print(
            f"forgot the folder {root['path']}; its {_plural(root['documents'], 'note')} "
            "stay searchable but are no longer updated"
        )
    else:
        print(
            f"removed the folder {root['path']} and its {_plural(report['removed'], 'note')} "
            "from the index; files on disk were not touched"
        )
    print(f"  collection : {name or target.db.name} now holds {_plural(total, 'note')}")
    print(f"  add it back: onefind index {_quote(root['path'])}{opts}")
    return EXIT_OK


def cmd_folders_relocate(args: argparse.Namespace) -> int:
    target = _resolve_target(args, must_exist=True, allow_active=True)
    name = target.name or _name_for_db(args, target.db)
    opts = _target_opts(target, name)
    new = Path(args.new).expanduser()
    try:
        new = new.resolve()
    except (OSError, RuntimeError):
        pass
    if not new.is_dir():
        raise DataError(
            f"the folder {new} does not exist; give the folder the notes were moved to"
        )
    with _open_index(target) as index:
        root = _find_folder(index, args.old)
        updated = index.relocate_root(root["root_id"], str(new))
    print(f"moved folder [{updated['root_id']}]: {root['path']} -> {updated['path']}")
    print(
        f"  {_plural(updated['documents'], 'note')} keep their ids, bookmarks and vectors; "
        f"pick up edits made meanwhile with: onefind index {_quote(updated['path'])}{opts} --prune"
    )
    return EXIT_OK


# -- serve ---------------------------------------------------------------------------


# -- watch (auto-sync in the foreground) -------------------------------------------------


class _WatchPrinter:
    """Turns auto-sync events into one concise, timestamped line each."""

    def __init__(self, out: TextIO | None = None) -> None:
        self.out = out
        self.lock = threading.Lock()

    def line(self, text: str) -> None:
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        with self.lock:
            print(f"{stamp}  {text}", file=self.out or sys.stdout, flush=True)

    def __call__(self, event: dict) -> None:
        kind = event.get("type")
        folder = Path(str(event.get("root") or "")).name or str(event.get("root") or "")
        if kind == "changes":
            parts = [
                f"{event.get(key, 0)} {key}" for key in ("new", "changed", "deleted")
                if event.get(key)
            ]
            self.line(f"{folder}: {', '.join(parts) or 'changes'} - syncing")
        elif kind == "synced":
            counts = ", ".join(
                f"{event.get(key, 0)} {key}" for key in ("added", "updated", "removed")
            )
            self.line(f"{folder}: synced ({counts}) in {event.get('seconds', 0):.1f} s")
        elif kind in {"paused", "error"} and event.get("message"):
            self.line(f"{kind}: {event['message']}")


def cmd_watch(args: argparse.Namespace) -> int:
    """Keep a collection's index up to date until Ctrl+C (or --duration)."""
    from .jobs import JobManager
    from .watcher import AutoSync, get_preference, is_network_root, set_preference

    target = _resolve_target(args, must_exist=True, allow_active=True)
    name = target.name or _name_for_db(args, target.db)
    opts = _target_opts(target, name)
    with _open_index(target) as index:  # a normal open upgrades older files first
        if args.set is not None:
            set_preference(index, args.set == "on")
            print(
                f"auto-sync {args.set} for {_label(target)}"
                + (" - 'onefind serve' keeps it up to date" if args.set == "on" else "")
            )
            return EXIT_OK
        roots = index.roots()
        saved = get_preference(index)
    if not roots:
        raise DataError(
            f"{_label(target)} has no folders to watch yet; index one first: "
            f"onefind index FOLDER{opts}"
        )
    if args.interval is not None and not args.interval > 0:
        raise UsageError("--interval must be greater than 0 seconds")
    if args.duration is not None and args.duration < 0:
        raise UsageError("--duration cannot be negative")
    network = [root["path"] for root in roots if is_network_root(root["path"])]
    printer = _WatchPrinter()
    jobs = JobManager()
    sync = AutoSync(
        jobs, interval=args.interval, allow_network=args.allow_network, on_event=printer
    )
    watched = len(roots) - (0 if args.allow_network else len(network))
    print(
        f"watching {_label(target)}: {_plural(watched, 'folder')}, checking every "
        f"{sync.interval:g} s. Press Ctrl+C to stop."
    )
    if network and not args.allow_network:
        _note(
            f"skipping {_plural(len(network), 'network folder')} (pass --allow-network to "
            "poll folders on other computers)"
        )
    if not saved:
        _note(
            "note: auto-sync is off for this collection in the web app; "
            f"'onefind watch --set on{opts}' turns it on there too"
        )
    stop = threading.Event()
    deadline = time.monotonic() + args.duration if args.duration is not None else None
    code = EXIT_OK
    with _ctrl_c_cancels(stop, "watching"):
        try:
            sync.start(target.db)
            while not stop.is_set():
                if deadline is not None and time.monotonic() >= deadline:
                    break
                if not sync.running(target.db):
                    status = sync.status(target.db)
                    _note(f"stopped: {status.get('message') or 'the collection is gone'}")
                    code = EXIT_DATA
                    break
                wait = 0.25 if deadline is None else max(0.0, min(0.25, deadline - time.monotonic()))
                stop.wait(wait)
        finally:
            sync.shutdown()
            jobs.shutdown()
    print("stopped watching")
    return code


def _accepts(function: Callable[..., Any], *names: str) -> bool:
    import inspect

    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return True
    return all(name in parameters for name in names)


_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")
_ALL_INTERFACES = ("0.0.0.0", "::", "[::]")


def _host_patterns(value: str) -> list[str]:
    """Host-header patterns for a host name or IP (port and brackets handled).

    Starlette compares the Host header's host part as sent, which keeps the
    brackets of an IPv6 literal ('[::1]'), so both spellings are listed.
    """
    import ipaddress

    text = value.strip()
    if text == "*":
        return ["*"]
    if text.startswith("["):
        inner = text[1:].split("]", 1)[0]
        return [inner, f"[{inner}]"] if inner else []
    if text.count(":") == 1:  # name:port or v4:port
        text = text.split(":", 1)[0]
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return [text.lower()] if text else []
    if address.version == 6:
        return [text, f"[{text}]"]
    return [text]


def _starlette_keeps_ipv6_brackets() -> bool:
    """True when TrustedHostMiddleware can match '[::1]:8080' (parse_host_header)."""
    try:
        from starlette.middleware import trustedhost
    except ImportError:  # pragma: no cover - serve extra missing
        return True
    return hasattr(trustedhost, "parse_host_header")


def _allowed_hosts(args: argparse.Namespace) -> list[str]:
    """Host headers the server accepts (blocks DNS rebinding).

    Loopback binds accept only loopback names. A remote bind adds the bind
    address and every --allowed-host; binding to all interfaces needs at
    least one --allowed-host. '*' (no check) is only used when given
    explicitly as --allowed-host '*'.
    """
    patterns: list[str] = []
    for host in _LOOPBACK_HOSTS:
        patterns += _host_patterns(host)
    if args.host not in _LOOPBACK_HOSTS:
        extra = list(getattr(args, "allowed_host", None) or [])
        if args.host in _ALL_INTERFACES:
            if not extra:
                raise UsageError(
                    f"binding to all interfaces ({args.host}) needs --allowed-host NAME "
                    "(the host name or IP clients will use, repeatable); the Host header "
                    "is checked to block DNS rebinding"
                )
        else:
            patterns += _host_patterns(args.host)
        for value in extra:
            patterns += _host_patterns(value)
    return list(dict.fromkeys(pattern for pattern in patterns if pattern))


def cmd_serve(args: argparse.Namespace) -> int:
    remote = args.host not in _LOOPBACK_HOSTS
    if remote and not args.allow_remote:
        raise UsageError(
            f"refusing to expose the unauthenticated demo on {args.host}; "
            "use --allow-remote only behind a trusted firewall or reverse proxy"
        )
    allowed_hosts = _allowed_hosts(args)
    if args.host == "::1" and not _starlette_keeps_ipv6_brackets():
        raise UsageError(
            "this Starlette version cannot match an IPv6 Host header ([::1]); "
            "use --host 127.0.0.1 or upgrade: pip install -U fastapi"
        )
    try:
        import uvicorn
    except ImportError as exc:
        raise UsageError('serve needs the [serve] extra - pip install -e ".[serve]"') from exc

    from .serve import create_app

    db = getattr(args, "db", None)
    home = getattr(args, "home", None)
    # A remote bind never launches files on this machine: a proxied or
    # rebound request can look like it comes from loopback.
    no_open = bool(getattr(args, "no_open", False)) or remote
    # Only pass the v1.2 keywords when they matter, so an older create_app
    # keeps working for the classic `serve --db FILE` call.
    extra: dict[str, Any] = {}
    if db is None or home is not None:
        extra["home"] = home
    if no_open:
        extra["allow_open"] = False
    if getattr(args, "no_auto_sync", False):
        extra["autosync"] = False
    if extra and not _accepts(create_app, *extra):
        raise UsageError(
            "this build's web app needs --db FILE (library mode, --home, --no-open and "
            "--allow-remote are not available yet)"
        )
    app = create_app(
        db,
        reset_token=args.reset_token,
        allowed_hosts=allowed_hosts,
        **extra,
    )
    if db is not None:
        where = f"db: {db}"
    else:
        from .library import Library

        where = f"library: {Library(home).home}"
    shown = f"[{args.host}]" if ":" in args.host else args.host
    print(f"onefind serving on http://{shown}:{args.port} ({where})")
    if remote:
        _note(
            "note: remote mode - opening files from the web app is off; accepted Host "
            f"names: {', '.join(allowed_hosts)}"
        )
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return EXIT_OK


def _not_yet(phase_hint: str) -> int:
    print(f"not implemented yet - planned in docs/06-engineering-plan.md ({phase_hint})")
    return EXIT_USAGE


# -- parser ----------------------------------------------------------------------------


def _add_target_args(parser: argparse.ArgumentParser, *, default_note: str) -> None:
    parser.add_argument(
        "--db", default=None,
        help=f"SQLite collection file (default {DEFAULT_DB}{default_note})",
    )
    parser.add_argument(
        "--collection", default=None, metavar="NAME",
        help="named collection from 'onefind collections' (--db wins if both are given)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="onefind", description=__doc__)
    parser.add_argument("--version", action="version", version=f"onefind {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    fallback = "; falls back to the active collection when it does not exist"

    p_check = sub.add_parser("check", help="verify SQLite FTS5 + sqlite-vec + model stack")
    p_check.add_argument("--json", action="store_true", help="machine-readable report")
    p_check.add_argument(
        "--full", action="store_true", help="also download/load the embedding model (network)"
    )
    p_check.set_defaults(func=cmd_check)

    p_index = sub.add_parser("index", help="index a folder of .txt/.md files into a database")
    p_index.add_argument("path", help="corpus folder")
    _add_target_args(p_index, default_note="; --collection creates the collection if needed")
    p_index.add_argument(
        "--embed",
        action="store_true",
        help="also compute embeddings (float+int8+bit vectors; needs [model] extra)",
    )
    p_index.add_argument("--model", default=None, help="embedding model name (default MiniLM)")
    p_index.add_argument(
        "--exclude", action="append", default=None, metavar="PATTERN",
        help="skip files/folders matching this glob (repeatable; remembered per folder - "
             "the patterns given replace the ones remembered for this folder)",
    )
    p_index.add_argument(
        "--prune", action="store_true",
        help="remove notes whose files were deleted or are now excluded",
    )
    p_index.add_argument(
        "--full", action="store_true",
        help="re-read and rewrite every file instead of skipping unchanged ones",
    )
    p_index.add_argument(
        "-q", "--quiet", action="store_true", help="no live progress on stderr"
    )
    p_index.set_defaults(func=cmd_index)

    p_search = sub.add_parser("search", help="query an index")
    p_search.add_argument("query")
    _add_target_args(p_search, default_note=fallback)
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
    p_search.add_argument(
        "--match", choices=["all", "any", "auto"], default=None,
        help="how keywords combine: all words, any word, or auto (all-words hits first, "
             "then partial matches; the default for lexical/hybrid, same as the app)",
    )
    p_search.add_argument("--k", type=int, default=10, help="number of results (1..50)")
    p_search.add_argument(
        "--folder", action="append", default=None, metavar="FOLDER",
        help="only notes in this folder or below (repeatable; '' = top level)",
    )
    p_search.add_argument(
        "--type", action="append", default=None, metavar="EXT",
        help="only this file type, e.g. md or .txt (repeatable)",
    )
    p_search.add_argument(
        "--tag", action="append", default=None, metavar="TAG",
        help="only notes with this front-matter tag (repeatable, any of)",
    )
    p_search.add_argument(
        "--since", default=None, metavar="YYYY-MM-DD",
        help="only notes modified on or after this date",
    )
    p_search.add_argument(
        "--explain", action="store_true", help="print why each result matched"
    )
    p_search.set_defaults(func=cmd_search)

    p_collections = sub.add_parser(
        "collections", help="list, create, switch, rename, or remove named collections"
    )
    c_sub = p_collections.add_subparsers(dest="collections_command", required=True)
    c_list = c_sub.add_parser("list", help="show all collections (* = active)")
    c_list.add_argument("--json", action="store_true", help="machine-readable list")
    c_list.set_defaults(func=cmd_collections_list)
    c_create = c_sub.add_parser("create", help="create a collection (or add an existing .db)")
    c_create.add_argument("name")
    c_create.add_argument(
        "--db", default=None,
        help="where to keep the file (an existing OneFind .db is added as-is)",
    )
    c_create.set_defaults(func=cmd_collections_create)
    c_use = c_sub.add_parser("use", help="make a collection the active one")
    c_use.add_argument("name")
    c_use.set_defaults(func=cmd_collections_use)
    c_remove = c_sub.add_parser("remove", help="forget a collection (the file is kept)")
    c_remove.add_argument("name")
    c_remove.add_argument(
        "--delete-file", action="store_true", dest="delete_file",
        help="also delete the collection file (needs --yes)",
    )
    c_remove.add_argument("--yes", action="store_true", help="confirm --delete-file")
    c_remove.set_defaults(func=cmd_collections_remove)
    c_rename = c_sub.add_parser("rename", help="rename a collection")
    c_rename.add_argument("name")
    c_rename.add_argument("new_name", metavar="NEW_NAME")
    c_rename.set_defaults(func=cmd_collections_rename)

    p_status = sub.add_parser(
        "status", help="collection health, changed files per folder, and the last run"
    )
    _add_target_args(p_status, default_note=fallback)
    p_status.add_argument(
        "--no-scan", action="store_true", dest="no_scan",
        help="skip checking folders for new/changed/deleted files",
    )
    p_status.add_argument("--json", action="store_true", help="machine-readable status")
    p_status.set_defaults(func=cmd_status)

    p_backup = sub.add_parser("backup", help="write a safe single-file copy of a collection")
    _add_target_args(p_backup, default_note=fallback)
    p_backup.add_argument(
        "--out", default=None, metavar="PATH",
        help="backup file, or a folder (existing, ending in a slash, or a new path with no "
             "extension) (default: the library backups folder)",
    )
    p_backup.add_argument(
        "--overwrite", action="store_true", help="replace --out if it already exists"
    )
    p_backup.add_argument(
        "--list", action="store_true", help="list existing backups instead of writing one"
    )
    p_backup.set_defaults(func=cmd_backup)

    p_restore = sub.add_parser("restore", help="restore a backup as a new named collection")
    p_restore.add_argument("backup", help="backup file (or its name in the backups folder)")
    p_restore.add_argument(
        "--as", required=True, dest="as_name", metavar="NAME",
        help="name of the new collection",
    )
    p_restore.set_defaults(func=cmd_restore)

    p_doctor = sub.add_parser("doctor", help="check a collection for damage; --repair fixes it")
    _add_target_args(p_doctor, default_note=fallback)
    p_doctor.add_argument(
        "--repair", action="store_true",
        help="rebuild the keyword index, drop leftovers, re-embed missing vectors",
    )
    p_doctor.set_defaults(func=cmd_doctor)

    p_compact = sub.add_parser("compact", help="reclaim unused space in a collection file")
    _add_target_args(p_compact, default_note=fallback)
    p_compact.set_defaults(func=cmd_compact)

    p_remove = sub.add_parser(
        "remove", help="remove documents from the index (files on disk are untouched)"
    )
    p_remove.add_argument("doc_ids", nargs="+", metavar="DOC_ID")
    _add_target_args(p_remove, default_note=fallback)
    p_remove.set_defaults(func=cmd_remove)

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
    p_smoke.add_argument(
        "--match", choices=["all", "any", "auto"], default="auto",
        help="keyword matching for lexical/hybrid cases (default auto, the same as the app; "
             "semantic cases always use all)",
    )
    p_smoke.set_defaults(func=cmd_smoke)

    p_serve = sub.add_parser("serve", help="run the local web app")
    p_serve.add_argument(
        "--db", default=None,
        help="SQLite index file to serve (default: the library's active collection)",
    )
    p_serve.add_argument(
        "--home", default=None,
        help="library folder holding collections.json (default $ONEFIND_HOME or the "
             "per-user data folder)",
    )
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8080)
    p_serve.add_argument(
        "--allow-remote",
        action="store_true",
        help="permit a non-loopback bind for the unauthenticated demo",
    )
    p_serve.add_argument(
        "--allowed-host", action="append", default=[], dest="allowed_host", metavar="NAME",
        help="Host header value to accept when serving remotely (repeatable; e.g. the "
             "proxy's public name or this machine's LAN IP); required with 0.0.0.0",
    )
    p_serve.add_argument(
        "--no-open", action="store_true", dest="no_open",
        help="disable opening/revealing files from the web app",
    )
    p_serve.add_argument("--reset-token", default=None, dest="reset_token",
                         help="token required to POST /api/reset, 16-256 characters "
                              "(random if omitted)")
    p_serve.add_argument(
        "--no-auto-sync", action="store_true", dest="no_auto_sync",
        help="never watch folders, even for collections whose auto-sync is on",
    )
    p_serve.add_argument("--log-level", default="warning", dest="log_level")
    p_serve.set_defaults(func=cmd_serve)

    p_watch = sub.add_parser(
        "watch",
        help="keep a collection up to date while notes change (auto-sync in the foreground)",
    )
    _add_target_args(p_watch, default_note=fallback)
    p_watch.add_argument(
        "--interval", type=float, default=None, metavar="SECONDS",
        help="seconds between folder checks (default 5; large folders are checked less often)",
    )
    p_watch.add_argument(
        "--duration", type=float, default=None, metavar="SECONDS",
        help="stop after this many seconds (default: run until Ctrl+C)",
    )
    p_watch.add_argument(
        "--allow-network", action="store_true", dest="allow_network",
        help="also poll folders on other computers (UNC paths, mapped network drives)",
    )
    p_watch.add_argument(
        "--set", choices=["on", "off"], default=None,
        help="only save the collection's auto-sync preference (used by 'onefind serve') and exit",
    )
    p_watch.set_defaults(func=cmd_watch)

    p_rebuild = sub.add_parser(
        "rebuild-vectors",
        help="split long notes into passages and rebuild their vectors (meaning search)",
    )
    _add_target_args(p_rebuild, default_note=fallback)
    p_rebuild.add_argument(
        "--chunking", choices=["keep", "default", "off"], default="keep",
        help="keep the collection's passage setting (default), turn passage search on "
             "(default: 200-word passages), or off (one vector per note)",
    )
    p_rebuild.add_argument(
        "-q", "--quiet", action="store_true", help="no live progress on stderr"
    )
    p_rebuild.set_defaults(func=cmd_rebuild_vectors)

    p_folders = sub.add_parser(
        "folders", help="list, remove or relocate the folders a collection indexes"
    )
    f_sub = p_folders.add_subparsers(dest="folders_command", required=True)
    f_list = f_sub.add_parser("list", help="show the collection's folders and saved exclusions")
    _add_target_args(f_list, default_note=fallback)
    f_list.add_argument("--json", action="store_true", help="machine-readable list")
    f_list.set_defaults(func=cmd_folders_list)
    f_remove = f_sub.add_parser(
        "remove", help="forget a folder (moved or deleted); files on disk are never touched"
    )
    f_remove.add_argument("folder", metavar="FOLDER", help="folder path or its [id] from 'folders list'")
    _add_target_args(f_remove, default_note=fallback)
    f_keep = f_remove.add_mutually_exclusive_group()
    f_keep.add_argument(
        "--delete-documents", action="store_false", dest="keep_documents",
        help="also remove the folder's notes from the index (default)",
    )
    f_keep.add_argument(
        "--keep-documents", action="store_true", dest="keep_documents",
        help="keep the folder's notes searchable (they are no longer updated)",
    )
    f_remove.set_defaults(func=cmd_folders_remove, keep_documents=False)
    f_relocate = f_sub.add_parser(
        "relocate", help="point a moved folder at its new place (notes keep their ids)"
    )
    f_relocate.add_argument("old", metavar="OLD", help="the folder's old path or its [id]")
    f_relocate.add_argument("new", metavar="NEW", help="where the folder is now")
    _add_target_args(f_relocate, default_note=fallback)
    f_relocate.set_defaults(func=cmd_folders_relocate)

    return parser


def _tolerant_streams() -> None:
    """Never crash on a note title the console encoding cannot show."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(Exception):
                reconfigure(errors="replace")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _tolerant_streams()
    try:
        return args.func(args)
    except OperationCancelled as exc:
        print(f"cancelled: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("cancelled: stopped by Ctrl+C", file=sys.stderr)
        return EXIT_CANCELLED
    except OneFindError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    except sqlite3.Error as exc:
        print(
            f"error: database problem: {exc} (check the file with 'onefind doctor')",
            file=sys.stderr,
        )
        return EXIT_DATA


if __name__ == "__main__":
    sys.exit(main())
