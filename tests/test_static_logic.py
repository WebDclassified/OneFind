"""Behaviour of the web UI's pure helpers, run in Node against the real modules.

The modules are imported unchanged; only ``window.matchMedia`` (read at
import time by a few views) is stubbed. Flows that need a browser are
covered live by ``tools/ui_check.py``; the source checks at the end guard
the wiring those flows depend on.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src" / "onefind" / "static"
DAY = 86400

node_only = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

PRELUDE = """
globalThis.window = {
  matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
  location: { origin: "http://127.0.0.1", href: "http://127.0.0.1/", search: "" },
  setTimeout, clearTimeout,
};
const load = (name) => import(new URL(name, STATIC).href);
"""


def run_node(body: str):
    """Run an async JS body with ``load(module)`` available; returns ``out``."""
    script = (
        f"const STATIC = {json.dumps(STATIC.as_uri() + '/')};\n"
        + PRELUDE
        + "let out = null;\n"
        + body
        + "\nprocess.stdout.write(JSON.stringify(out));\n"
    )
    result = subprocess.run(
        ["node", "--input-type=module"],
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


# -- saved and recent searches keep their date preset ------------------------------------


@node_only
def test_saved_date_presets_do_not_widen_over_time():
    t0 = 1_790_000_000_000  # ms
    out = run_node(f"""
const store = await load("store.js");
const t0 = {t0};
const iso = new Date(t0).toISOString();
out = {{}};
for (const preset of ["7d", "30d", "year"]) {{
  const payload = store.filtersPayload({{ ...store.emptyFilters(), modified: preset }}, t0);
  out[preset] = {{
    // re-run 1, 2, 10, 40 and 400 days after it was saved
    later: [1, 2, 10, 40, 400].map((days) => store.filtersFromPayload(payload, iso, t0 + days * {DAY}e3).modified),
    // an entry without a usable timestamp falls back to "now" (the old reading)
    noStamp: store.filtersFromPayload(payload, null, t0).modified,
    badStamp: store.filtersFromPayload(payload, "not a date", t0).modified,
  }};
}}
out.none = store.filtersFromPayload({{ tags: ["x"] }}, iso, t0);
""")
    for preset in ("7d", "30d", "year"):
        assert out[preset]["later"] == [preset] * 5, (preset, out[preset])
        assert out[preset]["noStamp"] == preset
        assert out[preset]["badStamp"] == preset
    assert out["none"]["modified"] == "any" and out["none"]["tags"] == ["x"]


def test_idle_lists_pass_the_entry_timestamp_when_restoring_filters():
    idle = _read("views-idle.js")
    assert "filtersFromPayload(item.filters, item.searched_at)" in idle
    assert "filtersFromPayload(item.filters, item.updated_at || item.created_at)" in idle
    assert "filtersFromPayload(item.filters)" not in idle


# -- index wizard ------------------------------------------------------------------------


@node_only
def test_wizard_keeps_saved_exclude_patterns_unless_the_field_was_edited():
    out = run_node("""
const { wizardIndexBody } = await load("views-index.js");
out = {
  untouched: wizardIndexBody({ path: "C:\\\\notes", excludeText: "", excludeEdited: false }),
  edited: wizardIndexBody({ path: "C:\\\\notes", excludeText: "drafts, archive/*\\nlogs", excludeEdited: true }),
  cleared: wizardIndexBody({ path: "C:\\\\notes", excludeText: "  ", excludeEdited: true }),
  file: wizardIndexBody({ path: "C:\\\\notes\\\\a.md", file: true, excludeText: "drafts", excludeEdited: true }),
  keyword: wizardIndexBody({ path: "/notes", embed: false }),
};
""")
    # An untouched field must not send [] (that would erase the stored patterns).
    assert "exclude" not in out["untouched"]
    assert out["untouched"] == {"path": "C:\\notes", "embed": None, "prune": True, "full": False,
                                "scope": "folder"}
    assert out["edited"]["exclude"] == ["drafts", "archive/*", "logs"]
    assert out["cleared"]["exclude"] == []  # emptied on purpose
    assert "exclude" not in out["file"]  # never replace the parent folder's patterns
    # A folder picked on purpose is indexed whole; a single file keeps the server default.
    assert "scope" not in out["file"]
    assert out["keyword"]["embed"] is False


@node_only
def test_reindex_skips_folders_that_no_longer_exist():
    out = run_node("""
