"""Collections registry: home resolution, persistence, naming, and backups."""

import datetime
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from onefind import library as library_module
from onefind.errors import DataError, UsageError
from onefind.library import (
    AlreadyRegisteredError,
    Library,
    NameTakenError,
    default_home,
    slugify,
    validate_name,
)
from onefind.store import Index


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    # Guard: nothing in these tests may fall back to the real user home.
    monkeypatch.setenv("ONEFIND_HOME", str(path))
    return path


@pytest.fixture
def lib(home):
    return Library(home)


def _registry(home: Path) -> dict:
    return json.loads((home / "collections.json").read_text(encoding="utf-8"))


def _corrupt_files(home: Path) -> list[Path]:
    return sorted(home.glob("collections.json.corrupt-*"))


def _make_index(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Index.open(path).close()
    return path


def _freeze(monkeypatch, moment: datetime.datetime) -> None:
    monkeypatch.setattr(library_module, "_now", lambda: moment)


# -- default_home --------------------------------------------------------------


def test_default_home_honors_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("ONEFIND_HOME", str(tmp_path / "custom"))
    assert default_home() == (tmp_path / "custom").absolute()


def test_default_home_blank_override_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("ONEFIND_HOME", "   ")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert default_home() == tmp_path / "xdg" / "onefind"


def test_default_home_windows_uses_appdata(tmp_path, monkeypatch):
    monkeypatch.delenv("ONEFIND_HOME", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    assert default_home() == (tmp_path / "Roaming" / "OneFind").absolute()


def test_default_home_windows_without_appdata(tmp_path, monkeypatch):
    monkeypatch.delenv("ONEFIND_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert default_home() == (tmp_path / "AppData" / "Roaming" / "OneFind").absolute()


def test_default_home_macos(tmp_path, monkeypatch):
    monkeypatch.delenv("ONEFIND_HOME", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert default_home() == tmp_path / "Library" / "Application Support" / "OneFind"


def test_default_home_linux_xdg_and_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("ONEFIND_HOME", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert default_home() == tmp_path / "xdg" / "onefind"
    monkeypatch.setenv("XDG_DATA_HOME", "relative/ignored")
    assert default_home() == tmp_path / ".local" / "share" / "onefind"
    monkeypatch.delenv("XDG_DATA_HOME")
    assert default_home() == tmp_path / ".local" / "share" / "onefind"


def test_library_without_home_uses_default_home(home):
    lib = Library()
    assert lib.home == home.absolute()
    assert lib.collections_dir == lib.home / "collections"
    assert lib.backups_dir == lib.home / "backups"


def test_constructing_library_has_no_side_effects(home):
    lib = Library(home)
    assert lib.list() == []
    assert lib.active() is None
    assert not home.exists()


# -- names and slugs -----------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [("  Notes  ", "Notes"), ("a", "a"), ("x" * 64, "x" * 64), ("Café 2", "Café 2"), ("日本", "日本")],
)
def test_validate_name_accepts(raw, expected):
    assert validate_name(raw) == expected


@pytest.mark.parametrize(
    "raw", ["", "   ", "x" * 65, "---", "!!", "a\nb", "tab\there", None, 3]
)
def test_validate_name_rejects(raw):
    with pytest.raises(UsageError):
        validate_name(raw)


@pytest.mark.parametrize(
    "name, slug",
    [
        ("My Notes", "my-notes"),
        ("  Work / 2026!! ", "work-2026"),
        ("Café Crème", "cafe-creme"),
        ("日本", "collection"),
        ("a--b__c", "a-b-c"),
        ("x" * 64, "x" * 48),
    ],
)
def test_slugify(name, slug):
    assert slugify(name) == slug


# -- create / list / get -----------------------------------------------------


def test_create_makes_empty_onefind_db_and_registry(lib, home):
    entry = lib.create("My Notes")
    db = Path(entry["db"])
    assert db == (home / "collections" / "my-notes.db").resolve()
    assert db.is_file()
    assert entry["name"] == "My Notes"
    assert entry["exists"] is True
    assert entry["active"] is True  # first collection becomes active
    assert entry["slug"] == "my-notes"
    datetime.datetime.fromisoformat(entry["created_at"])
    with Index.open(db) as index:
        assert index.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0

    data = _registry(home)
    assert data["version"] == 1
    assert data["active"] == "My Notes"
    assert data["collections"][0]["name"] == "My Notes"
    assert data["collections"][0]["db"] == str(db)
    assert set(data["collections"][0]) >= {"name", "db", "created_at"}


def test_list_shape_and_order(lib):
    lib.create("One")
    lib.create("Two")
    items = lib.list()
    assert [item["name"] for item in items] == ["One", "Two"]
    assert [item["active"] for item in items] == [True, False]
    for item in items:
        assert set(item) >= {"name", "db", "created_at", "exists", "active"}
        assert Path(item["db"]).is_absolute()


def test_create_does_not_change_existing_active(lib):
    lib.create("One")
    second = lib.create("Two")
    assert second["active"] is False
    assert lib.active()["name"] == "One"


def test_get_is_case_insensitive_and_trimmed(lib):
    lib.create("Research")
    assert lib.get("  rEsEaRcH ")["name"] == "Research"


def test_get_unknown_raises_data_error(lib):
    with pytest.raises(DataError):
        lib.get("missing")
    with pytest.raises(DataError):
        lib.get(None)


def test_create_duplicate_name_case_insensitive(lib):
    lib.create("Notes")
    with pytest.raises(NameTakenError):
        lib.create(" NOTES ")
    assert isinstance(NameTakenError("x"), UsageError)
    assert len(lib.list()) == 1


def test_create_invalid_name_creates_nothing(lib, home):
    with pytest.raises(UsageError):
        lib.create("  ")
    assert not home.exists()


def test_create_slug_collision_suffixes(lib, home):
    first = lib.create("Notes!")
    second = lib.create("Notes?")
    third = lib.create("notes.")
    assert Path(first["db"]).name == "notes.db"
    assert Path(second["db"]).name == "notes-2.db"
    assert Path(third["db"]).name == "notes-3.db"
    assert [first["slug"], second["slug"], third["slug"]] == ["notes", "notes-2", "notes-3"]


def test_create_avoids_leftover_unregistered_files(lib, home):
    collections = home / "collections"
    collections.mkdir(parents=True)
    (collections / "notes.db").write_bytes(b"user data")
    (collections / "notes-2.db-wal").write_bytes(b"stale wal")
    entry = lib.create("Notes")
    assert Path(entry["db"]).name == "notes-3.db"
    assert (collections / "notes.db").read_bytes() == b"user data"


def test_create_non_ascii_name_uses_fallback_slug(lib):
    entry = lib.create("日本")
    assert Path(entry["db"]).name == "collection.db"


def test_create_with_explicit_path_makes_parent_dirs(lib, tmp_path):
    target = tmp_path / "elsewhere" / "deep" / "work.db"
    entry = lib.create("Work", target)
    assert Path(entry["db"]) == target.resolve()
    assert target.is_file()
    Index.open(target).close()


def test_create_with_existing_path_refuses(lib, tmp_path):
    target = tmp_path / "exists.db"
    target.write_bytes(b"precious")
    with pytest.raises(DataError):
        lib.create("Work", target)
    assert target.read_bytes() == b"precious"
    assert lib.list() == []


def test_create_with_path_of_registered_db_refuses(lib, tmp_path):
    target = tmp_path / "x.db"
    lib.create("A", target)
    target.unlink()  # registered but missing on disk
    with pytest.raises(AlreadyRegisteredError) as info:
        lib.create("B", target)
    assert info.value.existing == "A"


def test_create_rolls_back_db_when_registry_write_fails(lib, home, monkeypatch):
    def boom(self, state):
        raise DataError("disk full")

    monkeypatch.setattr(Library, "_save", boom)
    with pytest.raises(DataError):
        lib.create("Notes")
    assert not (home / "collections" / "notes.db").exists()


def test_create_index_failure_is_data_error(lib, tmp_path, monkeypatch):
    def fail_open(path):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(Index, "open", staticmethod(fail_open))
    with pytest.raises(DataError):
        lib.create("Notes", tmp_path / "n.db")
    assert lib.list() == []


def test_list_reports_missing_files(lib):
    entry = lib.create("Notes")
    Path(entry["db"]).unlink()
    (item,) = lib.list()
    assert item["exists"] is False


# -- register ----------------------------------------------------------------


def test_register_existing_index(lib, tmp_path):
    db = _make_index(tmp_path / "moved" / "archive.db")
    entry = lib.register("Archive", db)
    assert entry["db"] == str(db.resolve())
    assert entry["exists"] is True
    assert entry["active"] is True
    assert lib.find_by_db(db)["name"] == "Archive"


def test_register_relative_path_is_stored_absolute(lib, tmp_path, monkeypatch):
    _make_index(tmp_path / "rel.db")
    monkeypatch.chdir(tmp_path)
    entry = lib.register("Rel", "rel.db")
    assert Path(entry["db"]).is_absolute()
    assert Path(entry["db"]) == (tmp_path / "rel.db").resolve()


def test_register_default_name_is_file_stem(lib, tmp_path):
    db = _make_index(tmp_path / "field-notes.db")
    assert lib.register(None, db)["name"] == "field-notes"


def test_register_missing_file(lib, tmp_path):
    with pytest.raises(DataError):
        lib.register("Ghost", tmp_path / "ghost.db")
    assert not (tmp_path / "ghost.db").exists()


def test_register_directory(lib, tmp_path):
    with pytest.raises(DataError):
        lib.register("Dir", tmp_path)


def test_register_empty_file_is_untouched(lib, tmp_path):
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    with pytest.raises(DataError):
        lib.register("Empty", empty)
    assert empty.exists() and empty.stat().st_size == 0


def test_register_non_sqlite_file(lib, tmp_path):
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"this is not sqlite at all, just some text" * 50)
    with pytest.raises(DataError):
        lib.register("Junk", junk)
    assert junk.exists()
    assert lib.list() == []


def test_register_foreign_sqlite_database(lib, tmp_path):
    other = tmp_path / "other.db"
    conn = sqlite3.connect(other)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()
    with pytest.raises(DataError):
        lib.register("Other", other)
    assert lib.list() == []


def test_register_duplicate_db_and_name(lib, tmp_path):
    db = _make_index(tmp_path / "a.db")
    lib.register("A", db)
    with pytest.raises(AlreadyRegisteredError):
        lib.register("B", db)
    with pytest.raises(NameTakenError):
        lib.register("a", _make_index(tmp_path / "b.db"))


def test_find_by_db_unknown(lib, tmp_path):
    assert lib.find_by_db(tmp_path / "nope.db") is None


# -- remove ------------------------------------------------------------------


def test_remove_keeps_file_by_default(lib):
    entry = lib.create("Notes")
    lib.remove("notes")
    assert lib.list() == []
    assert Path(entry["db"]).exists()


def test_remove_delete_file_removes_db_wal_shm(lib):
    entry = lib.create("Notes")
    db = Path(entry["db"])
    Path(f"{db}-wal").write_bytes(b"wal")
    Path(f"{db}-shm").write_bytes(b"shm")
    lib.remove("Notes", delete_file=True)
    assert not db.exists()
    assert not Path(f"{db}-wal").exists()
    assert not Path(f"{db}-shm").exists()


def test_remove_delete_file_when_already_missing(lib):
    entry = lib.create("Notes")
    Path(entry["db"]).unlink()
    lib.remove("Notes", delete_file=True)
    assert lib.list() == []


def test_remove_keeps_entry_when_delete_fails(lib, monkeypatch):
    entry = lib.create("Notes")
    real_unlink = Path.unlink

    def locked(self, missing_ok=False):
        if str(self) == entry["db"]:
            raise PermissionError("file in use")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", locked)
    with pytest.raises(DataError):
        lib.remove("Notes", delete_file=True)
    assert [item["name"] for item in lib.list()] == ["Notes"]


def test_remove_unknown(lib):
    with pytest.raises(DataError):
        lib.remove("nothing")


def test_remove_active_promotes_first_remaining(lib):
    lib.create("A")
    lib.create("B")
    lib.create("C")
    lib.set_active("B")
    lib.remove("B")
    assert lib.active()["name"] == "A"
    lib.remove("A")
    assert lib.active()["name"] == "C"
    lib.remove("C")
    assert lib.active() is None
    assert _registry(lib.home)["active"] is None


def test_remove_inactive_keeps_active(lib):
    lib.create("A")
    lib.create("B")
    lib.set_active("B")
    lib.remove("A")
    assert lib.active()["name"] == "B"


# -- rename / active -----------------------------------------------------------


def test_rename_keeps_file_and_slug_and_active(lib, home):
    entry = lib.create("Notes")
    renamed = lib.rename("notes", "  Journal ")
    assert renamed["name"] == "Journal"
    assert renamed["db"] == entry["db"]
    assert renamed["slug"] == "notes"
    assert renamed["active"] is True
    assert _registry(home)["active"] == "Journal"
    with pytest.raises(DataError):
        lib.get("Notes")


def test_rename_case_only_change_allowed(lib):
    lib.create("notes")
    assert lib.rename("notes", "NOTES")["name"] == "NOTES"


def test_rename_conflicts_and_errors(lib):
    lib.create("A")
    lib.create("B")
    with pytest.raises(NameTakenError):
        lib.rename("A", "b")
    with pytest.raises(UsageError):
        lib.rename("A", "")
    with pytest.raises(DataError):
        lib.rename("Z", "Y")
    assert [item["name"] for item in lib.list()] == ["A", "B"]


def test_rename_inactive_keeps_active_pointer(lib):
    lib.create("A")
    lib.create("B")
    lib.rename("B", "Bee")
    assert lib.active()["name"] == "A"


def test_set_active_and_active(lib, home):
    lib.create("A")
    lib.create("B")
    result = lib.set_active("b")
    assert result["name"] == "B" and result["active"] is True
    assert lib.active()["name"] == "B"
    assert _registry(home)["active"] == "B"
    assert [item["active"] for item in lib.list()] == [False, True]
    with pytest.raises(DataError):
        lib.set_active("nope")
    assert lib.active()["name"] == "B"


def test_dangling_active_pointer_falls_back_to_first(lib, home):
    lib.create("A")
    lib.create("B")
    data = _registry(home)
    data["active"] = "Removed By Hand"
    (home / "collections.json").write_text(json.dumps(data), encoding="utf-8")
    assert lib.active()["name"] == "A"


def test_two_instances_share_state(home):
    first = Library(home)
    second = Library(home)
    first.create("A")
    assert [item["name"] for item in second.list()] == ["A"]
    second.rename("A", "B")
    assert first.get("b")["name"] == "B"


# -- persistence & tolerant loading ----------------------------------------


def test_atomic_write_leaves_no_temp_files(lib, home):
    lib.create("A")
    lib.create("B")
    lib.rename("A", "C")
    leftovers = [p.name for p in home.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_save_uses_os_replace(lib, home, monkeypatch):
    calls = []
    real_replace = os.replace

    def spy(src, dst):
        calls.append((Path(src), Path(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(library_module.os, "replace", spy)
    lib.create("A")
    assert calls and calls[-1][1] == home / "collections.json"
    assert calls[-1][0].parent == home


def test_save_failure_preserves_previous_registry(lib, home, monkeypatch):
    lib.create("A")
    before = (home / "collections.json").read_bytes()

    def fail(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(library_module.os, "replace", fail)
    with pytest.raises(DataError):
        lib.rename("A", "B")
    assert (home / "collections.json").read_bytes() == before
    assert [p for p in home.iterdir() if p.name.endswith(".tmp")] == []


def test_replace_retries_transient_permission_error(lib, home, monkeypatch):
    real_replace = os.replace
    attempts = {"n": 0}

    def flaky(src, dst):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise PermissionError("sharing violation")
        return real_replace(src, dst)

    monkeypatch.setattr(library_module.os, "replace", flaky)
    monkeypatch.setattr(library_module.time, "sleep", lambda _s: None)
    lib.create("A")
    assert attempts["n"] == 2
    assert _registry(home)["collections"][0]["name"] == "A"


@pytest.mark.parametrize(
    "content",
    [
        b"{not json",
        b"\xff\xfe\x00garbage",
        b"[1, 2, 3]",
        b'{"version": 1, "collections": "nope"}',
        b'{"version": 0, "collections": []}',
        b'{"version": "1", "collections": []}',
        b"",
    ],
)
def test_corrupt_registry_is_set_aside_not_deleted(lib, home, content, monkeypatch):
    home.mkdir(parents=True)
    (home / "collections.json").write_bytes(content)
    _freeze(monkeypatch, datetime.datetime(2026, 10, 13, 9, 30, 15))
    assert lib.list() == []
    (corrupt,) = _corrupt_files(home)
    assert corrupt.name == "collections.json.corrupt-20261013-093015"
    assert corrupt.read_bytes() == content
    assert not (home / "collections.json").exists()
    # The library keeps working afterwards.
    lib.create("Fresh")
    assert [item["name"] for item in lib.list()] == ["Fresh"]
    assert len(_corrupt_files(home)) == 1


def test_corrupt_quarantine_names_do_not_collide(lib, home, monkeypatch):
    home.mkdir(parents=True)
    _freeze(monkeypatch, datetime.datetime(2026, 10, 13, 9, 30, 15))
    for _ in range(3):
        (home / "collections.json").write_bytes(b"{bad")
        lib.list()
    names = [p.name for p in _corrupt_files(home)]
    assert names == [
        "collections.json.corrupt-20261013-093015",
        "collections.json.corrupt-20261013-093015-2",
        "collections.json.corrupt-20261013-093015-3",
    ]


def test_quarantine_failure_raises_instead_of_overwriting(lib, home, monkeypatch):
    home.mkdir(parents=True)
    (home / "collections.json").write_bytes(b"{bad")

    def fail(src, dst):
        raise PermissionError("locked")

    monkeypatch.setattr(library_module.os, "replace", fail)
    with pytest.raises(DataError):
        lib.list()
    assert (home / "collections.json").read_bytes() == b"{bad"


def test_newer_registry_version_is_refused_untouched(lib, home):
    home.mkdir(parents=True)
    content = b'{"version": 2, "collections": []}'
    (home / "collections.json").write_bytes(content)
    with pytest.raises(DataError):
        lib.list()
    assert (home / "collections.json").read_bytes() == content
    assert _corrupt_files(home) == []


def test_partially_invalid_entries_are_dropped_with_backup_copy(lib, home, tmp_path):
    home.mkdir(parents=True)
    db = str(_make_index(tmp_path / "good.db").resolve())
    original = {
        "version": 1,
        "active": "Good",
        "collections": [
            {"name": "Good", "db": db, "created_at": "2026-01-01T00:00:00+00:00"},
            {"name": "", "db": "x.db"},
            {"name": "NoDb"},
            "not an object",
            {"name": "good", "db": "dupe.db"},
        ],
    }
    raw = json.dumps(original).encode("utf-8")
    (home / "collections.json").write_bytes(raw)
    items = lib.list()
    assert [item["name"] for item in items] == ["Good"]
    assert items[0]["active"] is True
    (corrupt,) = _corrupt_files(home)
    assert corrupt.read_bytes() == raw
    # The cleaned registry was rewritten, so further reads make no new copies.
    lib.list()
    lib.get("good")
    assert len(_corrupt_files(home)) == 1
    assert [c["name"] for c in _registry(home)["collections"]] == ["Good"]


def test_legacy_entries_without_slug_or_created_at(lib, home, tmp_path):
    home.mkdir(parents=True)
    a = str(_make_index(tmp_path / "a.db").resolve())
    b = str(_make_index(tmp_path / "b.db").resolve())
    c = str(_make_index(tmp_path / "c.db").resolve())
    data = {
        "version": 1,
        "active": None,
        "collections": [
            {"name": "Notes!", "db": a},
            {"name": "Notes?", "db": b, "slug": "notes"},
            {"name": "Other", "db": c, "slug": "notes"},
        ],
    }
    (home / "collections.json").write_text(json.dumps(data), encoding="utf-8")
    items = lib.list()
    slugs = [item["slug"] for item in items]
    assert len(set(slugs)) == 3
    assert slugs[1] == "notes"  # a stored slug wins over a generated one
    assert items[0]["created_at"] == ""
    assert items[0]["active"] is True  # null pointer -> first collection
    assert _corrupt_files(home) == []


def test_registry_with_bom_and_missing_collections_key(lib, home):
    home.mkdir(parents=True)
    (home / "collections.json").write_bytes(b'\xef\xbb\xbf{"version": 1}')
    assert lib.list() == []
    assert _corrupt_files(home) == []


def test_registry_written_as_utf8_json(lib, home):
    lib.create("Café 日本")
    text = (home / "collections.json").read_text(encoding="utf-8")
    assert "Café 日本" in text
    assert json.loads(text)["collections"][0]["name"] == "Café 日本"


# -- backups -------------------------------------------------------------------


def test_backup_path_for_format_and_dir(lib, monkeypatch):
    lib.create("My Notes")
    _freeze(monkeypatch, datetime.datetime(2026, 10, 13, 14, 5, 9))
    path = lib.backup_path_for("my notes")
    assert path == lib.backups_dir / "my-notes-20261013-140509.db"
    assert lib.backups_dir.is_dir()
    assert not path.exists()


def test_backup_path_for_same_second_suffixes(lib, monkeypatch):
    lib.create("Notes")
    _freeze(monkeypatch, datetime.datetime(2026, 10, 13, 14, 5, 9))
    first = lib.backup_path_for("Notes")
    first.write_bytes(b"1")
    second = lib.backup_path_for("Notes")
    assert second.name == "notes-20261013-140509-2.db"
    second.write_bytes(b"2")
    Path(lib.backups_dir / "notes-20261013-140509-3.db-wal").write_bytes(b"w")
    assert lib.backup_path_for("Notes").name == "notes-20261013-140509-4.db"


def test_backup_path_for_unknown(lib):
    with pytest.raises(DataError):
        lib.backup_path_for("ghost")


def test_backup_path_uses_stable_slug_after_rename(lib, monkeypatch):
    lib.create("Notes")
    lib.rename("Notes", "Journal")
    _freeze(monkeypatch, datetime.datetime(2026, 1, 2, 3, 4, 5))
    assert lib.backup_path_for("Journal").name == "notes-20260102-030405.db"


def test_list_backups_empty_without_dir(lib):
    assert lib.list_backups() == []


def _write_backup(lib, name, content=b"x", mtime=None):
    path = lib.backups_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_list_backups_newest_first_with_metadata(lib):
    lib.create("Notes")
    lib.create("Work Stuff")
    base = 1_760_000_000
    _write_backup(lib, "notes-20261001-100000.db", b"aa", base)
    _write_backup(lib, "work-stuff-20261002-100000.db", b"bbbb", base + 100)
    _write_backup(lib, "notes-20261003-100000.db", b"c", base + 200)
    _write_backup(lib, "random.db", b"zz", base + 50)
    _write_backup(lib, "notes-20261003-100000.txt", b"ignored", base + 300)
    (lib.backups_dir / "folder.db").mkdir()

    items = lib.list_backups()
    names = [Path(item["path"]).name for item in items]
    assert names == [
        "notes-20261003-100000.db",
        "work-stuff-20261002-100000.db",
        "random.db",
        "notes-20261001-100000.db",
    ]
    by_name = {Path(item["path"]).name: item for item in items}
    assert by_name["work-stuff-20261002-100000.db"]["collection"] == "work-stuff"
    assert by_name["work-stuff-20261002-100000.db"]["bytes"] == 4
    assert by_name["random.db"]["collection"] is None
    for item in items:
        assert set(item) == {"path", "bytes", "created_at", "collection"}
        assert Path(item["path"]).is_absolute()
        parsed = datetime.datetime.fromisoformat(item["created_at"])
        assert parsed.tzinfo is not None

    notes = lib.list_backups("NOTES")
    assert [Path(item["path"]).name for item in notes] == [
        "notes-20261003-100000.db",
        "notes-20261001-100000.db",
    ]
    assert [Path(i["path"]).name for i in lib.list_backups("Work Stuff")] == [
        "work-stuff-20261002-100000.db"
    ]


def test_list_backups_same_mtime_orders_by_sequence(lib):
    lib.create("Notes")
    when = 1_760_000_000
    for name in [
        "notes-20261003-100000.db",
        "notes-20261003-100000-2.db",
        "notes-20261003-100000-10.db",
        "notes-20261003-100000-9.db",
    ]:
        _write_backup(lib, name, mtime=when)
    names = [Path(item["path"]).name for item in lib.list_backups("Notes")]
    assert names == [
        "notes-20261003-100000-10.db",
        "notes-20261003-100000-9.db",
        "notes-20261003-100000-2.db",
        "notes-20261003-100000.db",
    ]


def test_list_backups_slug_parsing_edge_cases(lib):
    lib.create("Notes")
    _write_backup(lib, "notes-2-20261003-100000.db")
    _write_backup(lib, "notes-20261003-100000-3.db")
    _write_backup(lib, "notes-20261399-100000.db")  # invalid month
    _write_backup(lib, "Notes-20261003-100000.db")  # uppercase is not a slug
    parsed = {Path(i["path"]).name: i["collection"] for i in lib.list_backups()}
    assert parsed["notes-2-20261003-100000.db"] == "notes-2"
    assert parsed["notes-20261003-100000-3.db"] == "notes"
    assert parsed["notes-20261399-100000.db"] is None
    assert parsed["Notes-20261003-100000.db"] is None
    assert len(lib.list_backups("Notes")) == 1


def test_list_backups_for_removed_collection_by_slug(lib):
    lib.create("Old Stuff")
    _write_backup(lib, "old-stuff-20261003-100000.db")
    lib.remove("Old Stuff")
    items = lib.list_backups("old stuff")
    assert len(items) == 1 and items[0]["collection"] == "old-stuff"
    with pytest.raises(UsageError):
        lib.list_backups("   ")


def test_backup_roundtrip_with_real_paths(lib, monkeypatch):
    entry = lib.create("Notes")
    _freeze(monkeypatch, datetime.datetime(2026, 10, 13, 8, 0, 0))
    dest = lib.backup_path_for("Notes")
    dest.write_bytes(Path(entry["db"]).read_bytes())
    (item,) = lib.list_backups("Notes")
    assert Path(item["path"]) == dest.resolve()
    assert item["bytes"] == dest.stat().st_size
    assert item["collection"] == "notes"
    # A backup copy can be opened again as its own collection.
    restored = lib.register("Notes restored", dest)
    assert restored["exists"] is True
