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
    m.isPickableFile({ kind: "file", name: "set.JSONL", supported: false }),
    m.isPickableFile({ kind: "file", name: "photo.png", supported: false }),
    m.isPickableFile({ kind: "dir", name: "notes", supported: false }),
  ],
};
""")
    assert out["winParent"] == "C:\\Users\\me\\notes"
    assert out["posixParent"] == "/home/me/notes"
    assert out["rootParent"] == "/"
    assert out["stem"] == ["Trip notes", "data", ".hidden"]
    assert out["pickable"] == [True, True, False, False]


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