const { splitRoots } = await load("views-index.js");
out = [
  splitRoots({ roots: [
    { root: "C:\\\\gone", exists: false, root_id: 1 },
    { root: "C:\\\\notes", exists: true, root_id: 2 },
    { root: "C:\\\\older", root_id: 3 },
    { root: "", exists: true },
    null,
  ] }),
  splitRoots(null),
];
""")
    assert out[0] == {"roots": ["C:\\notes", "C:\\older"], "missing": ["C:\\gone"]}
    assert out[1] == {"roots": [], "missing": []}


@node_only
def test_failed_job_offers_the_servers_next_step():
    out = run_node("""
const { failedActionKind } = await load("views-index.js");
const job = (action) => ({ status: "failed", error: { message: "x", action } });
out = {
  switchMode: failedActionKind(job("switch_mode")),
  switchModeWithVectors: failedActionKind(job("switch_mode"), { hasVectors: true }),
  repair: failedActionKind(job("repair")),
  openCollection: failedActionKind(job("open_collection")),
  index: failedActionKind(job("index")),
  retry: failedActionKind(job("retry")),
  none: failedActionKind({ status: "failed" }),
  cancelled: failedActionKind(job("switch_mode"), { cancelled: true }),
};
""")
    assert out == {
        "switchMode": "keyword",
        "switchModeWithVectors": "repair",
        "repair": "repair",
        "openCollection": "open_collection",
        "index": None,
        "retry": None,
        "none": None,
        "cancelled": None,
    }


@node_only
def test_wizard_can_pick_single_files_and_jsonl_datasets():
    out = run_node("""
const m = await load("views-index.js");
out = {
  winParent: m.parentPath("C:\\\\Users\\\\me\\\\notes\\\\a.md"),
  posixParent: m.parentPath("/home/me/notes/a.md"),
  rootParent: m.parentPath("/a.md"),
  stem: [m.stemName("C:\\\\x\\\\Trip notes.md"), m.stemName("/x/data.jsonl"), m.stemName("/x/.hidden")],
  pickable: [
    m.isPickableFile({ kind: "file", name: "a.md", supported: true }),
    // The server marks .jsonl datasets supported (GET /api/fs/list); no name check here.
    m.isPickableFile({ kind: "file", name: "set.JSONL", supported: true }),
    m.isPickableFile({ kind: "file", name: "old-server.jsonl", supported: false }),
    m.isPickableFile({ kind: "file", name: "photo.png", supported: false }),
    m.isPickableFile({ kind: "dir", name: "notes", supported: false }),
  ],
};
""")
    assert out["winParent"] == "C:\\Users\\me\\notes"
    assert out["posixParent"] == "/home/me/notes"
    assert out["rootParent"] == "/"
    assert out["stem"] == ["Trip notes", "data", ".hidden"]
    assert out["pickable"] == [True, True, False, False, False]


def test_wizard_offers_a_typed_file_instead_of_saying_it_does_not_exist():
    index = _read("views-index.js")
    assert "is a file, not a folder" in index
    # Files in the browser are buttons now, not inert list items.
    assert 'class: "browser-item browser-file-button"' in index
    assert 'el("li", { class: "browser-file" }' not in index


# -- preview lands on the match -----------------------------------------------------------


@node_only
def test_preview_finds_the_paragraph_that_holds_the_snippet():
    out = run_node("""
