"""BEIR dataset acquisition and loading (T-07).

Two sources are supported:
1. Registry names ('scifact', 'nfcorpus') fetched from the HuggingFace Hub
   - corpus + queries arrive as parquet (BeIR/<name>), qrels as TSV
     from the sibling repo BeIR/<name>-qrels. Files cache under data/.
   - reading parquet requires the [eval] extra (pyarrow).
2. A local directory containing corpus.jsonl / queries.jsonl /
   qrels.tsv (or the parquet equivalents) - used by tests and for
   fully offline runs.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from .errors import DataError

HF_BASE = "https://huggingface.co/datasets/BeIR"
DATA_ROOT = Path("data")

_REGISTRY = {
    "scifact": {"corpus_bytes": 4_469_916, "docs": 5183},
    "nfcorpus": {},
}


def _download(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "scrydb-reproduction/0.1"})
    with urllib.request.urlopen(request, timeout=120) as response, target.open("wb") as fh:
        while chunk := response.read(1 << 16):
            fh.write(chunk)


def _find_one(folder: Path, stem: str, suffixes: tuple[str, ...]) -> Path | None:
    for suffix in suffixes:
        candidate = folder / f"{stem}{suffix}"
        if candidate.exists():
            return candidate
    return None


def ensure_dataset(name: str) -> Path:
    """Download-and-cache a registry dataset; returns its folder."""
    folder = DATA_ROOT / name
    needed = {
        "corpus": f"{HF_BASE}/{name}/resolve/main/corpus/corpus-00000-of-00001.parquet",
        "queries": f"{HF_BASE}/{name}/resolve/main/queries/queries-00000-of-00001.parquet",
        "qrels": f"{HF_BASE}/{name}-qrels/resolve/main/test.tsv",
    }
    targets = {
        "corpus": folder / "corpus.parquet",
        "queries": folder / "queries.parquet",
        "qrels": folder / "qrels.tsv",
    }
    for key, url in needed.items():
        if not targets[key].exists():
            print(f"downloading {key}: {url}")
            try:
                _download(url, targets[key])
            except Exception as exc:  # noqa: BLE001 - surface as DataError
                raise DataError(f"failed to download {key} for '{name}': {exc}") from exc
    return folder


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _read_parquet_rows(path: Path) -> list[dict]:
    import pyarrow.parquet as pq

    return pq.read_table(path).to_pylist()


def _rows(folder: Path, stem: str) -> list[dict]:
    path = _find_one(folder, stem, (".parquet", ".jsonl"))
    if path is None:
        raise DataError(f"missing {stem} file (jsonl or parquet) under {folder}")
    return _read_parquet_rows(path) if path.suffix == ".parquet" else _read_jsonl(path)


def load_corpus(folder: Path) -> dict[str, dict]:
    """doc_id -> {'title': str, 'text': str}."""
    corpus: dict[str, dict] = {}
    for row in _rows(folder, "corpus"):
        doc_id = row.get("_id") or row.get("id")
        if not doc_id:
            continue
        corpus[str(doc_id)] = {
            "title": row.get("title") or "",
            "text": row.get("text") or "",
        }
    if not corpus:
        raise DataError(f"empty corpus under {folder}")
    return corpus


def load_queries(folder: Path) -> dict[str, str]:
    """query_id -> query text."""
    queries: dict[str, str] = {}
    for row in _rows(folder, "queries"):
        qid = row.get("_id") or row.get("id")
        text = row.get("text") or ""
        if qid and text.strip():
            queries[str(qid)] = text
    if not queries:
        raise DataError(f"no usable queries under {folder}")
    return queries


def load_qrels(folder: Path) -> dict[str, dict[str, int]]:
    """qid -> {doc_id: relevance} from a TSV with header."""
    path = _find_one(folder, "qrels", (".tsv",))
    if path is None:
        raise DataError(f"missing qrels.tsv under {folder}")
    qrels: dict[str, dict[str, int]] = {}
    with path.open(encoding="utf-8") as fh:
        header = fh.readline().rstrip("\n").split("\t")
        id_cols = [i for i, col in enumerate(header) if col.lower().endswith("-id")]
        score_col = next((i for i, col in enumerate(header) if col.lower() == "score"), None)
        if len(id_cols) < 2 or score_col is None:
            raise DataError(f"unexpected qrels header in {path}: {header}")
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < len(header):
                continue
            qid, did = parts[id_cols[0]], parts[id_cols[1]]
            qrels.setdefault(qid, {})[did] = int(float(parts[score_col]))
    if not qrels:
        raise DataError(f"empty qrels under {folder}")
    return qrels


def load_local_or_registry(source: str) -> Path:
    """Registry name -> ensured folder; existing directory -> used as-is."""
    candidate = Path(source)
    if candidate.is_dir():
        return candidate
    if source in _REGISTRY:
        return ensure_dataset(source)
    raise DataError(
        f"unknown dataset '{source}' (registry: {sorted(_REGISTRY)}) "
        "or pass a local folder with corpus/queries/qrels files"
    )
