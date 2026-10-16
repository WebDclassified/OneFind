"""Cross-area follow-ups wired up after the parallel review fixes."""

from __future__ import annotations

import pytest

pytest.importorskip("numpy")

from onefind.errors import DataError

from test_search_review_fixes import reader_and_writer, scan_db  # noqa: F401 - fixtures


def test_store_vector_blocks_survive_a_concurrent_commit(reader_and_writer):  # noqa: F811
    reader, writer = reader_and_writer
    blocks = []
    for matrix, row_ids in reader.iter_vector_blocks(block_size=1):
        blocks.append(row_ids)
        # Another connection commits between blocks: the scan keeps its snapshot.
        writer.record_search(f"concurrent {len(blocks)}", "lexical", None, None, 0)
    assert len(blocks) >= 2
    assert reader.conn.in_transaction is False


def test_store_vector_blocks_end_their_transaction_on_early_close(reader_and_writer):  # noqa: F811
    reader, _writer = reader_and_writer
    scan = reader.iter_vector_blocks(block_size=1)
    next(scan)
    assert reader.conn.in_transaction is True
    scan.close()
    assert reader.conn.in_transaction is False


def test_scan_change_during_search_is_a_retryable_409(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    import onefind.serve as serve_module
    from test_serve_v3 import make_client

    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("# A\n\nTomatoes need sun.\n", encoding="utf-8")
    app, client = make_client(tmp_path / "home")
    with client:
        from test_serve_v3 import index_folder

        index_folder(client, notes)

        def changed(*_args, **_kwargs):
            raise DataError("vector index changed during scanning; retry the search")

        monkeypatch.setattr(serve_module, "search_detailed", changed)
        response = client.post("/api/search", json={"query": "tomatoes", "mode": "lexical"})
        assert response.status_code == 409, response.text
        error = response.json()["error"]
        assert error["code"] == "index_changed"
        assert error["retryable"] is True

        def broken(*_args, **_kwargs):
            raise DataError("something else is wrong")

        monkeypatch.setattr(serve_module, "search_detailed", broken)
        response = client.post("/api/search", json={"query": "tomatoes", "mode": "lexical"})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "search_error"


def test_replacing_a_saved_search_refreshes_its_saved_time(tmp_path, monkeypatch):
    import onefind.store as store_module
    from onefind.store import Index

    stamps = iter(["2026-01-01T00:00:00Z", "2026-03-01T00:00:00Z"])
    with Index.open(tmp_path / "saved.db") as index:
        monkeypatch.setattr(store_module, "utcnow_iso", lambda: next(stamps))
        first = index.save_query("Week", "garden", "lexical", filters={"since": "7d"})
        second = index.save_query("week", "garden beds", "lexical", filters={"since": "7d"})
    assert second["id"] == first["id"]
    # Relative date presets are worked out from the latest save, not the first.
    assert first["created_at"] == "2026-01-01T00:00:00Z"
    assert second["created_at"] == "2026-03-01T00:00:00Z"
