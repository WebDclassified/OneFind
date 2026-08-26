"""Environment verification (engineering plan task T-00).

`collect_check()` probes the runtime in layers so failures name the exact fix:
1. bundled SQLite version + FTS5 availability (BM25 smoke query)
2. sqlite-vec loadable-extension availability (KNN smoke query)
3. optional embedding-model stack (imported lazily; heavy download only on demand)
"""

from __future__ import annotations

import sqlite3
import sys

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


def sqlite_version() -> str:
    return sqlite3.sqlite_version


def python_version() -> str:
    return sys.version.split()[0]


def fts5_available(conn: sqlite3.Connection) -> tuple[bool, str]:
    """FTS5 counts only if we can create a table AND rank with bm25()."""
    try:
        # NOTE: no temp. prefix - bm25() cannot take schema-qualified names,
        # and this :memory: connection is disposable anyway.
        conn.execute("CREATE VIRTUAL TABLE probe_fts USING fts5(body)")
        conn.execute("INSERT INTO probe_fts VALUES ('hello hybrid world')")
        conn.execute(
            "SELECT bm25(probe_fts) FROM probe_fts WHERE probe_fts MATCH 'hello'"
        ).fetchone()
        return True, "bm25 query ok"
    except Exception as exc:  # noqa: BLE001 - report any failure verbatim
        return False, str(exc)


def vec_available(conn: sqlite3.Connection) -> tuple[bool, str]:
    """sqlite-vec must load as an extension AND answer a tiny KNN query."""
    try:
        import sqlite_vec  # type: ignore[import-not-found]
    except ImportError as exc:
        return False, f"package missing - run: pip install sqlite-vec ({exc})"

    loader_missing = not hasattr(conn, "enable_load_extension")
    if loader_missing:
        return (
            False,
            "this Python build disabled extension loading "
            "(no enable_load_extension); use an official python.org interpreter",
        )
    try:
        conn.enable_load_extension(True)  # noqa: PLC2801 - required API
        sqlite_vec.load(conn)
        version = conn.execute("SELECT vec_version()").fetchone()[0]
        conn.execute("CREATE VIRTUAL TABLE temp.probe_vec USING vec0(embedding float[4])")
        conn.execute(
            "INSERT INTO temp.probe_vec(rowid, embedding) VALUES (1, '[1,0,0,0]'), (2, '[0,1,0,0]')"
        )
        hits = conn.execute(
            "SELECT rowid FROM temp.probe_vec "
            "WHERE embedding MATCH '[0,1,0,0]' AND k = 2"
        ).fetchall()
        return True, f"vec_version={version}; knn_rows={len(hits)}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    finally:
        try:
            conn.enable_load_extension(False)  # noqa: PLC2801
        except Exception:  # noqa: BLE001,S110 - best-effort cleanup
            pass


def model_status(load_model: bool, model_name: str = DEFAULT_MODEL) -> dict:
    """Report the embedding stack without touching the network unless asked."""
    info: dict = {"model": model_name}
    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        info["status"] = "not_installed"
        info["fix"] = 'pip install -e ".[model]"'
        return info
    if not load_model:
        info["status"] = "installed_not_loaded"
        info["hint"] = "re-run with --full to download/load the model once"
        return info
    try:
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer(model_name)
        info["status"] = "loaded"
        info["dimension"] = model.get_sentence_embedding_dimension()
    except Exception as exc:  # noqa: BLE001
        info["status"] = "error"
        info["error"] = str(exc)[:300]
    return info


def collect_check(load_model: bool = False) -> dict:
    """Full layered report. ok=True means the engine can run (model optional)."""
    conn = sqlite3.connect(":memory:")
    try:
        fts_ok, fts_detail = fts5_available(conn)
        vec_ok, vec_detail = vec_available(conn)
    finally:
        conn.close()

    report = {
        "python": python_version(),
        "sqlite": {"version": sqlite_version(), "fts5": {"ok": fts_ok, "detail": fts_detail}},
        "vector": {"ok": vec_ok, "detail": vec_detail},
        "model": model_status(load_model),
        "ok": fts_ok and vec_ok,
    }
    return report
