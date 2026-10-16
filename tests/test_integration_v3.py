"""Cross-module integration checks for v1.2 (multi-root collections, backfill progress)."""

from __future__ import annotations

import hashlib
import os
import time

import numpy as np
import pytest

from onefind.ingest import ingest_path, qualified_doc_id, scan_changes
from onefind.jobs import EmbedderCache, JobManager
from onefind.search import search
from onefind.store import BATCH_SIZE, Document, Index

WAIT = 30.0


class FakeEmbedder:
    revision = "test-revision-1"
    dimension = 3

    def __init__(self, name: str = "fake-mini") -> None:
        self.name = name

    def encode_documents(self, texts):
        return np.stack([self._vector(text) for text in texts])

    def encode_query(self, text):
        return self._vector(text)

    @staticmethod
    def _vector(text):
        digest = hashlib.sha256(text.encode("utf-8")).digest()[:3]
        vector = np.frombuffer(digest, dtype=np.uint8).astype(np.float32) + 1.0
        return vector / np.linalg.norm(vector)


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def two_roots(tmp_path):
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    write(alpha / "readme.md", "# Alpha readme\nalpha project overview")
    write(alpha / "notes" / "plan.md", "# Alpha plan\nalpha planning steps")
    write(beta / "readme.md", "# Beta readme\nbeta project overview")
    write(beta / "notes" / "plan.md", "# Beta plan\nbeta planning steps")
    write(beta / "only-beta.md", "# Only beta\nunique beta content")
    return alpha, beta


def titles(index):
    return {doc_id: index.get_document(doc_id)["title"] for doc_id in index.fingerprints()}


# -- multi-root doc_ids --------------------------------------------------------------


def test_same_relative_path_in_two_roots_keeps_both_documents(tmp_path, two_roots):
    alpha, beta = two_roots
    with Index.open(tmp_path / "multi.db") as index:
        first = ingest_path(index, alpha, prune=True)
        second = ingest_path(index, beta, prune=True)
        assert (first["added"], second["added"], second["updated"]) == (2, 3, 0)
        beta_id = second["root_id"]
        assert titles(index) == {
            "readme.md": "Alpha readme",
            "notes/plan.md": "Alpha plan",
            qualified_doc_id(beta_id, "readme.md"): "Beta readme",
            qualified_doc_id(beta_id, "notes/plan.md"): "Beta plan",
            "only-beta.md": "Only beta",
        }
        # The relative source and folder stay plain for display and file opening.
        qualified = index.get_document(qualified_doc_id(beta_id, "notes/plan.md"))
        assert (qualified["source"], qualified["folder"], qualified["root_id"]) == (
            "notes/plan.md",
            "notes",
            beta_id,
        )


def test_reindexing_either_root_is_stable(tmp_path, two_roots):
    alpha, beta = two_roots
    with Index.open(tmp_path / "multi.db") as index:
        ingest_path(index, alpha, prune=True)
        ingest_path(index, beta, prune=True)
        for root in (alpha, beta, alpha, beta):
            result = ingest_path(index, root, prune=True)
            assert (result["added"], result["updated"], result["removed"]) == (0, 0, 0)
        assert index.count("documents") == 5


def test_prune_only_touches_its_own_root(tmp_path, two_roots):
    alpha, beta = two_roots
    with Index.open(tmp_path / "multi.db") as index:
        ingest_path(index, alpha, prune=True)
        beta_id = ingest_path(index, beta, prune=True)["root_id"]
        (beta / "readme.md").unlink()
        result = ingest_path(index, beta, prune=True)
        assert result["removed"] == 1
        remaining = set(index.fingerprints())
        assert "readme.md" in remaining
        assert qualified_doc_id(beta_id, "readme.md") not in remaining
        assert index.get_document("readme.md")["title"] == "Alpha readme"