const { snippetParagraphIndex } = await load("views-preview.js");
const paragraphs = [
  "# Title",
  "A quokka was mentioned once at the start.",
  "Day 68. Notes about the weekly market and the warm weather.",
  "The quokka is a small marsupial found on Rottnest Island,\\nwhere it is famous for its smile.",
];
// A passage that starts in one paragraph and runs into the next (as search snippets do).
const spanning = [
  { text: "… Day 68. Notes about the weekly market and the warm weather. The ", highlight: false },
  { text: "quokka", highlight: true },
  { text: " is a small   marsupial found on ", highlight: false },
  { text: "Rottnest", highlight: true },
  { text: " Island, where it …", highlight: false },
];
out = [
  snippetParagraphIndex(paragraphs, spanning),
  snippetParagraphIndex(paragraphs, "… The quokka is a small   marsupial found on Rottnest Island, where it …"),
  snippetParagraphIndex(paragraphs, [{ text: "nothing like this appears", highlight: false }]),
  snippetParagraphIndex(paragraphs, "nothing like this appears"),
  snippetParagraphIndex(paragraphs, "…"),
  snippetParagraphIndex(paragraphs, []),
];
""")
    assert out == [3, 3, -1, -1, -1, -1]


def test_preview_jumps_to_the_match_when_a_document_opens():
    preview = _read("views-preview.js")
    assert "renderBody({ jumpToMatch: true })" in preview
    # The find box keeps re-rendering without jumping.
    assert "window.setTimeout(renderBody, 120)" in preview


# -- wiring that the live checks exercise --------------------------------------------------


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(^|[^:\\\"'])//[^\n]*", r"\1", text)


def test_a_url_query_is_shown_but_not_recorded_in_history():
    app = _strip_comments(_read("app.js"))
    assert "applyQuery({ ...fromUrl, record: false, deferRecord: false })" in app
    assert "record: true" not in app.split("async function bootstrap", 1)[1]
    search = _strip_comments(_read("search.js"))
    assert "if (!record && deferRecord) scheduleHistoryCommit" in search
    # Typing still records a paused live search (deferRecord defaults to !record).
    assert "runSearch({ record = true, deferRecord = !record } = {})" in search


def test_short_query_aborts_the_running_search():
    search = _strip_comments(_read("search.js"))
    branch = search.split("if (query.length < MIN_LIVE_CHARS) {", 1)[1].split("live();", 1)[0]
    assert "abortInFlight()" in branch
    helper = search.split("function abortInFlight()", 1)[1].split("\n}\n", 1)[0]
    for needed in ("controller.abort()", "requestSeq += 1", "cancelPipeline()", "setBusy(false)"):
        assert needed in helper


def test_job_updates_do_not_postpone_the_busy_stats_refresh():
    app = _strip_comments(_read("app.js"))
    assert "schedulePoll({ keepBusy: true })" in app
    assert "if (keepBusy && pollTimer && pollDelay <= delay) return;" in app


def test_collection_switch_drops_the_folder_queue():
    app = _strip_comments(_read("app.js"))
    changed = app.split("async function onCollectionChanged", 1)[1].split("\n}\n", 1)[0]
    assert "resetJobQueue()" in changed
    index = _strip_comments(_read("views-index.js"))
    finished = index.split("function onJobFinished(job)", 1)[1].split("\n}\n", 1)[0]
    assert "queueStillValid()" in finished


def test_start_searching_keeps_focus_in_the_search_box():
    dialogs = _read("dialogs.js")
    assert "if (!restore) returnTargets.delete(dialog);" in dialogs
    index = _read("views-index.js")
    start = index.split("function startSearching()", 1)[1].split("\n}\n", 1)[0]
    assert 'closeDialog(dialog(), "", { restoreFocus: false })' in start


def test_idle_and_manage_refreshes_ignore_stale_answers():
    idle = _read("views-idle.js")
    body = idle.split("export async function refreshIdle()", 1)[1].split("\n}\n", 1)[0]
    assert body.index("++idleSeq") < body.index("if (!canSearch()) return;")
    assert "if (seq !== idleSeq) return;" in body
    manage = _read("views-manage.js")
    assert "if (seq !== manageSeq) return;" in manage


def test_drawer_locks_page_scroll():
    css = _read("styles.css")
    assert re.search(r"body\.has-drawer\s*\{[^}]*overflow:\s*hidden", css)


def test_documents_can_be_removed_from_the_index_in_the_ui():
    results = _read("views-results.js")
    assert "api.removeDocuments([result.docId])" in results
    assert 'menuItem("Remove from index…"' in results
    assert "The file on disk is not touched" in results
    preview = _read("views-preview.js")
    assert "removeFromIndex(asResult" in preview


def test_portability_copy_says_what_does_not_travel():
    manage = _read("views-manage.js")
    assert "What does not travel" in manage
    assert "indexed a second time" in manage
    assert "updating the index both need those folders" in manage


# -- network folders: the UI asks, then resends with confirm_network ----------------------


@node_only
def test_network_confirmation_is_only_asked_for_the_servers_confirm_network_answer():
    out = run_node("""
