"""Corpus loading for folders, text files, and local JSONL datasets."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from .errors import CorpusNotFoundError, DataError
from .store import Document, Index

SUPPORTED_SUFFIXES = {".txt", ".md"}
SUPPORTED_FILES = SUPPORTED_SUFFIXES | {".jsonl"}


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


def _relative_doc_id(relative_path: Path) -> str:
    """Use the canonical relative filename so every source path is unique."""
    return relative_path.as_posix()


def iter_files(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
            yield path


def _load_text_file(path: Path, root: Path) -> Document:
    raw = path.read_text(encoding="utf-8", errors="replace")
    relative = path.relative_to(root)
    title, body = _split_title(raw, path.stem)
    return Document(
        doc_id=_relative_doc_id(relative),
        body=body,
        title=title,
        source=relative.as_posix(),
    )


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
            yield Document(
                doc_id=doc_id,
                body=body,
                title=title,
                source=source,
                meta=meta,
            )


def load_documents(root: Path) -> Iterator[Document]:
    for path in iter_files(root):
        yield _load_text_file(path, root)


def ingest_path(index: Index, corpus_path: str | Path) -> dict:
    """Index a folder or supported file; repeated document IDs are upserted."""
    root = Path(corpus_path)
    if root.is_dir():
        docs = list(load_documents(root))
    elif root.is_file() and root.suffix.lower() in SUPPORTED_SUFFIXES:
        docs = [_load_text_file(root, root.parent)]
    elif root.is_file() and root.suffix.lower() == ".jsonl":
        docs = list(_load_jsonl(root))
    else:
        raise CorpusNotFoundError(
            f"corpus path not found or unsupported: {root} "
            f"(folders, .txt/.md files, and .jsonl files are supported)"
        )

    if not docs:
        raise CorpusNotFoundError(
            f"no readable corpus documents at {root} "
            f"(.txt/.md files inside folders, standalone .txt/.md files, "
            f"or .jsonl datasets are supported)"
        )
    sources_by_id: dict[str, str] = {}
    for document in docs:
        source = document.source or document.doc_id
        previous = sources_by_id.get(document.doc_id)
        if previous is not None and previous != source:
            raise DataError(
                f"duplicate document id {document.doc_id!r} from {previous!r} and {source!r}"
            )
        sources_by_id[document.doc_id] = source
    indexed = index.add_documents(docs)
    return {
        "files_indexed": indexed,
        "total_documents": index.count("documents"),
        "db": str(index.path),
    }
