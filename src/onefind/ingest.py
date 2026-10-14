"""Corpus loading for folders, text files, and local JSONL datasets.

Folder ingestion is incremental: every file is hashed and documents whose
stored ``content_hash`` matches are left alone. Runs are recorded in the
index (``index_runs``) and folders are registered as collection roots so the
app can rescan them later (see docs/11-implementation-contract.md section 2).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path, PurePosixPath

from .errors import CorpusNotFoundError, DataError, OperationCancelled, UsageError
from .store import BATCH_SIZE, Document, Index, normalize_tags

SUPPORTED_SUFFIXES = {".txt", ".md", ".markdown", ".rst", ".org", ".text"}
SUPPORTED_FILES = SUPPORTED_SUFFIXES | {".jsonl"}
DEFAULT_EXCLUDES = [".*", "node_modules", "__pycache__", ".venv", "venv"]
MAX_FILE_BYTES = 5 * 1024 * 1024
BINARY_SNIFF_BYTES = 8192
MAX_TAGS = 50
MAX_TAG_CHARS = 64
# Front matter must close within this many lines to count as front matter.
_FRONT_MATTER_MAX_LINES = 200

SKIP_TOO_LARGE = "too large"
SKIP_BINARY = "binary"
SKIP_EMPTY = "empty"
SKIP_UNREADABLE = "unreadable"

ProgressCallback = Callable[[dict], None]
CancelCallback = Callable[[], bool]

_FM_KEY = re.compile(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$")
_FM_ITEM = re.compile(r"^\s*-\s*(.*)$")


# -- text helpers ---------------------------------------------------------------


def _split_title(text: str, stem: str) -> tuple[str, str]:
    lines = text.splitlines()
    for position, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("# "):
            return stripped[2:].strip(), "\n".join(lines[position + 1 :]).strip()
        break
    return stem, text.strip()


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1].strip()
    return value


def _clean_tags(values: Iterable[str]) -> list[str]:
    cleaned: list[str] = []
    for value in values:
        tag = _unquote(value).lstrip("#").strip()
        if tag and len(tag) <= MAX_TAG_CHARS:
            cleaned.append(tag)
    return normalize_tags(cleaned)[:MAX_TAGS]


def parse_front_matter(text: str) -> tuple[dict, str]:
    """Split a leading ``---`` YAML-style block from ``text``.

    Returns ``({"title": str | None, "tags": list[str]}, body)``. Only a small,
    safe subset is understood (``key: value`` and ``- item`` lines); anything
    that does not look like front matter is left in the body untouched.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = None
    for position in range(1, min(len(lines), _FRONT_MATTER_MAX_LINES + 1)):
        if lines[position].strip() in {"---", "..."}:
            end = position
            break
    if end is None:
        return {}, text

    fields: dict[str, dict] = {}
    current: str | None = None
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key_match = _FM_KEY.match(line)
        if key_match and not line[0].isspace():
            current = key_match.group(1).lower()
            fields[current] = {"inline": key_match.group(2).strip(), "items": []}
            continue
        item_match = _FM_ITEM.match(line)
        if item_match and current is not None:
            fields[current]["items"].append(item_match.group(1))
            continue
        if line[0].isspace() and current is not None:
            continue  # nested value we do not interpret
        return {}, text  # e.g. a thematic break followed by prose

    title_field = fields.get("title")
    title = _unquote(title_field["inline"]) if title_field else ""
    tags: list[str] = []
    for key in ("tags", "tag", "keywords"):
        field = fields.get(key)
        if not field:
            continue
        inline = field["inline"]
        if inline.startswith("[") and inline.endswith("]"):
            inline = inline[1:-1]
        values = [part for part in inline.split(",")] if inline else []
        tags = _clean_tags([*values, *field["items"]])
        break
    return {"title": title or None, "tags": tags}, "\n".join(lines[end + 1 :])