const { needsNetworkConfirm, withNetworkConfirm } = await load("dialogs.js");
const { ApiError } = await load("api.js");
const asked = new ApiError("That folder is on a network location (nas). Continue anyway?",
  { code: "confirmation_required", status: 409, action: "confirm_network" });
const other = new ApiError("Type the name", { code: "confirmation_required", status: 400 });
const calls = [];
const ok = await withNetworkConfirm(async (confirm) => { calls.push(confirm); return { id: "j1" }; });
let rethrown = null;
try {
  await withNetworkConfirm(async (confirm) => { calls.push(confirm); throw other; });
} catch (error) { rethrown = error.code; }
out = {
  asked: needsNetworkConfirm(asked),
  other: needsNetworkConfirm(other),
  none: needsNetworkConfirm(null),
  ok, calls, rethrown,
};
""")
    assert out["asked"] is True
    assert out["other"] is False and out["none"] is False
    assert out["ok"] == {"id": "j1"}
    assert out["calls"] == [False, False]  # never pre-confirmed
    assert out["rethrown"] == "confirmation_required"


def test_network_confirmation_is_wired_into_open_and_index():
    api = _read("api.js")
    assert "confirm_network: true" in api
    assert "withNetworkConfirm((confirm) => api.openDocument(" in _read("views-results.js")
    assert _read("views-index.js").count("withNetworkConfirm((confirm) => api.startIndex(") == 2


def test_history_counts_strong_results_only():
    assert "scheduleHistoryCommit(body, view.strong.length)" in _read("search.js")


# -- auto-sync (contract section 10) -------------------------------------------------------


@node_only
def test_auto_sync_pill_stays_subtle():
    out = run_node("""
const header = await load("views-header.js");
const ready = { status: "ready", documents: 3, vectors: 0, status_detail: "3 notes ready." };
out = {
  off: header.describeStatus({ ...ready, auto_sync: { enabled: false, state: "idle" } }),
  on: header.describeStatus({ ...ready, auto_sync: { enabled: true, state: "idle", pending: {} } }),
  syncing: header.describeStatus({ ...ready, auto_sync: { enabled: true, state: "syncing", pending: { new: 2, changed: 1, deleted: 0 } } }),
  settling: header.describeStatus({ ...ready, auto_sync: { enabled: true, state: "scanning", pending: { new: 1 } } }),
  checking: header.describeStatus({ ...ready, auto_sync: { enabled: true, state: "scanning", pending: {} } }),
  error: header.describeStatus({ ...ready, auto_sync: { enabled: true, state: "error", message: "Disk gone." } }),
  paused: header.describeStatus({ ...ready, auto_sync: { enabled: true, state: "paused", message: "Waiting." } }),
  indexing: header.describeStatus({ ...ready, status: "indexing", auto_sync: { enabled: true, state: "paused" } }),
};
""")
    assert out["off"]["label"] == "Ready" and not out["off"].get("sync")
    assert out["on"]["label"] == "Ready" and out["on"]["sync"] == "Auto-sync on" and out["on"]["tone"] == "ok"
    assert out["syncing"]["label"] == "Syncing 3 changes…" and out["syncing"]["tone"] == "busy"
    assert out["syncing"]["quiet"] is True  # never announced to screen readers
    assert out["settling"]["label"] == "Syncing 1 change…"
    assert out["checking"]["label"] == "Ready"  # a routine check is invisible
    assert out["error"]["sync"] == "Auto-sync problem" and out["error"]["tone"] == "warn"
    assert "Disk gone." in out["error"]["detail"]
    assert out["paused"]["sync"] == "Auto-sync paused"
    assert out["indexing"]["label"].startswith("Indexing") and not out["indexing"].get("sync")


@node_only
def test_manage_auto_sync_line_and_wizard_defaults():
    out = run_node("""