def test_updates_go_to_the_right_root(tmp_path, two_roots):
    alpha, beta = two_roots
    with Index.open(tmp_path / "multi.db") as index:
        ingest_path(index, alpha)
        beta_id = ingest_path(index, beta)["root_id"]
        write(beta / "readme.md", "# Beta readme v2\nrewritten beta overview")
        result = ingest_path(index, beta)
        assert (result["added"], result["updated"]) == (0, 1)
        assert index.get_document(qualified_doc_id(beta_id, "readme.md"))["title"] == (
            "Beta readme v2"
        )
        assert index.get_document("readme.md")["title"] == "Alpha readme"


def test_scan_changes_understands_qualified_ids(tmp_path, two_roots):
    alpha, beta = two_roots
    with Index.open(tmp_path / "multi.db") as index:
        ingest_path(index, alpha)
        ingest_path(index, beta)
        clean = scan_changes(index, beta)
        assert (clean["new"], clean["changed"], clean["deleted"], clean["unchanged"]) == (
            0,
            0,
            0,
            3,
        )
        write(beta / "readme.md", "# Beta readme\nchanged text")
        (beta / "notes" / "plan.md").unlink()
        dirty = scan_changes(index, beta)
        assert (dirty["changed"], dirty["deleted"], dirty["unchanged"]) == (1, 1, 1)


def test_search_finds_both_roots_same_name(tmp_path, two_roots):
    alpha, beta = two_roots
    with Index.open(tmp_path / "multi.db") as index:
        ingest_path(index, alpha)
        beta_id = ingest_path(index, beta)["root_id"]
        hits = search(index, "overview", mode="lexical")
        assert {hit.doc_id for hit in hits} == {
            "readme.md",
            qualified_doc_id(beta_id, "readme.md"),
        }


def test_single_file_from_second_root_gets_qualified_id(tmp_path, two_roots):
    alpha, beta = two_roots
    with Index.open(tmp_path / "multi.db") as index:
        ingest_path(index, alpha)
        result = ingest_path(index, beta / "readme.md")
        beta_id = result["root_id"]
        assert result["added"] == 1
        assert index.get_document("readme.md")["title"] == "Alpha readme"
        assert index.get_document(qualified_doc_id(beta_id, "readme.md"))["title"] == (
            "Beta readme"
        )
        # Indexing the whole folder later reuses the id the file already has.
        again = ingest_path(index, beta)
        assert again["root_id"] == beta_id
        assert (again["added"], again["unchanged"]) == (2, 1)


def test_rootless_legacy_document_is_taken_over_in_place(tmp_path):
    folder = write(tmp_path / "notes" / "readme.md", "# Fresh\nfresh text").parent
    with Index.open(tmp_path / "legacy.db") as index:
        index.add_documents([Document(doc_id="readme.md", body="old text", title="Old")])
        result = ingest_path(index, folder)
        assert (result["added"], result["updated"]) == (0, 1)
        assert set(index.fingerprints()) == {"readme.md"}
        assert index.get_document("readme.md")["root_id"] == result["root_id"]


def test_unregistered_owner_is_taken_over(tmp_path, two_roots):
    alpha, beta = two_roots
    with Index.open(tmp_path / "multi.db") as index:
        alpha_id = ingest_path(index, alpha)["root_id"]
        assert index.remove_root(alpha_id)
        result = ingest_path(index, beta)
        assert (result["added"], result["updated"]) == (1, 2)
        assert index.get_document("readme.md")["title"] == "Beta readme"


def test_unreadable_file_in_second_root_is_not_pruned(tmp_path, two_roots, monkeypatch):
    import onefind.ingest as ingest_module

    alpha, beta = two_roots
    with Index.open(tmp_path / "multi.db") as index:
        ingest_path(index, alpha)
        beta_id = ingest_path(index, beta)["root_id"]
        real = ingest_module.read_text_document

        def flaky(path, relative, **kwargs):
            if relative == "readme.md":
                return None, ingest_module.SKIP_UNREADABLE
            return real(path, relative, **kwargs)

        monkeypatch.setattr(ingest_module, "read_text_document", flaky)
        result = ingest_path(index, beta, prune=True)
        assert result["removed"] == 0
        assert qualified_doc_id(beta_id, "readme.md") in index.fingerprints()


