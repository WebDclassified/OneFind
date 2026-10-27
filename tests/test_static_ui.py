"""Static checks for the packaged web UI: CSP constraints, module wiring,
DOM ids, theming/motion tokens, and the CSRF header on mutating requests."""

from __future__ import annotations

import inspect
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src" / "onefind" / "static"
ASSET_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*\.(js|css|svg)$")
JS_FILES = sorted(STATIC.glob("*.js"))
INDEX = STATIC / "index.html"
IMPORT_RE = re.compile(r"""import\s*(?:\{([^}]*)\}\s*from\s*)?["'](\.{1,2}/[^"']+)["']""", re.S)
EXPORT_RE = re.compile(
    r"export\s+(?:async\s+)?(?:function\*?|const|let|class)\s+([A-Za-z_$][\w$]*)"
)
EXPORT_LIST_RE = re.compile(r"export\s*\{([^}]*)\}")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _strip_js_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(^|[^:\\\"'])//[^\n]*", r"\1", text)


def _exports(text: str) -> set[str]:
    names = set(EXPORT_RE.findall(text))
    for group in EXPORT_LIST_RE.findall(text):
        for item in group.split(","):
            item = item.strip()
            if item:
                names.add(item.split(" as ")[-1].strip())
    return names


def test_static_directory_contains_core_files():
    for name in ("index.html", "app.js", "styles.css", "favicon.svg"):
        assert (STATIC / name).is_file(), name
    assert len(JS_FILES) > 1, "the UI is split into ES modules"


def test_every_asset_name_is_servable():
    for path in STATIC.iterdir():
        if path.name == "index.html" or path.name.startswith("__"):
            continue
        assert ASSET_NAME.match(path.name), f"{path.name} would not be served under /assets/"


def test_index_html_has_no_inline_code_or_styles():
    html = _read(INDEX)
    assert 'id="search-input"' in html
    assert 'src="/assets/app.js"' in html
    assert 'type="module"' in html
    assert not re.search(r"\son[a-z]+\s*=", html, re.I), "inline event handler"
    assert not re.search(r"\sstyle\s*=", html, re.I), "inline style attribute"
    assert "<style" not in html.lower()
    for tag in re.findall(r"<script\b[^>]*>", html, re.I):
        assert "src=" in tag, f"inline script block: {tag}"
    assert "javascript:" not in html.lower()


def test_index_html_ids_are_unique():
    ids = re.findall(r'\sid="([^"]+)"', _read(INDEX))
    duplicates = {item for item in ids if ids.count(item) > 1}
    assert not duplicates


def test_index_html_references_existing_assets():
    for name in re.findall(r'/assets/([^"\'\s>]+)', _read(INDEX)):
        assert (STATIC / name).is_file(), name


def test_old_demo_metaphor_and_showcase_queries_are_gone():
    text = "\n".join(_read(path) for path in [INDEX, *JS_FILES]).lower()
    for phrase in ("waypoint", "route 01", "destination", "prod-001", "edge 005", "data-query="):
        assert phrase not in text, phrase


@pytest.mark.parametrize("path", JS_FILES, ids=lambda p: p.name)
def test_javascript_never_uses_html_sinks(path):
    code = _strip_js_comments(_read(path))
    for sink in (".innerHTML", ".outerHTML", "insertAdjacentHTML", "document.write", "new Function"):
        assert sink not in code, f"{path.name} uses {sink}"
    assert not re.search(r"\beval\s*\(", code), f"{path.name} uses eval"
    assert not re.search(r"""setAttribute\(\s*["']style["']""", code), f"{path.name} sets a style attribute"
    assert not re.search(r"""setAttribute\(\s*["']on[a-z]+["']""", code), f"{path.name} sets an inline handler"


def test_entry_module_uses_safe_dom_and_cancellation_apis():
    code = _strip_js_comments(_read(STATIC / "app.js"))
    assert "replaceChildren" in code
    assert "AbortController" in code


@pytest.mark.parametrize("path", JS_FILES, ids=lambda p: p.name)
def test_imports_resolve_to_existing_modules_and_exports(path):
    code = _read(path)
    for names, target in IMPORT_RE.findall(code):
        module = (path.parent / target).resolve()
        assert module.is_file(), f"{path.name} imports missing {target}"
        assert module.parent == STATIC, f"{path.name} imports outside static/: {target}"
        assert ASSET_NAME.match(module.name), target
        exported = _exports(_read(module))
        for raw in names.split(","):
            name = raw.strip().split(" as ")[0].strip()
            if name:
                assert name in exported, f"{path.name} imports {name!r} which {target} does not export"


def test_every_module_is_reachable_from_the_entry_point():
    seen: set[str] = set()
    pending = ["app.js"]
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        for _names, target in IMPORT_RE.findall(_read(STATIC / name)):
            pending.append(Path(target).name)
    assert seen == {path.name for path in JS_FILES}


def test_ids_used_by_javascript_exist():
    html_ids = set(re.findall(r'\sid="([^"]+)"', _read(INDEX)))
    code = "\n".join(_read(path) for path in JS_FILES)
    dynamic = set(re.findall(r"""\.id\s*=\s*["']([^"']+)["']""", code))
    dynamic |= set(re.findall(r"""\bid:\s*["']([^"']+)["']""", code))
    dynamic |= set(re.findall(r"""resultBox\(\s*["']([^"']+)["']""", code))
    used = set(re.findall(r"""byId\(\s*["']([^"']+)["']\s*\)""", code))
    used |= set(re.findall(r"""data-section=["']?([a-z-]+)""", _read(INDEX)))
    missing = used - html_ids - dynamic
    assert not missing, f"ids referenced but never defined: {sorted(missing)}"


def test_only_the_api_module_talks_to_the_network():
    for path in JS_FILES:
        code = _strip_js_comments(_read(path))
        if path.name == "api.js":
            continue
        assert "fetch(" not in code, f"{path.name} calls fetch directly"
        assert "XMLHttpRequest" not in code


def test_mutating_requests_send_the_csrf_header():
    code = _read(STATIC / "api.js")
    assert '"X-OneFind-Request": "1"' in code
    # The generic request helper adds the header to every request (GETs too:
    # the server requires it on /api/fs/list), and the streaming search adds
    # it explicitly.
    helper = code[code.index("export async function request"):code.index("const get = ")]
    assert "Object.assign(headers, CSRF_HEADER);" in helper
    assert 'method !== "GET"' not in helper
    stream = code[code.index("export async function streamSearch"):]
    assert "CSRF_HEADER" in stream


def test_streaming_search_reads_ndjson_with_fallback():
    api_code = _read(STATIC / "api.js")
    assert "/api/search/stream" in api_code
    assert "getReader()" in api_code
    assert "StreamUnavailable" in _read(STATIC / "search.js")
    assert "api.search(" in _read(STATIC / "search.js")


def test_api_client_covers_the_contract_endpoints():
    code = _read(STATIC / "api.js")
    for endpoint in (
        "/api/stats", "/api/facets", "/api/search", "/api/collections", "/api/collections/open",
        "/api/collections/active", "/api/collections/rename", "/api/collections/remove", "/api/fs/list",
        "/api/index", "/api/jobs", "/api/index/changes", "/api/documents/", "/api/documents/open",
        "/api/history", "/api/history/clear", "/api/saved", "/api/bookmarks", "/api/recent-documents",
        "/api/maintenance/backup", "/api/backups", "/api/maintenance/restore", "/api/maintenance/check",
        "/api/maintenance/repair", "/api/maintenance/compact", "/api/maintenance/clear",
    ):
        assert endpoint in code, endpoint


def test_no_external_resources_are_loaded():
    allowed = {"http://www.w3.org/2000/svg"}
    for path in [INDEX, *JS_FILES, STATIC / "styles.css", STATIC / "favicon.svg"]:
        for url in re.findall(r"https?://[^\s\"')]+", _read(path)):
            assert url in allowed, f"{path.name} references {url}"
    css = _read(STATIC / "styles.css")
    assert "@import" not in css
    assert "@font-face" not in css


def test_css_defines_themes_and_motion_language():
    css = _read(STATIC / "styles.css")
    for token in ("--ease-out: cubic-bezier(.2, .8, .2, 1)", "--dur-1: 120ms", "--dur-2: 200ms", "--dur-3: 320ms"):
        assert token in css, token
    assert ':root[data-theme="dark"]' in css
    assert "prefers-color-scheme: dark" in css
    assert "prefers-reduced-motion: reduce" in css
    assert 'html[data-motion="reduced"]' in css
    assert ":focus-visible" in css
    assert "mark {" in css


def test_theme_preference_storage_is_guarded():
    store = _read(STATIC / "store.js")
    block = store[store.index("export const storage"):]
    assert block.count("try {") >= 3
    assert "localStorage" not in "\n".join(
        _read(path) for path in JS_FILES if path.name != "store.js"
    ), "localStorage must only be touched through the guarded helper"


def test_url_state_keeps_shareable_parameters():
    search = _read(STATIC / "search.js")
    for param in ('"q"', '"mode"', '"precision"', '"k"'):
        assert param in search
    assert "history.replaceState" in search


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("path", JS_FILES, ids=lambda p: p.name)
def test_javascript_modules_parse(path):
    result = subprocess.run(
        ["node", "--input-type=module", "--check"],
        input=_read(path),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


def test_server_serves_every_module(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from onefind.serve import create_app

    assert "home" in inspect.signature(create_app).parameters
    app = create_app(None, home=str(tmp_path / "home"))
    with TestClient(app) as client:
        for path in [*JS_FILES, STATIC / "styles.css", STATIC / "favicon.svg"]:
            response = client.get(f"/assets/{path.name}")
            assert response.status_code == 200, path.name
            if path.suffix == ".js":
                assert "javascript" in response.headers["content-type"]
        assert client.get("/assets/Not-Valid.js").status_code == 404
        assert client.get("/assets/missing-module.js").status_code == 404


# -- v1.2 polish: regressions the design reviews caught ---------------------------------


def _css_block(css: str, selector: str) -> str:
    """The declarations of the first rule whose selector list is exactly `selector`."""
    match = re.search(r"(?:^|\})\s*" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert match, f"no rule for {selector}"
    return match.group(1)


def test_dialogs_are_never_transformed_at_rest_or_while_opening():
    # A transformed dialog becomes the positioning box for the toasts moved
    # inside it, which then cover its own buttons (design review, finding 1).
    css = _read(STATIC / "styles.css")
    for selector in (".dialog", ".dialog[open]", ".sheet", ".sheet[open]"):
        block = _css_block(css, selector)
        assert "transform" not in block and "animation" not in block, selector
    assert "@starting-style" in css


def test_pre_script_dark_theme_defines_every_dark_token():
    css = _read(STATIC / "styles.css")
    dark = set(re.findall(r"(--[a-z0-9-]+):", _css_block(css, ':root[data-theme="dark"]')))
    media = css[css.index("@media (prefers-color-scheme: dark)"):]
    early = set(re.findall(r"(--[a-z0-9-]+):", media[: media.index("\n}\n")]))
    assert dark <= early, f"missing before scripts run: {sorted(dark - early)}"


def test_every_icon_name_used_exists():
    icons = _read(STATIC / "icons.js")
    names = set(re.findall(r"^\s+\"?([a-z-]+)\"?: \[", icons, re.M))
    code = "\n".join(_read(path) for path in JS_FILES)
    used = set(re.findall(r"""iconName:\s*["']([a-z-]+)["']""", code))
    used |= set(re.findall(r"""\bicon\(\s*["']([a-z-]+)["']""", code))
    used |= set(re.findall(r"""\bicon:\s*["']([a-z-]+)["']""", code))
    assert used, "no icon references found"
    assert used <= names, f"unknown icons: {sorted(used - names)}"


def test_header_manage_and_options_use_different_icons():
    html = _read(INDEX)

    def first_svg(element_id: str) -> str:
        start = html.index(f'id="{element_id}"')
        return html[start: html.index("</svg>", start)]

    assert first_svg("manage-button").split("<path")[1:] != first_svg("options-toggle").split("<path")[1:]


def test_filter_pickers_do_not_apply_on_every_arrow_key():
    code = _strip_js_comments(_read(STATIC / "views-filters.js"))
    assert "onFilterSelect(" in code
    for select in ("filter-folder", "filter-type", "filter-tag", "filter-date"):
        assert f'byId("{select}").addEventListener("change"' not in code, select


def test_single_key_shortcuts_can_be_turned_off():
    assert 'id="pref-single-keys"' in _read(INDEX)
    keyboard = _read(STATIC / "keyboard.js")
    assert "singleKeysEnabled()" in keyboard
    assert "storage." in keyboard and "localStorage" not in keyboard


def test_reduced_motion_also_stops_view_transitions():
    css = _read(STATIC / "styles.css")
    assert 'html[data-motion="reduced"]::view-transition-group(*)' in css
    motion = _read(STATIC / "motion.js")
    view_transition = motion[motion.index("export function withViewTransition"):]
    assert "reducedMotion()" in view_transition[: view_transition.index("try {")]


def test_result_cards_keep_secondary_actions_in_one_shared_menu():
    results = _strip_js_comments(_read(STATIC / "views-results.js"))
    assert "setupFloatingMenu(" in results
    assert 'id="card-menu"' in _read(INDEX)
    bar = results[results.index("function actionBar("): results.index("let cardSerial")]
    menu = results[results.index("function openCardMenu("): results.index("function actionBar(")]
    for label in ("Copy snippet", "Filter to this folder"):
        assert label in menu and label not in bar, label
    assert sorted(re.findall(r'action\("([A-Za-z ]+)"', bar)) == ["Explain", "Open", "Preview"]


def test_mutating_requests_still_carry_the_csrf_header_everywhere():
    # Every POST/DELETE helper goes through the guarded request function.
    code = _strip_js_comments(_read(STATIC / "api.js"))
    assert "fetch(" in code
    assert code.count("CSRF_HEADER") >= 2