const { autoSyncLine } = await load("views-manage.js");
const { wizardAutoSyncDefault, wizardIndexBody } = await load("views-index.js");
const { autoSyncFinished } = await load("store.js");
const now = Date.parse("2026-10-14T12:00:00Z");
out = {
  off: autoSyncLine({ enabled: false }, now),
  offWithHistory: autoSyncLine({ enabled: false, last_sync_at: "2026-10-14T11:58:00Z" }, now),
  on: autoSyncLine({ enabled: true, state: "idle" }, now),
  synced: autoSyncLine({ enabled: true, state: "idle", last_sync_at: "2026-10-14T11:58:00Z", last_changes: { added: 1, updated: 1, removed: 0 } }, now),
  syncing: autoSyncLine({ enabled: true, state: "syncing", pending: { new: 2 } }, now),
  error: autoSyncLine({ enabled: true, state: "error", message: "Disk gone." }, now),
  newCollection: wizardAutoSyncDefault({ collection: { name: "A" }, documents: 10, auto_sync: { enabled: false } }, { newCollection: true }),
  empty: wizardAutoSyncDefault({ collection: { name: "A" }, documents: 0, auto_sync: { enabled: false } }),
  none: wizardAutoSyncDefault(null),
  existingOff: wizardAutoSyncDefault({ collection: { name: "A" }, documents: 10, auto_sync: { enabled: false } }),
  existingOn: wizardAutoSyncDefault({ collection: { name: "A" }, documents: 10, auto_sync: { enabled: true } }),
  bodyOn: wizardIndexBody({ path: "/notes", autoSync: true }),
  bodyOff: wizardIndexBody({ path: "/notes", autoSync: false }),
  bodyUnset: wizardIndexBody({ path: "/notes" }),
  first: autoSyncFinished({ collection: null, syncs: null }, { collection: { name: "A" }, auto_sync: { syncs: 2 } }),
  more: autoSyncFinished({ collection: "A", syncs: 2 }, { collection: { name: "A" }, auto_sync: { syncs: 3 } }),
  switched: autoSyncFinished({ collection: "A", syncs: 2 }, { collection: { name: "B" }, auto_sync: { syncs: 5 } }),
  same: autoSyncFinished({ collection: "A", syncs: 3 }, { collection: { name: "A" }, auto_sync: { syncs: 3 } }),
};
""")
    assert out["off"].startswith("Off.") and "Update index" in out["off"]
    assert out["offWithHistory"] == "Off. Last synced 2 min ago."
    assert out["on"] == "On · watching your folders for changes."
    assert out["synced"] == "On · last synced 2 min ago (2 notes changed)."
    assert out["syncing"] == "Syncing 2 changes now…"
    assert out["error"] == "Disk gone."
    assert out["newCollection"] is True and out["empty"] is True and out["none"] is True
    assert out["existingOff"] is False and out["existingOn"] is True
    assert out["bodyOn"]["auto_sync"] is True and out["bodyOff"]["auto_sync"] is False
    assert "auto_sync" not in out["bodyUnset"]
    assert out["first"]["finished"] is False and out["first"]["next"] == {"collection": "A", "syncs": 2}
    assert out["more"]["finished"] is True
    assert out["switched"]["finished"] is False and out["same"]["finished"] is False


def test_auto_sync_is_wired_into_the_ui():
    api_code = _read("api.js")
    assert '"/api/autosync"' in api_code and 'post("/api/autosync"' in api_code
    html = _read("index.html")
    for element_id in ("status-sync", "index-autosync", "autosync-toggle", "autosync-status", "manage-autosync"):
        assert f'id="{element_id}"' in html, element_id
    assert "Everything stays on this computer." in html
    assert "Keep this collection up to date automatically" in html
    app = _read("app.js")
    assert "noticeAutoSync(stats)" in app and "POLL_AUTOSYNC_MS" in app
    # A finished sync never interrupts: no toast with an action, only a short info toast.
    sync_block = app[app.index("async function onAutoSynced"):]
    sync_block = sync_block[:sync_block.index("\n}\n")]
    assert "action:" not in sync_block and 'tone: "info"' in sync_block


# -- v1.2.1: passages, rebuild vectors, saved exclusions, collection guard -------------------


@node_only
def test_wizard_prefills_a_folders_saved_exclusions():
    out = run_node("""