def _decode(raw: bytes) -> str | None:
    """Decode file bytes; None means the file is binary."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            text = raw.decode("utf-16")
        except UnicodeDecodeError:
            return None
    elif b"\x00" in raw[:BINARY_SNIFF_BYTES]:
        return None
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            # Not UTF-8: most likely Windows "ANSI" (classic Notepad). cp1252
            # leaves five bytes undefined; latin-1 never fails, so no
            # character is ever replaced by U+FFFD.
            try:
                text = raw.decode("cp1252")
            except UnicodeDecodeError:
                text = raw.decode("latin-1")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def _relative_doc_id(relative_path: Path) -> str:
    """Use the canonical relative filename so every source path is unique."""
    return relative_path.as_posix()


class _DocIdAssigner:
    """Give one root's files doc_ids that never overwrite another root's notes.

    A file keeps the doc_id it already has in this root. Otherwise it gets its
    relative path, unless that id belongs to a different, still-registered
    root (two folders both holding ``readme.md``); then it gets the qualified
    id ``~<root_id>/<relative>``. Documents with no root (pre-v3 or JSONL)
    are taken over, which keeps re-indexing a migrated folder in place.
    """

    def __init__(self, manifest: dict[str, dict], root_id: int, registered: set[int]) -> None:
        self.manifest = manifest
        self.root_id = root_id
        self.registered = registered
        self.owned = {
            str(meta["source"]): doc_id
            for doc_id, meta in manifest.items()
            if meta["root_id"] == root_id and meta["source"]
        }

    @classmethod
    def for_root(cls, index: Index, manifest: dict[str, dict], root_id: int) -> _DocIdAssigner:
        return cls(manifest, root_id, {int(root["root_id"]) for root in index.roots()})

    def __call__(self, relative: str) -> str:
        current = self.owned.get(relative)
        if current is not None:
            return current
        stored = self.manifest.get(relative)
        if stored is None:
            return relative
        owner = stored["root_id"]
        if owner is None or owner == self.root_id or owner not in self.registered:
            return relative
        return qualified_doc_id(self.root_id, relative)


def qualified_doc_id(root_id: int, relative: str) -> str:
    """The doc_id used when ``relative`` is already taken by another root."""
    return f"~{int(root_id)}/{relative}"


def _folder_of(relative: str) -> str:
    parent = PurePosixPath(relative).parent.as_posix()
    return "" if parent == "." else parent


# -- exclusion & discovery --------------------------------------------------------


def normalize_excludes(exclude: Iterable[str] | str | None) -> list[str]:
    """Validate user exclude globs and normalize them to posix form."""
    if exclude is None:
        return []
    if isinstance(exclude, str):
        exclude = [exclude]
    patterns: list[str] = []
    for pattern in exclude:
        if not isinstance(pattern, str):
            raise UsageError("exclude patterns must be strings")
        cleaned = pattern.strip().replace("\\", "/")
        while cleaned.startswith("./"):
            cleaned = cleaned[2:]
        cleaned = cleaned.strip("/")
        if cleaned and cleaned not in patterns:
            patterns.append(cleaned)
    return patterns


def is_excluded(relative: str, patterns: Iterable[str]) -> bool:
    """True when a glob matches the relative path, a component, or a parent dir.

    Matching is case-insensitive so the same rules work on every OS.
    """
    rel = relative.replace("\\", "/").strip("/").lower()
    if not rel:
        return False
    parts = rel.split("/")
    candidates = [rel, *parts, *("/".join(parts[:end]) for end in range(2, len(parts)))]
    for pattern in patterns:
        lowered = pattern.lower()
        if any(fnmatch.fnmatchcase(candidate, lowered) for candidate in candidates):
            return True
    return False


def list_files(
    root: Path,
    patterns: Iterable[str] = (),
    *,
    limit: int | None = None,
    on_directory: Callable[[int], None] | None = None,
    prefix: str = "",
    unreadable: list[str] | None = None,
) -> tuple[list[tuple[str, Path]], bool]:
    """Supported, non-excluded files under ``root`` as sorted (relative, path).

    Excluded directories are never descended into. Returns ``(files,
    truncated)`` where ``truncated`` is True when ``limit`` was exceeded.

    ``prefix`` (posix) is prepended to every relative path, so a sub-folder
    of a registered root is listed (and excluded) relative to that root.
    Folders that could not be listed are appended to ``unreadable`` (when
    given) as relative paths; ``prefix`` itself (``""`` without one) means
    ``root`` could not be listed at all. Their contents are unknown, so
    callers must not treat their notes as deleted.
    """
    patterns = list(patterns)
    lead = prefix.strip("/")
    found: list[tuple[str, Path]] = []
    truncated = False

    def _join(relative: str) -> str:
        if not lead:
            return relative
        return f"{lead}/{relative}" if relative else lead

    def _onerror(error: OSError) -> None:
        if unreadable is None:
            return
        name = getattr(error, "filename", None)
        relative = ""
        if name:
            try:
                relative = Path(name).relative_to(root).as_posix()
            except ValueError:
                relative = ""
        unreadable.append(_join("" if relative == "." else relative))

    for dirpath, dirnames, filenames in os.walk(root, onerror=_onerror):
        if on_directory is not None:
            on_directory(len(found))
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        rel_dir = _join("" if rel_dir == "." else rel_dir)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not is_excluded(f"{rel_dir}/{name}" if rel_dir else name, patterns)
        )
        for name in sorted(filenames):
            if os.path.splitext(name)[1].lower() not in SUPPORTED_SUFFIXES:
                continue
            relative = f"{rel_dir}/{name}" if rel_dir else name
            if is_excluded(relative, patterns):
                continue
            full = Path(dirpath) / name
            if not full.is_file():
                continue
            found.append((relative, full))
            if limit is not None and len(found) > limit:
                truncated = True
                break
        if truncated:
            break
    found.sort(key=lambda item: item[0])
    if truncated and limit is not None:
        found = found[:limit]
    return found, truncated


def _under_any(relative: str, folders: Iterable[str]) -> bool:
    """True when ``relative`` lies in one of ``folders`` (``""`` = everything)."""
    return any(
        folder == "" or relative == folder or relative.startswith(folder + "/")
        for folder in folders
    )


def _norm_key(path: str | Path) -> str:
    return os.path.normcase(str(path))


def _relative_below(target: Path, base: str) -> str | None:
    """``target`` relative to ``base`` (posix, "" when equal), or None if outside.

    Case-insensitive on Windows, like the file system.
    """
    target_key, base_key = _norm_key(target), _norm_key(base)
    if target_key == base_key:
        return ""
    lead = base_key if base_key.endswith(("\\", "/")) else base_key + os.sep
    if not target_key.startswith(lead):
        return None
    rest = str(target)[len(lead):]
    return rest.replace(os.sep, "/").strip("/")


def _covering_root(index: Index, target: Path) -> tuple[dict, str] | None:
    """The registered root that is ``target`` or its nearest ancestor.

    Returns ``(root, prefix)`` where ``prefix`` is ``target`` relative to the
    root in posix form (``""`` when ``target`` is the root itself).
    """
    best: tuple[dict, str] | None = None
    for root in index.roots():
        relative = _relative_below(target, root["path"])
        if relative is None:
            continue
        if best is None or len(_norm_key(root["path"])) > len(_norm_key(best[0]["path"])):
            best = (root, relative)
    return best


def _nested_roots(index: Index, target: Path) -> list[tuple[dict, str]]:
    """Registered roots strictly inside ``target`` with their prefixes."""
    nested = []
    for root in index.roots():
        relative = _relative_below(Path(root["path"]), str(target))
        if relative:
            nested.append((root, relative))
    return nested


def iter_files(root: Path, exclude: Iterable[str] | None = None) -> Iterator[Path]:
    """Supported files under ``root`` (default excludes always apply)."""
    files, _truncated = list_files(Path(root), [*DEFAULT_EXCLUDES, *normalize_excludes(exclude)])
    for _relative, path in files:
        yield path


# -- loading ---------------------------------------------------------------------


def read_text_document(
    path: Path,
    relative: str,
    *,
    root_id: int | None = None,
) -> tuple[Document | None, str | None]:
    """Load one text file as a Document, or return ``(None, skip_reason)``."""
    try:
        stat = path.stat()
        if stat.st_size > MAX_FILE_BYTES:
            return None, SKIP_TOO_LARGE
        raw = path.read_bytes()
    except OSError:
        return None, SKIP_UNREADABLE
    if not raw.strip():
        return None, SKIP_EMPTY
    text = _decode(raw)
    if text is None:
        return None, SKIP_BINARY

    front, remainder = parse_front_matter(text)
    if not remainder.strip():
        return None, SKIP_EMPTY
    stem = PurePosixPath(relative).stem
    if front.get("title"):
        title, body = str(front["title"]), remainder.strip()
    else:
        title, body = _split_title(remainder, stem)
    return (
        Document(
            doc_id=_relative_doc_id(PurePosixPath(relative)),
            body=body,
            title=title,
            source=relative,
            content_hash=hashlib.sha256(raw).hexdigest(),
            mtime=stat.st_mtime,
            size_bytes=stat.st_size,
            ext=PurePosixPath(relative).suffix.lower(),
            folder=_folder_of(relative),
            tags=front.get("tags") or None,
            root_id=root_id,
        ),
        None,
    )


def _load_text_file(path: Path, root: Path) -> Document:
    """Backward-compatible loader for one file relative to ``root``."""
    relative = path.relative_to(root).as_posix()
    document, reason = read_text_document(path, relative)
    if document is None:
        raise DataError(f"{relative} was skipped: {reason}")
    return document


def _load_jsonl(path: Path) -> Iterator[Document]:
    seen: set[str] = set()
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DataError(
                    f"invalid JSON in {path} at line {line_number}: {exc.msg}"
                ) from exc
            if not isinstance(row, dict):
                raise DataError(f"expected a JSON object in {path} at line {line_number}")

            raw_id = row.get("_id") or row.get("id")
            doc_id = str(raw_id) if raw_id not in (None, "") else f"line-{line_number}"
            if doc_id in seen:
                raise DataError(f"duplicate document id {doc_id!r} in {path}")
            seen.add(doc_id)

            body_value = row.get("text") or row.get("body") or ""
            body = str(body_value).strip()
            if not body:
                raise DataError(f"document {doc_id!r} in {path} has no text/body content")
            title = str(row.get("title") or doc_id)
            source = str(row.get("source") or f"{path.name}:{doc_id}")
            meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else None
            encoded = body.encode("utf-8")
            yield Document(
                doc_id=doc_id,
                body=body,
                title=title,
                source=source,
                meta=meta,
                content_hash=hashlib.sha256(encoded).hexdigest(),
                size_bytes=len(encoded),
            )


def load_documents(root: Path, exclude: Iterable[str] | None = None) -> Iterator[Document]:
    """Indexable documents under ``root``; skipped files are silently omitted."""
    root = Path(root)
    files, _truncated = list_files(root, [*DEFAULT_EXCLUDES, *normalize_excludes(exclude)])
    for relative, path in files:
        document, _reason = read_text_document(path, relative)
        if document is not None:
            yield document


# -- ingestion session --------------------------------------------------------------


class _Session:
    """Counts, batching, progress, and cancellation for one ingest run."""

    def __init__(
        self,
        index: Index,
        *,
        incremental: bool,
        progress: ProgressCallback | None,
        cancel: CancelCallback | None,
    ) -> None:
        self.index = index
        self.incremental = incremental
        self.progress = progress
        self.cancel = cancel
        self.manifest = index.document_manifest()
        self.added = 0
        self.updated = 0
        self.unchanged = 0
        self.removed = 0
        self.skipped: list[dict] = []
        self.written = 0
        self._pending: list[tuple[Document, str]] = []
        self._touched: list[dict] = []

    @property
    def considered(self) -> int:
        return self.added + self.updated + self.unchanged

    def counts(self) -> dict:
        return {
            "added": self.added,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "removed": self.removed,
            "skipped": len(self.skipped),
        }

    def check_cancel(self) -> None:
        if self.cancel is not None and self.cancel():
            raise OperationCancelled("indexing was cancelled; finished batches were kept")

    def emit(self, phase: str, done: int, total: int | None, message: str) -> None:
        if self.progress is not None:
            self.progress({"phase": phase, "done": done, "total": total, "message": message})

    def skip(self, path: str, reason: str) -> None:
        self.skipped.append({"path": path, "reason": reason})

    def offer(self, document: Document, *, compare_fields: bool = False) -> None:
        """Queue a document for writing unless it is unchanged."""
        stored = self.manifest.get(document.doc_id)
        if stored is None:
            self._queue(document, "added")
            return
        same = (
            self.incremental
            and stored["content_hash"] is not None
            and stored["content_hash"] == document.content_hash
        )
        if same and compare_fields:
            meta_json = (
                json.dumps(document.meta, sort_keys=True) if document.meta else None
            )
            same = (
                stored["title"] == document.title
                and stored["source"] == document.source
                and stored["meta_json"] == meta_json
            )
        if not same:
            self._queue(document, "updated")
            return
        self.unchanged += 1
        if (
            stored["mtime"] != document.mtime
            or stored["size_bytes"] != document.size_bytes
            or stored["root_id"] != document.root_id
        ):
            self._touched.append(
                {
                    "doc_id": document.doc_id,
                    "mtime": document.mtime,
                    "size_bytes": document.size_bytes,
                    "root_id": document.root_id,
                    "folder": document.folder,
                    "ext": document.ext,
                }
            )
            if len(self._touched) >= BATCH_SIZE:
                self._flush_touched()

    def _queue(self, document: Document, kind: str) -> None:
        self._pending.append((document, kind))
        if len(self._pending) >= BATCH_SIZE:
            self.flush()

    def _flush_touched(self) -> None:
        if self._touched:
            self.index.update_metadata(self._touched)
            self._touched = []

    def flush(self) -> None:
        """Write queued documents; counters only move once a batch commits."""
        self._flush_touched()
        if not self._pending:
            return
        self.check_cancel()
        batch, self._pending = self._pending, []
        base = self.written
        if self.index.embedder is not None:
            # Announce the (slow) vector step before the model runs, so
            # progress shows "building vectors" while the batch is encoded.
            self.emit("embed", base, None, f"Building vectors for {len(batch)} notes")

        def relay(event: dict) -> None:
            done = base + int(event["done"])
            verb = "Embedded" if event["phase"] == "embed" else "Saved"
            self.emit(event["phase"], done, None, f"{verb} {done} documents")

        self.index.add_documents([document for document, _kind in batch], progress=relay)
        self.written += len(batch)
        self.added += sum(1 for _document, kind in batch if kind == "added")
        self.updated += sum(1 for _document, kind in batch if kind == "updated")

    def result(self, *, root: str, root_id: int | None, run_id: int, duplicates) -> dict:
        return {
            "files_indexed": self.considered,
            "total_documents": self.index.count("documents"),
            "db": str(self.index.path),
            "root": root,
            "root_id": root_id,
            "run_id": run_id,
            **{key: value for key, value in self.counts().items() if key != "skipped"},
            "skipped": list(self.skipped),
            "duplicates": duplicates,
        }


def _empty_corpus_error(root: Path, session: _Session | None = None) -> CorpusNotFoundError:
    detail = ""
    if session is not None and session.skipped:
        reasons = sorted({item["reason"] for item in session.skipped})
        detail = f"; {len(session.skipped)} file(s) skipped ({', '.join(reasons)})"
    return CorpusNotFoundError(
        f"no readable corpus documents at {root}{detail} "
        f"(.txt/.md files inside folders - also .markdown/.rst/.org/.text - "
        f"standalone .txt/.md files, or .jsonl datasets are supported)"
    )


def _run_bookkeeping(index: Index, run_id: int, session: _Session, exc: BaseException) -> None:
    status = (
        "cancelled" if isinstance(exc, (OperationCancelled, KeyboardInterrupt)) else "failed"
    )
    try:
        if index.conn.in_transaction:
            index.conn.rollback()
        index.finish_run(run_id, status, session.counts(), error=str(exc) or type(exc).__name__)
    except Exception:  # noqa: BLE001 - never mask the original failure
        pass


def _unreadable_error(root: Path) -> DataError:
    error = PermissionError(13, "the folder could not be listed", str(root))
    exc = DataError(f"cannot list {root}: access denied or the folder is unavailable")
    exc.__cause__ = error
    return exc


def _ingest_folder(
    index: Index,
    source: Path,
    *,
    exclude: Iterable[str] | str | None,
    prune: bool,
    incremental: bool,
    progress: ProgressCallback | None,
    cancel: CancelCallback | None,
    refresh_files: bool = False,
) -> dict:
    target = source.resolve()
    cover = _covering_root(index, target)
    if cover is not None and cover[1]:
        # A sub-folder of a registered root: re-index it as part of that root
        # (same doc_ids, no second root, prune limited to the sub-folder).
        existing, prefix = cover
    elif cover is not None:
        existing, prefix = cover[0], ""
    else:
        existing, prefix = None, ""
    if existing is not None and not prefix and existing["files_only"] and refresh_files:
        return _refresh_file_root(
            index, existing, prune=prune, incremental=incremental, progress=progress, cancel=cancel
        )
    subfolder = bool(prefix)
    root_key = existing["path"] if existing is not None else str(target)
    if exclude is None:
        user_patterns = list(existing["exclude"]) if existing else []
        stored_exclude = None
    else:
        user_patterns = normalize_excludes(exclude)
        # A sub-folder run uses its own excludes for this run only.
        stored_exclude = None if subfolder else user_patterns
    patterns = [*DEFAULT_EXCLUDES, *user_patterns]

    adopted = 0
    if not subfolder:
        nested = _nested_roots(index, target)
        if nested:
            # Indexing a parent of registered folders: merge them into this
            # root so no note is ever indexed twice. doc_ids are kept.
            parent_id = index.ensure_root(root_key, stored_exclude, files_only=False)
            for child, child_prefix in nested:
                adopted += 1
                index.adopt_root(child["root_id"], parent_id, child_prefix)
            existing = index.get_root(root_key)

    scope = f"{prefix}/" if subfolder else ""

    def in_scope(source_path: str | None) -> bool:
        return bool(source_path) and str(source_path).startswith(scope)

    if existing is not None:
        stored_sources = {
            doc_id: str(meta["source"] or "")
            for doc_id, meta in index.document_manifest(existing["root_id"]).items()
        }
        had_documents = any(in_scope(value) for value in stored_sources.values())
    else:
        had_documents = False

    session = _Session(index, incremental=incremental, progress=progress, cancel=cancel)
    run_id = index.start_run(root_key)
    root_id: int | None = existing["root_id"] if existing is not None else None
    try:
        session.emit("scan", 0, None, f"Looking for notes in {target.name or root_key}")

        def on_directory(found: int) -> None:
            session.check_cancel()
            session.emit("scan", found, None, f"Found {found} files so far")

        unreadable: list[str] = []
        files, _truncated = list_files(
            target, patterns, on_directory=on_directory, prefix=prefix, unreadable=unreadable
        )
        if prefix in unreadable:
            # The folder itself could not be listed: nothing is known about
            # its notes, so never treat them as deleted.
            raise _unreadable_error(target)
        total = len(files)
        session.emit("scan", total, total, f"Found {total} supported files")
        if not files and not (prune and had_documents) and not adopted:
            raise _empty_corpus_error(source)

        if subfolder:
            assert root_id is not None
        else:
            root_id = index.ensure_root(root_key, stored_exclude, files_only=False)
        doc_id_for = _DocIdAssigner.for_root(index, session.manifest, root_id)
        for folder in unreadable:
            session.skip(folder or ".", SKIP_UNREADABLE)
        keep: set[str] = set()
        for position, (relative, path) in enumerate(files, start=1):
            session.check_cancel()
            document, reason = read_text_document(path, relative, root_id=root_id)
            session.emit("read", position, total, f"Reading {relative}")
            if document is None:
                session.skip(relative, reason or SKIP_UNREADABLE)
                if reason == SKIP_UNREADABLE:
                    keep.add(doc_id_for(relative))  # transient errors must not delete data
                continue
            document.doc_id = doc_id_for(relative)
            keep.add(document.doc_id)
            session.offer(document)
        session.flush()

        if prune:
            session.check_cancel()
            current = {
                doc_id: str(meta["source"] or "")
                for doc_id, meta in index.document_manifest(root_id).items()
            }
            stale = sorted(
                doc_id
                for doc_id, source_path in current.items()
                if doc_id not in keep
                and (not subfolder or in_scope(source_path))
                # Notes in folders that could not be listed are unknown, not gone.
                and not (unreadable and _under_any(source_path, unreadable))
            )
            session.emit("prune", 0, len(stale), f"Removing {len(stale)} missing documents")
            if stale:
                _carry_over_user_rows(index, root_id, stale, keep, set(session.manifest))
            session.removed = index.remove_documents(stale) if stale else 0
            session.emit(
                "prune", len(stale), len(stale), f"Removed {session.removed} missing documents"
            )

        if (
            session.considered == 0
            and session.removed == 0
            and not (prune and had_documents)
            and not adopted
        ):
            if existing is None:
                index.remove_root(root_id)
                root_id = None
            raise _empty_corpus_error(source, session)

        index.mark_root_indexed(root_id)
        duplicates = index.duplicate_groups(root_id=root_id)
        index.finish_run(run_id, "succeeded", session.counts())
    except BaseException as exc:
        _run_bookkeeping(index, run_id, session, exc)
        raise
    session.emit("done", session.considered, session.considered, _summary(session))
    return session.result(root=root_key, root_id=root_id, run_id=run_id, duplicates=duplicates)


def _carry_over_user_rows(
    index: Index, root_id: int, stale: list[str], keep: set[str], before: set[str]
) -> None:
    """Move bookmarks/activity of renamed notes to their new doc_id.

    A stale document whose content hash equals a document added in this run
    was renamed or moved, so what the user attached to it follows it.
    """
    hashes = index.fingerprints(root_id)
    by_hash: dict[str, str] = {}
    for doc_id in sorted(keep - before):
        digest = hashes.get(doc_id)
        if digest:
            by_hash.setdefault(digest, doc_id)
    pairs = [
        (doc_id, by_hash[hashes[doc_id]])
        for doc_id in stale
        if hashes.get(doc_id) and hashes[doc_id] in by_hash
    ]
    if pairs:
        index.move_user_rows(pairs)


def _refresh_file_root(
    index: Index,
    root: dict,
    *,
    prune: bool,
    incremental: bool,
    progress: ProgressCallback | None,
    cancel: CancelCallback | None,
) -> dict:
    """Update a root registered by indexing single files: only those files."""
    root_key = root["path"]
    root_id = int(root["root_id"])
    base = Path(root_key)
    stored = index.document_manifest(root_id)
    session = _Session(index, incremental=incremental, progress=progress, cancel=cancel)
    run_id = index.start_run(root_key)
    try:
        sources = sorted(
            (str(meta["source"]), doc_id) for doc_id, meta in stored.items() if meta["source"]
        )
        total = len(sources)
        session.emit("scan", total, total, f"Checking {total} indexed files")
        stale: list[str] = []
        for position, (relative, doc_id) in enumerate(sources, start=1):
            session.check_cancel()
            path = base.joinpath(*PurePosixPath(relative).parts)
            session.emit("read", position, total, f"Reading {relative}")
            if not path.is_file():
                stale.append(doc_id)
                continue
            document, reason = read_text_document(path, relative, root_id=root_id)
            if document is None:
                session.skip(relative, reason or SKIP_UNREADABLE)
                if reason != SKIP_UNREADABLE:
                    stale.append(doc_id)
                continue
            document.doc_id = doc_id
            session.offer(document)
        session.flush()
        if prune and stale:
            session.emit("prune", 0, len(stale), f"Removing {len(stale)} missing documents")
            session.removed = index.remove_documents(stale)
            session.emit(
                "prune", len(stale), len(stale), f"Removed {session.removed} missing documents"
            )
        index.mark_root_indexed(root_id)
        duplicates = index.duplicate_groups(root_id=root_id)
        index.finish_run(run_id, "succeeded", session.counts())
    except BaseException as exc:
        _run_bookkeeping(index, run_id, session, exc)
        raise
    session.emit("done", session.considered, session.considered, _summary(session))
    return session.result(root=root_key, root_id=root_id, run_id=run_id, duplicates=duplicates)


def _ingest_single_file(
    index: Index,
    source: Path,
    *,
    incremental: bool,
    progress: ProgressCallback | None,
    cancel: CancelCallback | None,
) -> dict:
    resolved = source.resolve()
    cover = _covering_root(index, resolved)
    if cover is not None and cover[1]:
        # The file is inside a registered folder: update its row in place.
        covering, relative = cover
        root_key = covering["path"]
    else:
        covering, relative = None, resolved.name
        root_key = str(resolved.parent)
    session = _Session(index, incremental=incremental, progress=progress, cancel=cancel)
    run_id = index.start_run(str(resolved))
    root_id: int | None = None
    try:
        session.check_cancel()
        document, reason = read_text_document(resolved, relative)
        session.emit("read", 1, 1, f"Reading {relative}")
        if document is None:
            session.skip(relative, reason or SKIP_UNREADABLE)
            raise _empty_corpus_error(source, session)
        if covering is not None:
            root_id = int(covering["root_id"])
        else:
            root_id = index.ensure_root(root_key, files_only=True)
        document.root_id = root_id
        document.doc_id = _DocIdAssigner.for_root(index, session.manifest, root_id)(relative)
        session.offer(document)
        session.flush()
        index.mark_root_indexed(root_id)
        duplicates = [group for group in index.duplicate_groups() if document.doc_id in group]
        index.finish_run(run_id, "succeeded", session.counts())
    except BaseException as exc:
        _run_bookkeeping(index, run_id, session, exc)
        raise
    session.emit("done", 1, 1, _summary(session))
    return session.result(root=root_key, root_id=root_id, run_id=run_id, duplicates=duplicates)


def _ingest_jsonl(
    index: Index,
    source: Path,
    *,
    incremental: bool,
    progress: ProgressCallback | None,
    cancel: CancelCallback | None,
) -> dict:
    resolved = source.resolve()
    session = _Session(index, incremental=incremental, progress=progress, cancel=cancel)
    run_id = index.start_run(str(resolved))
    try:
        documents = list(_load_jsonl(source))
        if not documents:
            raise _empty_corpus_error(source)
        total = len(documents)
        for position, document in enumerate(documents, start=1):
            session.check_cancel()
            session.emit("read", position, total, f"Reading {document.doc_id}")
            session.offer(document, compare_fields=True)
        session.flush()
        ids = {document.doc_id for document in documents}
        duplicates = [
            group
            for group in index.duplicate_groups()
            if any(doc_id in ids for doc_id in group)
        ]
        index.finish_run(run_id, "succeeded", session.counts())
    except BaseException as exc:
        _run_bookkeeping(index, run_id, session, exc)
        raise
    session.emit("done", session.considered, session.considered, _summary(session))
    return session.result(root=str(resolved), root_id=None, run_id=run_id, duplicates=duplicates)


def _summary(session: _Session) -> str:
    parts = [
        f"{session.added} added",
        f"{session.updated} updated",
        f"{session.unchanged} unchanged",
    ]
    if session.removed:
        parts.append(f"{session.removed} removed")
    if session.skipped:
        parts.append(f"{len(session.skipped)} skipped")
    return "Indexing finished: " + ", ".join(parts)


def ingest_path(
    index: Index,
    corpus_path: str | Path,
    *,
    exclude: Iterable[str] | str | None = None,
    prune: bool = False,
    incremental: bool = True,
    progress: ProgressCallback | None = None,
    cancel: CancelCallback | None = None,
    refresh_files: bool = False,
) -> dict:
    """Index a folder, a supported text file, or a JSONL dataset.

    Repeated document IDs are upserted. ``exclude`` globs add to
    ``DEFAULT_EXCLUDES`` (``None`` reuses the patterns stored for the folder);
    ``incremental`` skips files whose content hash is unchanged; ``prune``
    removes documents of this folder whose file is gone, excluded, or no
    longer indexable. ``progress(event)`` receives ``{"phase", "done",
    "total", "message"}`` events and ``cancel()`` returning True raises
    :class:`OperationCancelled` between files and batches (finished batches
    stay committed).

    A folder or file inside an already registered folder is indexed as part
    of that folder (same doc_ids, prune limited to the sub-folder); a folder
    that contains registered folders absorbs them. ``refresh_files=True`` on
    a folder that was registered by indexing single files re-reads only those
    files instead of the whole folder.
    """
    source = Path(corpus_path)
    suffix = source.suffix.lower()
    if source.is_dir():
        return _ingest_folder(
            index,
            source,
            exclude=exclude,
            prune=prune,
            incremental=incremental,
            progress=progress,
            cancel=cancel,
            refresh_files=refresh_files,
        )
    if source.is_file() and suffix in SUPPORTED_SUFFIXES:
        return _ingest_single_file(
            index, source, incremental=incremental, progress=progress, cancel=cancel
        )
    if source.is_file() and suffix == ".jsonl":
        return _ingest_jsonl(
            index, source, incremental=incremental, progress=progress, cancel=cancel
        )
    raise CorpusNotFoundError(
        f"corpus path not found or unsupported: {source} "
        f"(folders, .txt/.md files - also .markdown/.rst/.org/.text - "
        f"and .jsonl files are supported)"
    )


def _would_index(path: Path, relative: str) -> bool:
    """True when ingest would turn this file into a document (not skip it)."""
    document, _reason = read_text_document(path, relative)
    return document is not None


def _compare_file(path: Path, stored: dict, result: dict) -> None:
    """Count one stored file as unchanged or changed (mtime+size, then hash)."""
    try:
        stat = path.stat()
    except OSError:
        return
    same_stat = (
        stored["mtime"] is not None
        and abs(float(stored["mtime"]) - stat.st_mtime) < 1e-6
        and stored["size_bytes"] == stat.st_size
    )
    if same_stat:
        result["unchanged"] += 1
        return
    if not 0 < stat.st_size <= MAX_FILE_BYTES:
        result["changed"] += 1
        return
    try:
        digest = _hash_file(path)
    except OSError:
        return
    if digest == stored["content_hash"]:
        result["unchanged"] += 1
    else:
        result["changed"] += 1


def scan_changes(
    index: Index,
    root: str | Path,
    exclude: Iterable[str] | str | None = None,
    limit: int = 20000,
) -> dict:
    """Compare a registered folder with the index without writing anything.

    Uses mtime+size first and hashes a file only when they differ. ``deleted``
    is only computed when the scan was not truncated by ``limit``. A new file
    only counts when ingest would actually index it (empty, front-matter-only
    and binary files are skipped by ingest, so they never make a folder
    stale). Folders that could not be listed are reported in ``unreadable``
    (only present when there are any) and their notes are not counted as deleted. A folder registered by
    indexing single files only checks those files.
    """
    if int(limit) < 1:
        raise UsageError("scan limit must be at least 1")
    candidate = Path(root)
    try:
        resolved = candidate.resolve()
    except OSError:
        resolved = candidate
    key = str(resolved)
    registered = index.get_root(key)
    prefix = ""
    if registered is None:
        cover = _covering_root(index, resolved)
        if cover is not None:
            registered, prefix = cover
    stored_rows = index.document_manifest(registered["root_id"]) if registered else {}
    # Key by the relative source path: a doc_id may be root-qualified.
    manifest = {
        str(meta["source"] or doc_id): meta for doc_id, meta in stored_rows.items()
    }
    if prefix:
        manifest = {
            relative: meta
            for relative, meta in manifest.items()
            if relative.startswith(prefix + "/")
        }
    result = {
        "root": key,
        "exists": resolved.is_dir(),
        "new": 0,
        "changed": 0,
        "deleted": 0,
        "unchanged": 0,
        "truncated": False,
    }
    if not result["exists"]:
        result["deleted"] = len(manifest)
        return result

    if registered is not None and not prefix and registered.get("files_only"):
        base = Path(registered["path"])
        for relative, stored in manifest.items():
            path = base.joinpath(*PurePosixPath(relative).parts)
            if not path.is_file():
                result["deleted"] += 1
                continue
            _compare_file(path, stored, result)
        return result

    if exclude is None:
        user_patterns = list(registered["exclude"]) if registered else []
    else:
        user_patterns = normalize_excludes(exclude)
    unreadable: list[str] = []
    files, truncated = list_files(
        resolved,
        [*DEFAULT_EXCLUDES, *user_patterns],
        limit=int(limit),
        prefix=prefix,
        unreadable=unreadable,
    )
    result["truncated"] = truncated
    if unreadable:
        result["unreadable"] = sorted(set(unreadable))
    if prefix in unreadable:
        return result  # nothing is known about this folder's notes
    seen: set[str] = set()
    for relative, path in files:
        stored = manifest.get(relative)
        if stored is None:
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if 0 < size <= MAX_FILE_BYTES and _would_index(path, relative):
                result["new"] += 1
            continue
        seen.add(relative)
        _compare_file(path, stored, result)
    if not truncated:
        result["deleted"] = sum(
            1
            for relative in set(manifest) - seen
            if not (unreadable and _under_any(relative, unreadable))
        )
    return result