def test_qualified_doc_id_format():
    assert qualified_doc_id(7, "a/b.md") == "~7/a/b.md"


# -- vector backfill progress -------------------------------------------------------


def _lexical_index(path, count):
    index = Index.open(path)
    index.add_documents(
        [Document(doc_id=f"d{n:04d}", body=f"body number {n}", title=f"T{n}") for n in range(count)]
    )
    return index


def test_attach_embedder_reports_backfill_progress(tmp_path):
    pytest.importorskip("sqlite_vec")
    count = BATCH_SIZE + 5
    with _lexical_index(tmp_path / "bf.db", count) as index:
        if not index.vec_ready:
            pytest.skip("sqlite-vec cannot load here")
        events: list[dict] = []
        index.attach_embedder(FakeEmbedder(), progress=events.append)
        assert events == [
            {"phase": "embed", "done": BATCH_SIZE, "total": count},
            {"phase": "embed", "done": count, "total": count},
        ]
        assert index.health()["missing_vectors"] == 0
        # Nothing left to backfill: no events on a second attach.
        events.clear()
        index.attach_embedder(FakeEmbedder(), progress=events.append)
        assert events == []


def test_attach_embedder_without_progress_still_backfills(tmp_path):
    pytest.importorskip("sqlite_vec")
    with _lexical_index(tmp_path / "bf.db", 3) as index:
        if not index.vec_ready:
            pytest.skip("sqlite-vec cannot load here")
        index.attach_embedder(FakeEmbedder())
        assert index.health()["vectors"] == 3


def test_job_reports_backfill_of_existing_notes(tmp_path):
    pytest.importorskip("sqlite_vec")
    folder = tmp_path / "notes"
    for n in range(4):
        write(folder / f"n{n}.md", f"# Note {n}\ncontent {n}")
    db = tmp_path / "job.db"
    with Index.open(db) as index:
        if not index.vec_ready:
            pytest.skip("sqlite-vec cannot load here")
        ingest_path(index, folder)  # lexical only
    manager = JobManager(EmbedderCache(factory=FakeEmbedder))
    try:
        job = manager.start_index(db, folder, embed=True)
        job = manager.wait(job["id"], timeout=WAIT)
        assert job["status"] == "succeeded", job["error"]
        assert job["counts"]["embedded"] == 4
        assert job["result"]["unchanged"] == 4
        assert job["health"]["missing_vectors"] == 0
        assert {step["name"]: step["status"] for step in job["steps"]}["vectors"] == "done"
    finally:
        manager.shutdown()


def test_job_on_multi_root_collection_keeps_both(tmp_path, two_roots):
    alpha, beta = two_roots
    db = tmp_path / "jobs-multi.db"
    manager = JobManager(EmbedderCache(factory=FakeEmbedder))
    try:
        for root in (alpha, beta, alpha):
            job = manager.wait(manager.start_index(db, root, embed=False)["id"], timeout=WAIT)
            assert job["status"] == "succeeded", job["error"]
        assert job["result"]["removed"] == 0
    finally:
        manager.shutdown()
    with Index.open(db) as index:
        assert index.count("documents") == 5


def test_mtime_only_touch_counts_unchanged_in_second_root(tmp_path, two_roots):
    alpha, beta = two_roots
    with Index.open(tmp_path / "multi.db") as index:
        ingest_path(index, alpha)
        ingest_path(index, beta)
        target = beta / "readme.md"
        later = time.time() + 5
        os.utime(target, (later, later))
        result = ingest_path(index, beta)
        assert (result["added"], result["updated"], result["unchanged"]) == (0, 0, 3)
        assert scan_changes(index, beta)["unchanged"] == 3