const m = await load("views-index.js");
const roots = [
  { root_id: 1, path: "C:\\\\Users\\\\me\\\\Notes", exclude: ["drafts", "archive/*"] },
  { root_id: 2, path: "/home/me/notes", exclude: [] },
  { root_id: 3, path: "/home/me/Work", exclude: ["tmp"] },
];
out = {
  exact: m.savedExcludesFor(roots, "C:\\\\Users\\\\me\\\\Notes"),
  windowsCase: m.savedExcludesFor(roots, "c:/users/me/notes/"),
  none: m.savedExcludesFor(roots, "/home/me/notes"),
  posixCase: m.savedExcludesFor(roots, "/home/me/work"),
  subfolder: m.savedExcludesFor(roots, "C:\\\\Users\\\\me\\\\Notes\\\\drafts"),
  unknown: m.savedExcludesFor(roots, "/elsewhere"),
  noRoots: m.savedExcludesFor(null, "/home/me/notes"),
  same: [m.samePath("/a/b/", "/a/b"), m.samePath("C:\\\\A", "c:/a"), m.samePath("", "")],
  body: m.wizardIndexBody({ path: "/notes", collection: "My Notes" }),
  bodyNoCollection: m.wizardIndexBody({ path: "/notes", collection: null }),
};
""")
    assert out["exact"] == ["drafts", "archive/*"]
    assert out["windowsCase"] == ["drafts", "archive/*"]
    assert out["none"] == []  # a known folder without patterns
    assert out["posixCase"] is None  # POSIX paths are case-sensitive
    assert out["subfolder"] is None  # only the folder itself
    assert out["unknown"] is None and out["noRoots"] is None
    assert out["same"] == [True, True, False]
    assert out["body"]["collection"] == "My Notes"
    assert "collection" not in out["bodyNoCollection"]


def test_wizard_shows_saved_exclusions_without_sending_them():
    index = _read("views-index.js")
    assert "loadSavedRoots()" in index and "applySavedExcludes()" in index
    # Pre-filling must not count as an edit: an untouched field keeps the saved patterns.
    block = index[index.index("function applySavedExcludes"):]
    block = block[:block.index("\n}\n")]
    assert "excludeEdited = true" not in block and "if (excludeEdited) return;" in block
    assert "Editing them replaces the saved patterns" in block
    assert 'id="index-options-summary"' in _read("index.html")
    assert '"/api/roots"' in _read("api.js")
    # Queued folder updates carry the collection they were queued for.
    assert "collection: queueCollection" in index


@node_only
def test_rebuild_jobs_have_their_own_labels_and_counts():
    out = run_node("""
