"""Corpus loading: folder of .txt/.md files -> Document stream.

doc_id = filename stem (stable across re-indexes => idempotent upserts).
Title rule: first non-empty line starting with '# ' becomes the title
and is excluded from the body; otherwise the stem is used as title.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from .errors import CorpusNotFoundError
from .store import Document, Index

SUPPORTED_SUFFIXES = {".txt", ".md"}


def _split_title(text: str, stem: str) -> tuple[str, str]:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("# "):
            return stripped[2:].strip(), "\n".join(lines[i + 1 :]).strip()
        break
    return stem, text.strip()


def iter_files(root: Path) -> Iterator[Path]:
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES:
            yield p


def load_documents(root: Path) -> Iterator[Document]:
    for path in iter_files(root):
        raw = path.read_text(encoding="utf-8", errors="replace")
        title, body = _split_title(raw, path.stem)
        yield Document(
            doc_id=path.stem,
            body=body,
            title=title,
            source=str(path.relative_to(root)),
        )


def ingest_path(index: Index, corpus_path: str | Path) -> dict:
    """Index every supported file under `corpus_path`. Idempotent."""
    root = Path(corpus_path)
    if not root.is_dir():
        raise CorpusNotFoundError(f"corpus folder not found: {root}")
    docs = list(load_documents(root))
    if not docs:
        raise CorpusNotFoundError(
            f"no readable .txt/.md files under {root} "
            f"(supported suffixes: {sorted(SUPPORTED_SUFFIXES)})"
        )
    indexed = index.add_documents(docs)
    return {
        "files_indexed": indexed,
        "total_documents": index.count("documents"),
        "db": str(index.path),
    }