const m = await load("views-index.js");
const header = await load("views-header.js");
const rebuild = (phase, done, total) => ({ kind: "rebuild_vectors", status: "running", phase, done, total });
out = {
  labels: ["scan", "index", "vectors", "ready"].map((name) => m.stepLabel(name, "rebuild_vectors")),
  indexLabels: ["scan", "index", "vectors", "ready"].map((name) => m.stepLabel(name)),
  scan: m.jobCountText(rebuild("scan", 7, 7)),
  scanning: m.jobCountText(rebuild("scan", 0, null)),
  embed: m.jobCountText(rebuild("embed", 3, 7)),
  model: m.jobCountText(rebuild("model", 0, null)),
  indexEmbed: m.jobCountText({ kind: "index", phase: "embed", done: 3, total: 7 }),
  summary: m.rebuildSummaryRows({ rebuilt: 3, passages: 12, vectors_embedded: 12 }),
  summaryNothing: m.rebuildSummaryRows({ rebuilt: 0, passages: 0, vectors_embedded: 0 }),
  pill: header.describeStatus({ status: "indexing", collection: { name: "A" } }, rebuild("embed", 3, 7)).label,
  indexPill: header.describeStatus({ status: "indexing", collection: { name: "A" } }, { kind: "index", status: "running", phase: "read", done: 3, total: 7 }).label,
};
""")
    assert out["labels"] == ["Find long notes", "Split long notes", "Build vectors", "Ready"]
    assert out["indexLabels"] == ["Scan files", "Index text", "Build vectors", "Ready"]
    assert out["scan"] == "7 long notes to update"
    assert out["scanning"] == "Looking for long notes…"
    assert out["embed"] == "3 of 7 notes"
    assert out["model"] == "Loading the local model…"
    assert out["indexEmbed"] == "3 of 7 vectors"
    assert out["summary"] == [["Notes updated", 3], ["Passages", 12], ["Vectors built", 12]]
    assert out["summaryNothing"] == [["Notes updated", 0]]
    assert out["pill"] == "Rebuilding 43%"
    assert out["indexPill"] == "Indexing 43%"


@node_only
def test_manage_offers_a_rebuild_or_passage_search_for_long_notes():
    out = run_node("""
const m = await load("views-manage.js");
const base = { collection: { name: "A" }, documents: 50, vectors: 80 };
const needed = { needed: true, documents: 4, reason: "4 notes are longer than one passage but still have a single whole-note vector.", recommended: false, long_notes: 4 };
const recommended = { needed: false, documents: 0, reason: "", recommended: true, long_notes: 6 };
out = {
  rebuild: m.passageOffer({ ...base, vectors_need_rebuild: needed }),
  improve: m.passageOffer({ ...base, long_notes: 6, vectors_need_rebuild: recommended }),
  keywordOnly: m.passageOffer({ ...base, vectors: 0, long_notes: 6, vectors_need_rebuild: recommended }),
  nothing: m.passageOffer({ ...base, vectors_need_rebuild: { needed: false, documents: 0, recommended: false, long_notes: 0 } }),
  oldServer: m.passageOffer(base),
  offline: m.passageOffer({ ...base, offline: true, vectors_need_rebuild: needed }),
  on: m.longNotesText({ long_notes: 3, chunking: { version: 1, max_words: 200, overlap_words: 40 } }),
  onNone: m.longNotesText({ long_notes: 0, chunking: { version: 1, max_words: 200, overlap_words: 40 } }),
  off: m.longNotesText({ long_notes: 6, chunking: null }),
  offNone: m.longNotesText({ long_notes: 0, chunking: null }),
};
""")
    assert out["rebuild"]["kind"] == "rebuild" and out["rebuild"]["notes"] == 4
    assert out["rebuild"]["reason"].startswith("4 notes")
    assert out["improve"] == {"kind": "improve", "notes": 6, "reason": ""}
    assert out["keywordOnly"] is None  # nothing to gain without meaning search
    assert out["nothing"] is None and out["oldServer"] is None and out["offline"] is None
    assert out["on"] == "3 notes longer than 200 words are searched passage by passage"
    assert out["onNone"] == "Split into passages when longer than 200 words"
    assert out["off"] == "Meaning search reads only the beginning of 6 long notes"
    assert out["offNone"] == "Each note has one vector"


def test_manage_wires_the_rebuild_actions():
    manage = _read("views-manage.js")
    assert "Improve meaning search for long notes" in manage
    assert 'startRebuild("default"' in manage and 'startRebuild("keep"' in manage
    assert '"Rebuild vectors"' in manage
    # replaceChildren(null) would print "null": the optional callout is filtered out.
    health = manage[manage.index("function renderHealth"):]
    health = health[:health.index("\n}\n")]
    assert "passageCallout(stats)" in health and "].filter(Boolean));" in health
    api_code = _read("api.js")
    assert 'post("/api/maintenance/rebuild-vectors", { chunking })' in api_code
    index = _read("views-index.js")
    assert "rebuildVectors: (options) => rebuildVectors(options)" in index
    assert "api.rebuildVectors(chunking)" in index


@node_only
def test_preview_finds_the_passage_a_meaning_match_came_from():
    out = run_node("""
const { paragraphSpans, passageParagraphRange } = await load("views-preview.js");
const { normalizePassage, normalizeResult } = await load("views-results.js");
const body = "# Journal\\r\\n\\r\\nA quokka at the start.\\r\\n\\r\\nDay 1. Market notes.\\r\\n\\r\\nDay 2. More market notes about bread.\\r\\n\\r\\nThe quokka lives on Rottnest Island.\\r\\n\\r\\nDay 3. Rain.";
// Passages are exact slices of the stored body; they may start mid-paragraph.
const passage = "More market notes about bread.\\n\\nThe quokka lives on Rottnest Island.";
out = {
  spans: paragraphSpans(body).map((span) => span.text),
  exact: passageParagraphRange(body, passage),
  loose: passageParagraphRange(body, "more  market notes about bread.\\nThe quokka lives on   Rottnest Island."),
  whole: passageParagraphRange(body, body),
  missing: passageParagraphRange(body, "Nothing like this is in the note at all."),
  empty: passageParagraphRange(body, ""),
  normalized: normalizePassage({ seq: 4, count: 5, text: "x" }),
  bad: [normalizePassage(null), normalizePassage({ seq: 0, count: 1, text: "x" }), normalizePassage({ seq: 5, count: 5, text: "x" }), normalizePassage({ seq: 1, count: 2, text: " " })],
  result: normalizeResult({ doc_id: "a.md", passage: { seq: 1, count: 3, text: "y" } }).passage,
  plain: normalizeResult({ doc_id: "a.md" }).passage,
};
""")
    assert out["spans"] == ["# Journal", "A quokka at the start.", "Day 1. Market notes.",
                            "Day 2. More market notes about bread.", "The quokka lives on Rottnest Island.", "Day 3. Rain."]
    assert out["exact"] == {"first": 3, "last": 4}
    assert out["loose"] == {"first": 3, "last": 4}
    assert out["whole"] == {"first": 0, "last": 5}
    assert out["missing"] is None and out["empty"] is None
    assert out["normalized"] == {"seq": 4, "count": 5, "text": "x"}
    assert out["bad"] == [None, None, None, None]
    assert out["result"] == {"seq": 1, "count": 3, "text": "y"}
    assert out["plain"] is None


def test_preview_marks_the_passage_and_jumps_inside_it():
    preview = _read("views-preview.js")
    assert "passageParagraphRange(currentDoc?.body, result.passage.text)" in preview
    assert '" is-passage"' in preview and "Closest passage" in preview
    # From a passage the preview never jumps to a highlight elsewhere in the note.
    jump = preview[preview.index("const target = range"):]
    jump = jump[:jump.index(";")]
    passage_branch = jump.split(" : ")[0]
    assert "marks[0]" not in passage_branch and "label" in passage_branch
    css = _read("styles.css")
    assert ".preview-paragraph.is-passage" in css and ".passage-label" in css
