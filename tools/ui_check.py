"""Live end-to-end walkthrough of the OneFind web app (real server, real Chrome).

Builds a realistic notes folder and an empty library home in a scratch
directory, starts ``onefind serve`` in library mode (``--no-open``), drives
Chrome through every major flow with Playwright, asserts the visible
behaviour, and saves screenshots of each state (light + dark, wide + mobile).

Nothing outside the scratch directory is touched: the server and every CLI
call run with ``ONEFIND_HOME=<scratch>/home``.

Usage from the repository root:
    .venv/Scripts/python tools/ui_check.py [--out DIR] [--scratch DIR] [--port 8765] [--release DIR]

``--release DIR`` also captures the release screenshot set (every major state
in light and dark, at 1280 and 375 px wide) into DIR.

Exit status is 0 when every check passed, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCRATCH = Path(tempfile.gettempdir()) / "onefind-ui"
CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]
WIDTHS = [(1280, 860), (900, 900), (700, 900), (375, 780), (320, 640)]
DAY = 86400.0

# HTTP error responses the walkthrough provokes on purpose: (method, path regex, status).
EXPECTED_ALWAYS = [
    ("POST", r"^/api/documents/open$", 403),  # served with --no-open
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scratch", type=Path, default=DEFAULT_SCRATCH,
                        help="working folder for the notes, library home and logs")
    parser.add_argument("--out", type=Path, default=None,
                        help="screenshot folder (default: <scratch>/shots)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--chrome", default=os.environ.get("ONEFIND_CHROME"),
                        help="Chrome/Chromium executable (auto-detected when omitted)")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument("--slow", type=int, default=0, help="slow-motion delay in ms (debugging)")
    parser.add_argument("--release", type=Path, default=None,
                        help="also capture the numbered release screenshot set into this folder")
    args = parser.parse_args(argv)
    args.scratch = args.scratch.resolve()
    args.out = (args.out or args.scratch / "shots").resolve()
    args.release = args.release.resolve() if args.release else None
    return args


def find_chrome(explicit: str | None) -> str:
    for candidate in [explicit, *CHROME_CANDIDATES]:
        if candidate and Path(candidate).is_file():
            return candidate
    for command in ("chrome", "google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(command)
        if found:
            return found
    raise SystemExit("Chrome/Chromium not found; pass --chrome or set ONEFIND_CHROME")


# -- fixture: a realistic notes folder --------------------------------------------------

TAGGED = {
    "Personal/health/circadian-rhythm.md": ["health", "sleep"],
    "Personal/sourdough.md": ["cooking", "baking"],
    "Personal/finance/emergency-fund.md": ["money", "planning"],
    "Personal/running-shoes.md": ["health", "running"],
}
OLD_FILES = [  # modified more than a year ago (date filter checks)
    "Personal/archive/plan.md",
    "Personal/music/jazz-turnarounds.md",
    "Personal/science/cloud-formation.md",
]
RECENT_FILES = [  # modified in the last few days
    "Personal/citrus-vitamin-c.md",
    "Personal/sourdough.md",
    "Personal/health/circadian-rhythm.md",
]


def _rmtree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=False)


def build_notes(notes: Path) -> None:
    """A person's notes: personal topics, work records, a drafts folder."""
    _rmtree(notes)
    personal = notes / "Personal"
    for item in (ROOT / "sample-data").iterdir():
        if item.name == "queries.jsonl":
            continue
        target = personal / item.name
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
    work = notes / "Work"
    for folder in ("engineering", "operations", "security", "support"):
        source = ROOT / "showcase-data" / folder
        files = sorted(source.glob("*.md"))[:5]
        (work / folder.title()).mkdir(parents=True, exist_ok=True)
        for path in files:
            shutil.copy2(path, work / folder.title() / path.name)
    edge = ROOT / "showcase-data" / "edge_cases"
    (personal / "misc").mkdir(parents=True, exist_ok=True)
    for path in sorted(edge.glob("edge-00[12]*.md")):
        shutil.copy2(path, personal / "misc" / path.name)
    drafts = notes / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    (drafts / "unfinished-idea.md").write_text(
        "# Unfinished idea\n\nA half-written thought about zeppelin logistics.\n", encoding="utf-8"
    )
    for relative, tags in TAGGED.items():
        path = notes / relative
        body = path.read_text(encoding="utf-8")
        path.write_text(f"---\ntags: [{', '.join(tags)}]\n---\n{body}", encoding="utf-8")
    now = time.time()
    for path in notes.rglob("*"):
        if path.is_file():
            os.utime(path, (now - 40 * DAY, now - 40 * DAY))
    for relative in OLD_FILES:
        os.utime(notes / relative, (now - 500 * DAY, now - 500 * DAY))
    for relative in RECENT_FILES:
        os.utime(notes / relative, (now - 2 * DAY, now - 2 * DAY))


def run_cli(env: dict, *arguments: str) -> None:
    subprocess.run([sys.executable, "-m", "onefind.cli", *arguments], cwd=ROOT, env=env,
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# -- server --------------------------------------------------------------------------------


class Server:
    def __init__(self, scratch: Path, home: Path, port: int, env: dict) -> None:
        self.home = home
        self.port = port
        self.env = env
        self.log_path = scratch / "server.log"
        self.process: subprocess.Popen | None = None
        self._log = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        if _port_busy(self.port):
            raise SystemExit(f"port {self.port} is already in use; pass --port")
        self._log = open(self.log_path, "a", encoding="utf-8")  # noqa: SIM115
        self.process = subprocess.Popen(
            [sys.executable, "-m", "onefind.cli", "serve", "--home", str(self.home),
             "--port", str(self.port), "--no-open", "--log-level", "warning"],
            cwd=ROOT, env=self.env, stdout=self._log, stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 60
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise SystemExit(f"server exited early; see {self.log_path}")
            try:
                with urllib.request.urlopen(f"{self.url}/health", timeout=2) as response:
                    if response.status == 200:
                        return
            except (urllib.error.URLError, OSError):
                time.sleep(0.3)
        raise SystemExit("server did not start within 60 s")

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        self.process = None
        if self._log is not None:
            self._log.close()
            self._log = None
        deadline = time.time() + 10
        while _port_busy(self.port) and time.time() < deadline:
            time.sleep(0.2)

    def api(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"{self.url}{path}", data=data, method=method,
            headers={"Content-Type": "application/json", "X-OneFind-Request": "1"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read() or b"null")


def _port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


# -- the checker ---------------------------------------------------------------------------


class Checker:
    """Soft assertions, browser diagnostics, and screenshots."""

    def __init__(self, page: Page, out: Path, server: Server) -> None:
        self.page = page
        self.out = out
        self.server = server
        self.failures: list[str] = []
        self.passed = 0
        self.step_name = "setup"
        self.shots: list[Path] = []
        self.console_errors: list[str] = []
        self.page_errors: list[str] = []
        self.bad_responses: list[tuple[str, str, int, str]] = []
        self.failed_requests: list[str] = []
        self.expected: list[tuple[str, str, int]] = list(EXPECTED_ALWAYS)
        self.offline = False
        self.requests: list[tuple[str, str]] = []
        page.on("console", self._on_console)
        page.on("pageerror", lambda error: self.page_errors.append(f"[{self.step_name}] {error}"))
        page.on("response", self._on_response)
        page.on("requestfailed", self._on_request_failed)
        page.on("request", lambda request: self.requests.append((request.method, _path(request.url))))

    # diagnostics
    def _on_console(self, message) -> None:
        if message.type != "error":
            return
        text = message.text
        if text.startswith("Failed to load resource"):
            return  # covered by the response check, which knows what was expected
        self.console_errors.append(f"[{self.step_name}] {text}")

    def _on_response(self, response) -> None:
        if response.status < 400:
            return
        method = response.request.method
        path = _path(response.url)
        for exp_method, pattern, status in self.expected:
            if exp_method == method and re.search(pattern, path) and status == response.status:
                return
        self.bad_responses.append((self.step_name, method, response.status, path))

    def _on_request_failed(self, request) -> None:
        failure = request.failure or ""
        if "ERR_ABORTED" in failure or (self.offline and "ERR_CONNECTION_REFUSED" in failure):
            return
        self.failed_requests.append(f"[{self.step_name}] {request.method} {_path(request.url)}: {failure}")

    @contextmanager
    def expecting(self, method: str, pattern: str, status: int):
        entry = (method, pattern, status)
        self.expected.append(entry)
        try:
            yield
        finally:
            self.expected.remove(entry)

    # assertions
    def check(self, condition: bool, message: str) -> bool:
        if condition:
            self.passed += 1
        else:
            self.failures.append(f"[{self.step_name}] {message}")
            print(f"   FAIL {message}", flush=True)
        return bool(condition)

    def step(self, name: str, func: Callable[[], None]) -> None:
        self.step_name = name
        print(f"-- {name}", flush=True)
        started = time.perf_counter()
        try:
            func()
        except Exception as exc:  # noqa: BLE001 - report and keep walking
            detail = " / ".join(line.strip() for line in str(exc).splitlines()[:4] if line.strip())
            self.failures.append(f"[{name}] crashed: {type(exc).__name__}: {detail[:400]}")
            print(f"   CRASH {type(exc).__name__}: {exc}", flush=True)
            traceback.print_exc(limit=3)
            self.shot(f"zz-fail-{_slug(name)}")
            self._recover()
        print(f"   ({time.perf_counter() - started:.1f}s)", flush=True)

    def _recover(self) -> None:
        page = self.page
        try:
            for _ in range(3):
                page.keyboard.press("Escape")
            page.evaluate("for (const d of document.querySelectorAll('dialog[open]')) d.close()")
        except PlaywrightError:
            pass

    def shot(self, name: str, *, full_page: bool = False) -> Path:
        self.out.mkdir(parents=True, exist_ok=True)
        path = self.out / f"{name}.png"
        try:
            self.page.wait_for_timeout(120)
            self.page.screenshot(path=str(path), full_page=full_page, animations="disabled")
            self.shots.append(path)
        except PlaywrightError as exc:
            self.failures.append(f"[{self.step_name}] screenshot {name} failed: {exc}")
        return path

    def summary(self) -> bool:
        problems = (
            [f"page error {item}" for item in self.page_errors]
            + [f"console error {item}" for item in self.console_errors]
            + [f"unexpected HTTP {status} {method} {path} [{step}]" for step, method, status, path in self.bad_responses]
            + [f"request failed {item}" for item in self.failed_requests]
        )
        self.failures.extend(problems)
        print()
        print(f"{self.passed} checks passed, {len(self.failures)} problems")
        for failure in self.failures:
            print(f"  - {failure}")
        print(f"{len(self.shots)} screenshots in {self.out}")
        return not self.failures


def _path(url: str) -> str:
    match = re.match(r"https?://[^/]+(/[^?#]*)", url)
    return match.group(1) if match else url


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


# -- page helpers ------------------------------------------------------------------------


def wait_js(page: Page, expression: str, timeout: float = 15000) -> None:
    """Poll a JS expression until truthy.

    ``page.wait_for_function`` evaluates its predicate with ``eval`` inside
    the page, which the app's CSP (no 'unsafe-eval') rightly blocks;
    ``page.evaluate`` goes through the DevTools protocol instead.
    """
    deadline = time.time() + timeout / 1000
    while True:
        if page.evaluate(expression):
            return
        if time.time() > deadline:
            raise TimeoutError(f"timed out after {timeout:.0f} ms waiting for: {expression[:160]}")
        page.wait_for_timeout(80)


def visible(page: Page, selector: str) -> bool:
    return page.locator(selector).first.is_visible()


def wait_view(page: Page, name: str, timeout: float = 20000) -> None:
    page.wait_for_selector(f"#view-{name}:not([hidden])", state="visible", timeout=timeout)


def current_view(page: Page) -> str:
    return page.get_attribute("#results-pane", "data-view") or ""


def text(page: Page, selector: str) -> str:
    locator = page.locator(selector).first
    return (locator.text_content(timeout=5000) or "").strip()


def toast_text(page: Page) -> str:
    return " | ".join(t.strip() for t in page.locator("#toasts .toast-text").all_text_contents())


def wait_toast(page: Page, pattern: str, timeout: float = 8000) -> bool:
    deadline = time.time() + timeout / 1000
    regex = re.compile(pattern, re.I)
    while time.time() < deadline:
        if regex.search(toast_text(page)):
            return True
        page.wait_for_timeout(100)
    return False


def clear_toasts(page: Page) -> None:
    page.evaluate("document.getElementById('toasts').replaceChildren()")


def search(page: Page, query: str, *, enter: bool = True) -> str:
    box = page.locator("#search-input")
    box.fill("")
    box.fill(query)
    if enter:
        box.press("Enter")
    wait_js(page, 
        "() => ['results','empty','error'].includes(document.getElementById('results-pane').dataset.view)",
        timeout=60000,
    )
    return current_view(page)


def result_titles(page: Page, selector: str = "#results-list .result-title") -> list[str]:
    return [t.strip() for t in page.locator(selector).all_text_contents()]


def open_manage(page: Page, section: str | None = None) -> None:
    if not page.locator("#manage-dialog[open]").count():
        clear_toasts(page)
        page.click("#manage-button")
        page.wait_for_selector("#manage-dialog[open]")
    if section:
        page.click(f".sheet-nav [data-section='manage-{section}']")


def close_manage(page: Page) -> None:
    if page.locator("#manage-dialog[open]").count():
        page.click("#manage-dialog .dialog-head [data-close]")
        page.wait_for_selector("#manage-dialog", state="hidden")


def set_theme(page: Page, pref: str) -> None:
    page.evaluate(
        """(pref) => { try { if (pref === 'system') localStorage.removeItem('onefind.theme');
           else localStorage.setItem('onefind.theme', pref); } catch {} }""",
        pref,
    )
    for _ in range(3):
        if page.get_attribute("html", "data-theme-pref") == pref:
            break
        page.click("#theme-toggle")
        page.wait_for_timeout(50)


def layout_problems(page: Page) -> list[str]:
    """Horizontal overflow and overlapping siblings in the main layout rows."""
    return page.evaluate(
        """() => {
        const problems = [];
        const vw = document.documentElement.clientWidth;
        if (document.documentElement.scrollWidth > vw + 1) {
          const offenders = [];
          for (const node of document.querySelectorAll('body *')) {
            if (!node.getClientRects().length) continue;
            const r = node.getBoundingClientRect();
            if (r.right > vw + 1 && r.width > 0 && getComputedStyle(node).position !== 'fixed') {
              offenders.push((node.id ? '#' + node.id : node.tagName.toLowerCase() + '.' + [...node.classList].join('.')) + ' right=' + Math.round(r.right));
            }
            if (offenders.length > 6) break;
          }
          problems.push('horizontal scroll: scrollWidth ' + document.documentElement.scrollWidth + ' > ' + vw + ' ' + offenders.join(', '));
        }
        const rows = ['.header-inner', '.header-actions', '.search-controls', '.control-buttons',
                      '.results-head', '.results-tools', '.result-head', '.welcome-actions',
                      '.filter-controls', '.preview-head', '.dialog-foot', '.dialog-head'];
        for (const sel of rows) {
          for (const row of document.querySelectorAll(sel)) {
            if (!row.getClientRects().length) continue;
            const kids = [...row.children].filter((k) => k.getClientRects().length && getComputedStyle(k).visibility !== 'hidden');
            for (let i = 0; i < kids.length; i++) for (let j = i + 1; j < kids.length; j++) {
              const a = kids[i].getBoundingClientRect(), b = kids[j].getBoundingClientRect();
              const ox = Math.min(a.right, b.right) - Math.max(a.left, b.left);
              const oy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
              if (ox > 2 && oy > 2) problems.push('overlap in ' + sel + ': ' + (kids[i].id || kids[i].className) + ' / ' + (kids[j].id || kids[j].className));
            }
            const rr = row.getBoundingClientRect();
            for (const k of kids) {
              const r = k.getBoundingClientRect();
              if (r.right > Math.max(rr.right, vw) + 2) problems.push('clipped in ' + sel + ': ' + (k.id || k.className));
            }
          }
        }
        return [...new Set(problems)];
        }"""
    )


# -- the walkthrough -----------------------------------------------------------------------


class Walkthrough:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.scratch: Path = args.scratch
        self.home = self.scratch / "home"
        self.notes = self.scratch / "notes"
        self.elsewhere = self.scratch / "elsewhere"
        self.archive_db = self.elsewhere / "archive.db"
        self.bulk = self.elsewhere / "bulk"
        self.extra = self.elsewhere / "travel"
        # Auto-sync polls every 0.5 s here (5 s by default) so its step stays quick.
        self.env = {**os.environ, "ONEFIND_HOME": str(self.home), "PYTHONIOENCODING": "utf-8",
                    "ONEFIND_AUTOSYNC_INTERVAL": "0.5"}
        self.server = Server(self.scratch, self.home, args.port, self.env)
        self.c: Checker | None = None
        self.page: Page | None = None

    # setup
    def prepare(self) -> None:
        self.scratch.mkdir(parents=True, exist_ok=True)
        for path in (self.home, self.elsewhere):
            _rmtree(path)
        build_notes(self.notes)
        self.elsewhere.mkdir(parents=True)
        archive_src = self.elsewhere / "old-notes"
        shutil.copytree(ROOT / "showcase-data" / "governance", archive_src)
        run_cli(self.env, "index", str(archive_src), "--db", str(self.archive_db), "-q")
        for folder in ROOT.joinpath("showcase-data").iterdir():
            if folder.is_dir():
                shutil.copytree(folder, self.bulk / folder.name)
        self.extra.mkdir(parents=True)
        (self.extra / "lisbon-trip.md").write_text(
            "# Lisbon trip\n\nTram 28 climbs to the castle; buy the day pass at the kiosk.\n", encoding="utf-8")
        (self.extra / "packing-list.txt").write_text(
            "Packing list: passport, adapter plug, rain jacket, walking shoes.\n", encoding="utf-8")
        _rmtree(self.args.out)
        self.args.out.mkdir(parents=True)
        (self.scratch / "server.log").unlink(missing_ok=True)

    def run(self) -> bool:
        self.prepare()
        self.server.start()
        chrome = find_chrome(self.args.chrome)
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    executable_path=chrome, headless=not self.args.headed, slow_mo=self.args.slow
                )
                context = browser.new_context(
                    viewport={"width": 1280, "height": 860},
                    color_scheme="light",
                    accept_downloads=True,
                    reduced_motion="no-preference",
                )
                context.grant_permissions(["clipboard-read", "clipboard-write"], origin=self.server.url)
                page = context.new_page()
                page.set_default_timeout(15000)
                self.page = page
                self.c = Checker(page, self.args.out, self.server)
                self.walk()
                ok = self.c.summary()
                context.close()
                browser.close()
                return ok
        finally:
            self.server.stop()

    def walk(self) -> None:
        c = self.c
        assert c is not None
        steps = [
            ("first run onboarding", self.first_run),
            ("index wizard: folder browser", self.wizard_choose),
            ("index wizard: progress and summary", self.wizard_progress),
            ("idle home after indexing", self.idle_home),
            ("live search with debounce", self.live_search),
            ("a link with ?q= searches without recording it", self.deep_link),
            ("a shorter query cancels the running search", self.short_query_abort),
            ("status refreshes during a job; failed job next step", self.job_stats_and_failure),
            ("enter search with streamed stages", self.enter_search),
            ("explain, preview, find in document", self.explain_and_preview),
            ("result actions: open, copy, bookmark", self.result_actions),
            ("keyboard shortcuts", self.keyboard),
            ("filters: folder, type, tag, date", self.filters),
            ("weak matches and no results", self.weak_and_empty),
            ("modes and precision", self.modes),
            ("saved searches and history", self.saved_and_history),
            ("bookmarks and recent documents", self.bookmarks_and_recent),
            ("export", self.export),
            ("manage: health and stale changes", self.manage_health),
            ("manage: check, repair, compact", self.manage_maintenance),
            ("manage: backup and restore", self.manage_backups),
            ("collections: create, rename, open, switch, remove", self.collections),
            ("collections: add vectors to keyword-only collection", self.add_vectors),
            ("manage: clear with typed confirmation", self.clear_collection),
            ("indexing: add a folder, full rebuild, cancel", self.indexing_more),
            ("wizard keeps a folder's saved exclusions", self.wizard_keeps_excludes),
            ("index a single file and a JSONL dataset", self.single_files),
            ("long notes: split into passages, rebuild vectors, passage preview", self.long_notes),
            ("folder queue, stale home lists, missing folders", self.folder_queue),
            ("remove a document from the index", self.remove_document),
            ("auto-sync: enable in Manage, new note becomes searchable", self.auto_sync),
            ("theme and reduced motion", self.theme_and_motion),
            ("responsive layouts", self.responsive),
            ("error state when the server stops", self.server_down),
            ("final screenshots", self.final_screenshots),
        ]
        if self.args.release:
            steps.append(("release screenshot set", self.release_shots))
        for name, func in steps:
            c.step(name, func)

    # -- steps -----------------------------------------------------------------------

    def first_run(self) -> None:
        page, c = self.page, self.c
        page.goto(self.server.url + "/")
        wait_view(page, "onboarding")
        wait_js(page, "document.getElementById('status-label').textContent !== 'Connecting'")
        c.check(text(page, "#status-label") == "No index", f"status pill says {text(page, '#status-label')!r}")
        c.check(text(page, "#collection-name") == "No collection", "header shows no collection")
        c.check("Index a folder" in text(page, "#onboard-index"), "primary action is 'Index a folder'")
        c.check(visible(page, "#onboard-open"), "'Open an existing collection' is offered")
        c.check(page.get_attribute("#search-input", "placeholder") == "Index a folder to start searching your notes.",
                "placeholder explains indexing")
        c.check(not layout_problems(page), f"layout: {layout_problems(page)}")
        c.shot("01-onboarding-light")
        # Typing before an index exists must not search.
        page.fill("#search-input", "hello")
        page.press("#search-input", "Enter")
        c.check(wait_toast(page, "Index a folder"), "searching without an index explains what to do")
        page.fill("#search-input", "")
        clear_toasts(page)

    def wizard_choose(self) -> None:
        page, c = self.page, self.c
        page.click("#onboard-index")
        page.wait_for_selector("#index-dialog[open]")
        c.check(visible(page, "#index-name-row"), "first index asks for a collection name")
        page.wait_for_selector("#fs-list li")
        c.check(page.locator("#fs-places li").count() >= 1, "places are listed")
        page.fill("#fs-path", str(self.notes))
        page.press("#fs-path", "Enter")
        page.wait_for_selector("#fs-list .browser-item:has-text('Personal')")
        names = [t.strip() for t in page.locator("#fs-list .browser-item span").all_text_contents()]
        c.check(names[:3] == ["drafts", "Personal", "Work"], f"folders listed first, sorted: {names}")
        c.check("Index “notes”" in text(page, "#index-start"), f"start button names the folder: {text(page, '#index-start')!r}")
        page.click("#fs-list .browser-item:has-text('Personal')")
        page.wait_for_selector("#fs-crumbs .crumb-current:has-text('Personal')")
        c.check(page.locator("#fs-list .browser-file-button").count() > 0, "supported files are shown in a folder")
        c.check("supported file" in text(page, "#fs-count"), f"file count line: {text(page, '#fs-count')!r}")
        page.click("#fs-up")
        page.wait_for_selector("#fs-crumbs .crumb-current:has-text('notes')")
        # A path that does not exist shows a plain error.
        page.fill("#fs-path", str(self.notes / "missing-folder"))
        page.press("#fs-path", "Enter")
        with c.expecting("GET", r"^/api/fs/list$", 404):
            page.wait_for_selector("#fs-error:not([hidden])")
        c.check("does not exist" in text(page, "#fs-error"), f"missing folder message: {text(page, '#fs-error')!r}")
        page.fill("#fs-path", str(self.notes))
        page.press("#fs-path", "Enter")
        page.wait_for_selector("#fs-crumbs .crumb-current:has-text('notes')")
        c.check(not visible(page, "#fs-error"), "error clears after a valid path")
        page.click(".disclosure summary:has-text('Options')")
        page.fill("#index-exclude", "drafts")
        page.fill("#index-name", "My Notes")
        c.check(page.is_checked("#index-embed"), "vectors are on by default")
        c.check(page.is_checked("#index-autosync"), "auto-sync is on by default for a new collection")
        # The walkthrough edits this folder behind the app's back to check stale
        # detection; auto-sync gets its own step later (enabled from Manage).
        page.uncheck("#index-autosync")
        c.check(not layout_problems(page), f"layout: {layout_problems(page)}")
        c.shot("02-wizard-choose-light")

    def wizard_progress(self) -> None:
        page, c = self.page, self.c
        page.click("#index-start")
        page.wait_for_selector("#index-dialog[data-step='progress']", timeout=20000)
        page.wait_for_timeout(250)
        c.shot("03-wizard-progress-light")
        seen_steps: set[tuple[str, str]] = set()
        deadline = time.time() + 240
        while time.time() < deadline:
            step = page.get_attribute("#index-dialog", "data-step")
            for item in page.locator("#job-steps .stepper-step").all():
                seen_steps.add(((item.text_content() or "").strip()[:20], item.get_attribute("data-status") or ""))
            if step in ("done", "failed"):
                break
            page.wait_for_timeout(150)
        step = page.get_attribute("#index-dialog", "data-step")
        c.check(step == "done", f"indexing finished in the wizard (step={step}, {text(page, '#failed-message') if step == 'failed' else ''})")
        polls = [p for m, p in c.requests if m == "GET" and p.startswith("/api/jobs/")]
        c.check(len(polls) >= 2, f"the job was polled ({len(polls)} polls)")
        c.check(any(status == "active" for _, status in seen_steps), f"stepper showed an active step: {sorted(seen_steps)}")
        summary = {dt.strip(): dd.strip() for dt, dd in zip(
            page.locator("#done-summary dt").all_text_contents(),
            page.locator("#done-summary dd").all_text_contents())}
        c.check(int(summary.get("Added", "0").replace(",", "")) >= 30, f"summary counts added notes: {summary}")
        c.check("keyword and meaning" in text(page, "#done-copy"), f"done copy mentions meaning search: {text(page, '#done-copy')!r}")
        c.check("Ready to search" in text(page, "#index-title"), "wizard title says ready")
        c.check(not layout_problems(page), f"layout: {layout_problems(page)}")
        c.shot("04-wizard-done-light")
        page.click("#index-foot button:has-text('Start searching')")
        page.wait_for_selector("#index-dialog", state="hidden")
        c.check(page.evaluate("document.activeElement.id") == "search-input", "focus moves to the search box")

    def idle_home(self) -> None:
        page, c = self.page, self.c
        wait_view(page, "idle")
        wait_js(page, "document.getElementById('status-label').textContent === 'Ready'", timeout=20000)
        c.check(text(page, "#collection-name") == "My Notes", f"collection name: {text(page, '#collection-name')!r}")
        c.check(visible(page, "#suggestions"), "'Try' suggestions appear after the first index")
        detail = text(page, "#idle-status .status-sentence")
        c.check("Keyword and meaning" in detail, f"status sentence: {detail!r}")
        c.check(page.locator("#docs-list li:not(.list-empty)").count() > 0, "recent documents are listed")
        c.check(not page.locator("#mode-group input[value='semantic']").is_disabled(), "meaning mode is enabled")
        c.check(page.get_attribute("#search-input", "placeholder") == "Search your notes by keyword or meaning.",
                "ready placeholder")
        c.check(not visible(page, "#idle-changes"), "no stale-change line right after indexing")
        c.check(not layout_problems(page), f"layout: {layout_problems(page)}")
        c.shot("05-idle-light")

    def live_search(self) -> None:
        page, c = self.page, self.c
        box = page.locator("#search-input")
        box.click()
        box.press_sequentially("v", delay=40)
        page.wait_for_timeout(100)
        c.check(current_view(page) == "idle", f"one character keeps the home screen (view={current_view(page)})")
        c.check(page.evaluate("document.getElementById('search-field').classList.contains('is-typing')"),
                "typing shows the readiness cue")
        before = len([r for r in c.requests if r[1].startswith("/api/search")])
        box.press_sequentially("itamin c", delay=60)
        wait_js(page, "document.getElementById('results-pane').dataset.view === 'results'", timeout=30000)
        after = len([r for r in c.requests if r[1].startswith("/api/search")])
        c.check(1 <= after - before <= 3, f"live search is debounced ({after - before} requests for 8 keystrokes)")
        titles = result_titles(page)
        c.check(bool(titles) and "Citrus" in titles[0], f"top live result: {titles[:3]}")
        c.check("q=vitamin+c" in page.url, f"query is kept in the URL: {page.url}")
        # The live search is recorded once the user pauses.
        page.wait_for_timeout(1900)
        c.check(any(m == "POST" and p == "/api/history" for m, p in c.requests), "paused live search is recorded in history")

    def enter_search(self) -> None:
        page, c = self.page, self.c
        start = len(c.requests)
        view = search(page, "how do plants turn sunlight into energy")
        c.check(view == "results", f"natural question gives results (view={view})")
        streamed = [p for m, p in c.requests[start:] if p == "/api/search/stream"]
        plain = [p for m, p in c.requests[start:] if p == "/api/search"]
        c.check(len(streamed) == 1 and not plain, f"Enter uses the streamed endpoint only ({len(streamed)} stream, {len(plain)} plain)")
        titles = result_titles(page)
        c.check(any("hotosynthesis" in t for t in titles[:3]), f"photosynthesis note in top 3: {titles[:5]}")
        trail = page.locator("#stage-trail .trail-step").all_text_contents()
        c.check(len(trail) >= 4, f"stage timings list the hybrid pipeline: {trail}")
        c.check(not visible(page, "#stage-trail"), "per-step timings stay folded until asked for")
        page.click("#stage-total")
        c.check(visible(page, "#stage-trail"), "the timing summary opens the per-step list")
        page.click("#stage-total")
        summary = text(page, "#results-summary")
        c.check("Hybrid" in summary and "result" in summary, f"summary: {summary!r}")
        c.check(" ms" not in summary, f"timings are not repeated in the summary: {summary!r}")
        first_card = page.locator("#results-list .result-card").first
        c.check(first_card.locator(".result-actions button").count() == 4,
                "a card shows four actions (Open, Preview, Explain, More)")
        tab_stops = page.evaluate("""() => [...document.querySelectorAll('#view-results .result-card, #view-results .result-actions button')]
            .filter((n) => n.tabIndex >= 0).length""")
        c.check(tab_stops <= 5, f"the result list is one Tab stop plus the current card's actions ({tab_stops})")
        header_bottom = page.evaluate("document.getElementById('results-list').getBoundingClientRect().top")
        c.check(header_bottom < 420, f"the first result starts high on the page (y={header_bottom:.0f})")
        badges = page.locator("#results-list .confidence").all_text_contents()
        c.check(bool(badges), "confidence badges shown")
        c.check(not layout_problems(page), f"layout: {layout_problems(page)}")
        c.shot("06-results-light")
        # A slow refinement keeps the old results (dimmed), not a blank pipeline.
        page.route("**/api/search/stream", lambda route: (time.sleep(0.9), route.continue_()))
        try:
            # Record what the results pane goes through while the refresh runs.
            page.evaluate("""() => {
                const pane = document.getElementById('results-pane');
                window.__paneLog = { views: new Set([pane.dataset.view]), refreshing: false };
                window.__paneObserver = new MutationObserver(() => {
                  window.__paneLog.views.add(pane.dataset.view);
                  if (pane.classList.contains('is-refreshing')) window.__paneLog.refreshing = true;
                });
                window.__paneObserver.observe(pane, { attributes: true, attributeFilter: ['class', 'data-view'] });
            }""")
            page.fill("#search-input", "how do plants turn sunlight into food")
            page.press("#search-input", "Enter")
            wait_js(page, "window.__paneLog.refreshing && !document.getElementById('results-pane').classList.contains('is-refreshing')",
                    timeout=30000)
            log = page.evaluate("""() => { window.__paneObserver.disconnect();
                return { views: [...window.__paneLog.views], refreshing: window.__paneLog.refreshing }; }""")
            c.check(log["views"] == ["results"], f"a slow refresh keeps the results on screen (views seen: {log['views']})")
            c.check(log["refreshing"], "the refreshing results are marked as updating")
            # The loading pipeline with real stages, for a new search from the home screen.
            page.locator("#search-input").focus()
            page.keyboard.press("Escape")
            wait_view(page, "idle")
            page.fill("#search-input", "sourdough starter feeding schedule")
            page.press("#search-input", "Enter")
            page.wait_for_selector("#view-loading:not([hidden])", timeout=3000)
            c.check(page.locator("#pipeline .pipeline-step").count() >= 3, "loading shows the predicted pipeline")
            opacity = page.evaluate("getComputedStyle(document.getElementById('view-loading')).opacity")
            c.check(float(opacity) > 0.9, f"the loading view is never dimmed (opacity {opacity})")
            c.shot("07-loading-light")
        finally:
            wait_js(page, "document.getElementById('results-pane').dataset.view === 'results'", timeout=30000)
            page.unroute("**/api/search/stream")

    def explain_and_preview(self) -> None:
        page, c = self.page, self.c
        search(page, "vitamin c immune system")
        card = page.locator("#results-list .result-card").first
        card.focus()
        page.keyboard.press("e")
        panel = card.locator(".why-panel")
        c.check(panel.is_visible(), "'e' opens 'Why this result'")
        reasons = panel.locator(".why-reasons li").all_text_contents()
        c.check(bool(reasons), f"explanation has reasons: {reasons}")
        c.check(panel.locator(".term-matched").count() > 0, "matched words are listed")
        c.check("Mode: Hybrid" in (panel.locator(".why-facts").text_content() or ""), "explanation names the mode")
        c.shot("08-explain-light")
        page.keyboard.press("e")
        c.check(not panel.is_visible(), "'e' again closes the explanation")
        left_before = page.evaluate("document.getElementById('results-list').getBoundingClientRect().left")
        page.keyboard.press("Enter")
        page.wait_for_selector("#preview:not([hidden])")
        wait_js(page, "document.getElementById('preview-title').textContent !== 'Loading…'")
        c.check("Citrus" in text(page, "#preview-title"), f"preview title: {text(page, '#preview-title')!r}")
        c.check(page.locator("#preview-body mark").count() > 0, "query words are highlighted in the preview")
        c.check(page.locator("#preview-meta .meta-row").count() >= 2, "preview shows metadata")
        page.wait_for_timeout(400)
        c.check(page.evaluate("document.querySelector('#results-list .result-card').classList.contains('is-previewed')"),
                "the previewed card is highlighted")
        left_after = page.evaluate("document.getElementById('results-list').getBoundingClientRect().left")
        c.check(left_after <= left_before, f"results make room for the side preview ({left_before:.0f} -> {left_after:.0f})")
        page.fill("#preview-find", "orange")
        wait_js(page, "document.getElementById('preview-find-count').textContent.includes('found')")
        page.press("#preview-find", "Enter")
        c.check("1 of" in text(page, "#preview-find-count"), f"find steps through matches: {text(page, '#preview-find-count')!r}")
        page.fill("#preview-find", "zebra crossing")
        wait_js(page, "document.getElementById('preview-find-count').textContent === 'No matches'")
        c.check(not layout_problems(page), f"layout: {layout_problems(page)}")
        c.shot("09-preview-light")
        page.keyboard.press("Escape")
        page.wait_for_selector("#preview", state="hidden")
        c.check(page.evaluate("document.activeElement?.classList.contains('result-card')"), "Esc returns focus to the card")
        c.check(current_view(page) == "results", "Esc closes only the preview first")

    def result_actions(self) -> None:
        page, c = self.page, self.c
        clear_toasts(page)
        card = page.locator("#results-list .result-card").first
        card.focus()
        page.keyboard.press("o")
        c.check(wait_toast(page, "turned off"), f"open with --no-open explains it is disabled: {toast_text(page)!r}")
        clear_toasts(page)
        page.keyboard.press("c")
        c.check(wait_toast(page, "Snippet copied"), f"copy toast: {toast_text(page)!r}")
        clipboard = page.evaluate("navigator.clipboard.readText()")
        c.check("vitamin" in clipboard.lower(), f"snippet is on the clipboard: {clipboard[:80]!r}")
        clear_toasts(page)
        page.keyboard.press("b")
        c.check(wait_toast(page, "^Bookmarked"), f"bookmark toast: {toast_text(page)!r}")
        c.check(card.locator(".meta-bookmark").is_visible(), "the card shows it is bookmarked")
        clear_toasts(page)
        # Secondary actions live in the card's More menu.
        card.locator(".more-button").click()
        page.wait_for_selector("#card-menu:not([hidden])")
        items = [t.strip() for t in page.locator("#card-menu .menu-text").all_text_contents()]
        c.check("Copy snippet" in items and "Remove bookmark" in items, f"More menu items: {items}")
        c.check(page.evaluate("document.activeElement.closest('#card-menu') !== null"), "the menu takes focus")
        c.shot("08b-card-menu-light")
        page.keyboard.press("Escape")
        page.wait_for_selector("#card-menu", state="hidden")
        c.check(page.evaluate("document.activeElement.classList.contains('more-button')"), "Esc returns focus to More")
        # The open button in the preview uses the same path.
        card.locator("button:has-text('Preview')").click()
        page.wait_for_selector("#preview-actions button:has-text('Open')")
        page.click("#preview-actions button:has-text('Show in folder')")
        c.check(wait_toast(page, "turned off"), "reveal in folder is disabled too")
        c.check(page.locator("#preview-actions .bookmark-button").get_attribute("aria-pressed") == "true",
                "preview knows the note is bookmarked")
        page.click("#preview-close")
        page.wait_for_selector("#preview", state="hidden")
        clear_toasts(page)

    def keyboard(self) -> None:
        page, c = self.page, self.c
        search(page, "policy owner approval")
        page.locator("body").click(position={"x": 5, "y": 300})
        page.keyboard.press("/")
        c.check(page.evaluate("document.activeElement.id") == "search-input", "'/' focuses the search box")
        page.keyboard.press("ArrowDown")
        first = page.evaluate("document.activeElement.closest('.result-card')?.dataset.docId || null")
        c.check(first is not None, "ArrowDown from the search box enters the results")
        page.keyboard.press("j")
        second = page.evaluate("document.activeElement.closest('.result-card')?.dataset.docId || null")
        c.check(second is not None and second != first, "'j' moves to the next result")
        page.keyboard.press("ArrowDown")
        page.keyboard.press("k")
        third = page.evaluate("document.activeElement.closest('.result-card')?.dataset.docId || null")
        c.check(third == second, "'k' moves back")
        page.keyboard.press("ArrowUp")
        c.check(page.evaluate("document.activeElement.closest('.result-card')?.dataset.docId") == first, "ArrowUp moves back")
        page.keyboard.press("Control+k")
        c.check(page.evaluate("document.activeElement.id") == "search-input", "Ctrl+K focuses the search box")
        page.locator("#results-list .result-card").first.focus()
        page.keyboard.press("?")
        page.wait_for_selector("#shortcuts-dialog[open]")
        c.shot("10-shortcuts-light")
        page.keyboard.press("Escape")
        page.wait_for_selector("#shortcuts-dialog", state="hidden")
        c.check(page.evaluate("document.activeElement.classList.contains('result-card')"), "focus returns after the shortcuts dialog")
        page.keyboard.press("ArrowUp")
        c.check(page.evaluate("document.activeElement.id") == "search-input", "ArrowUp on the first result returns to the search box")
        page.keyboard.press("ArrowDown")
        # Mode radios: arrows move between modes without jumping to the search box.
        page.locator("#mode-group input[value='hybrid']").focus()
        page.keyboard.press("ArrowLeft")
        c.check(page.evaluate("document.activeElement.name") == "mode", "arrowing through the modes keeps focus in the group")
        wait_js(page, "document.getElementById('results-pane').dataset.view === 'results' && !document.getElementById('results-pane').classList.contains('is-refreshing')", timeout=30000)
        page.locator("#mode-group input[value='hybrid']").check(force=True)
        wait_js(page, "!document.getElementById('results-pane').classList.contains('is-refreshing')", timeout=30000)
        page.locator("#results-list .result-card").first.focus()
        page.keyboard.press("Escape")
        c.check(current_view(page) == "idle" and page.input_value("#search-input") == "", "Esc clears the search")
        c.check(page.evaluate("document.activeElement.id") == "search-input", "focus is back in the search box")
        c.check("q=" not in page.url, "the URL query is cleared")

    def filters(self) -> None:
        page, c = self.page, self.c
        # Filter pickers open from the Filters button; nothing shows until then.
        c.check(visible(page, "#filters-toggle") and not visible(page, "#filter-bar"), "filters start folded behind the Filters button")
        search(page, "policy")
        page.click("#filters-toggle")
        c.check(visible(page, "#filter-folder"), "the Filters button opens the pickers")
        # Arrow keys browse a picker without applying every option on the way.
        before_requests = len([r for r in c.requests if r[1].startswith("/api/search")])
        page.locator("#filter-folder").focus()
        page.keyboard.press("ArrowDown")
        page.keyboard.press("ArrowDown")
        page.wait_for_timeout(400)
        after_requests = len([r for r in c.requests if r[1].startswith("/api/search")])
        c.check(after_requests == before_requests and not visible(page, "#filter-chips .chip-item"),
                "arrowing through the folder picker does not apply filters")
        page.keyboard.press("Enter")
        page.wait_for_selector("#filter-chips .chip-item")
        page.click("#filter-chips .chip-filter")
        page.wait_for_timeout(400)
        c.check(page.evaluate("document.activeElement.id") == "filters-toggle" or page.evaluate("document.activeElement.classList.contains('chip-filter')"),
                "removing a chip keeps focus nearby")
        folders = page.locator("#filter-folder option").all_text_contents()
        c.check(any("Work" in item for item in folders), f"folder facets: {folders[:6]}")
        work = next((page.locator("#filter-folder option").nth(i).get_attribute("value")
                     for i, label in enumerate(folders) if label.startswith("Work") and "›" not in label), None)
        if work is None:
            work = next((page.locator("#filter-folder option").nth(i).get_attribute("value")
                         for i, label in enumerate(folders) if "Work" in label), "")
        page.select_option("#filter-folder", work)
        page.wait_for_selector("#filter-chips .chip-item")
        wait_js(page, "document.getElementById('results-pane').dataset.view !== 'loading'")
        page.wait_for_timeout(500)
        paths = page.locator("#view-results .result-path").all_text_contents()
        c.check(bool(paths) and all("Work" in p for p in paths), f"folder filter keeps only Work notes: {paths[:4]}")
        c.check(text(page, "#filters-count") == "1", "filter count badge")
        c.check("1 filter" in text(page, "#results-summary"), f"summary mentions the filter: {text(page, '#results-summary')!r}")
        c.shot("11-filters-light")
        page.click("#filter-chips .chip-filter")
        page.wait_for_timeout(400)
        types = page.locator("#filter-type option").all_text_contents()
        c.check(any("TXT" in t for t in types) and any("MD" in t for t in types), f"type facets: {types}")
        page.select_option("#filter-type", ".txt")
        search(page, "glaze")
        page.wait_for_timeout(300)
        titles = result_titles(page) + result_titles(page, "#weak-list .result-title")
        c.check(len(titles) >= 1 and all("laze" in t or "otter" in t for t in titles), f"type filter results: {titles}")
        page.click("#filters-clear")
        page.wait_for_timeout(300)
        tags = page.locator("#filter-tag option").all_text_contents()
        c.check(any("#health" in t for t in tags), f"tag facets from front matter: {tags}")
        page.select_option("#filter-tag", "health")
        search(page, "routine")
        page.wait_for_timeout(300)
        cards = page.locator("#view-results .result-card")
        tagged_titles = result_titles(page) + result_titles(page, "#weak-list .result-title")
        c.check(cards.count() >= 1, f"tag filter finds tagged notes ({tagged_titles})")
        c.check(page.locator("#view-results .result-card .tag:has-text('#health')").count() == cards.count(),
                "every tag-filtered result carries the tag")
        page.click("#filters-clear")
        page.select_option("#filter-date", "30d")
        view = search(page, "vitamin")
        page.wait_for_timeout(300)
        c.check(view == "results" and "Citrus" in " ".join(result_titles(page)), "recent note passes the 30-day filter")
        view = search(page, "jazz turnarounds")
        page.wait_for_timeout(300)
        all_titles = result_titles(page) + result_titles(page, "#weak-list .result-title")
        c.check(not any("azz" in t for t in all_titles), f"old note is filtered out by date: {all_titles}")
        if view == "empty":
            c.check("filters may also be hiding" in text(page, "#empty-copy"), "no-results copy mentions filters")
            c.check(page.locator("#empty-actions button:has-text('Clear filters')").count() == 1, "offers to clear filters")
        page.click("#filters-clear")
        page.wait_for_timeout(300)
        c.check(not visible(page, "#filter-chips .chip-item"), "filters cleared")
        page.click("#filters-toggle")
        c.check(not visible(page, "#filter-bar"), "the Filters button folds the pickers away again")
        page.set_viewport_size({"width": 375, "height": 780})
        c.check(visible(page, "#filters-toggle") and not visible(page, "#filter-bar"), "narrow screens fold filters away")
        page.click("#filters-toggle")
        c.check(visible(page, "#filter-bar") and page.get_attribute("#filters-toggle", "aria-expanded") == "true",
                "the filters toggle opens the bar")
        c.shot("11b-filters-mobile-light")
        page.click("#filters-toggle")
        page.set_viewport_size({"width": 1280, "height": 860})

    def weak_and_empty(self) -> None:
        page, c = self.page, self.c
        view = search(page, "what is the capital of mongolia")
        page.wait_for_timeout(200)
        abstained = visible(page, "#abstain-notice")
        c.check(view == "empty" or abstained or visible(page, "#weak-group"),
                f"unrelated question is flagged as weak (view={view})")
        if abstained:
            c.check("No strong matches" in text(page, "#abstain-notice"), "abstain notice copy")
            c.check(not visible(page, "#weak-list"), "weak matches start collapsed")
            c.check("No strong matches" in text(page, "#results-summary"), f"summary agrees: {text(page, '#results-summary')!r}")
            c.check(not visible(page, "#weak-toggle"), "one show/hide control, not two")
            c.check(not visible(page, "#results-tools"), "no save/export for a search without strong matches")
            c.shot("12-abstain-light")
            page.click("#abstain-notice button:has-text('Show')")
            c.check(visible(page, "#weak-list"), "'Show weaker matches' expands them")
            c.shot("13-weak-open-light")
            page.click("#abstain-notice .abstain-toggle")
            c.check(not visible(page, "#weak-list"), "the same button hides them again")
        page.check("#mode-group input[value='lexical']", force=True)
        page.wait_for_timeout(100)
        view = search(page, "zzqxv flibbertigibbet")
        c.check(view == "empty", f"nonsense keyword gives no results (view={view})")
        copy = text(page, "#empty-copy")
        c.check("any of the words" in copy, f"empty keyword copy says no word matched: {copy!r}")
        buttons = page.locator("#empty-actions button").all_text_contents()
        c.check(any("meaning" in b.lower() or "hybrid" in b.lower() for b in buttons), f"suggests another mode: {buttons}")
        c.check(not layout_problems(page), f"layout: {layout_problems(page)}")
        c.shot("14-empty-light")
        page.click("#empty-actions button:has-text('Search by meaning')")
        wait_js(page, "document.getElementById('results-pane').dataset.view !== 'empty' || true")
        page.wait_for_timeout(800)
        c.check(page.is_checked("#mode-group input[value='semantic']"), "switching mode from the empty state")
        page.check("#mode-group input[value='hybrid']", force=True)
        # Relaxed keyword matching: a question whose words never all appear together.
        page.check("#mode-group input[value='lexical']", force=True)
        view = search(page, "vitamin sourdough")
        c.check(view == "results", f"keyword search tops up partial matches (view={view})")
        c.check(visible(page, "#results-note") and "all of your keywords" in text(page, "#results-note"),
                f"partial-match note: {text(page, '#results-note')!r}")
        c.shot("14b-partial-matches-light")
        view = search(page, "citrus vitamin")
        c.check(not visible(page, "#results-note"), "no partial-match note when a note has every word")
        page.check("#mode-group input[value='hybrid']", force=True)
        page.wait_for_timeout(500)

    def modes(self) -> None:
        page, c = self.page, self.c
        search(page, "bicycle gears")
        page.click("#options-toggle")
        c.check(visible(page, "#options-panel"), "options panel opens")
        for value in ("int8", "binary", "float"):
            radio = page.locator(f"#precision-group input[value='{value}']")
            if radio.is_disabled():
                c.check(value == "int8", f"precision {value} is unexpectedly disabled")
                continue
            radio.check(force=True)
            wait_js(page, "document.getElementById('results-pane').dataset.view === 'results'", timeout=30000)
            page.wait_for_timeout(300)
            c.check("Bicycle" in " ".join(result_titles(page)[:3]), f"{value} precision finds the bicycle note")
        page.select_option("#result-count", "5")
        page.wait_for_timeout(600)
        c.check(page.locator("#view-results .result-card").count() <= 5, "result count control")
        page.select_option("#result-count", "10")
        page.wait_for_timeout(400)
        page.click("#options-toggle")
        page.check("#mode-group input[value='semantic']", force=True)
        page.wait_for_timeout(600)
        c.check("Meaning" in text(page, "#results-summary"), f"meaning mode summary: {text(page, '#results-summary')!r}")
        page.check("#mode-group input[value='hybrid']", force=True)
        page.wait_for_timeout(600)

    def saved_and_history(self) -> None:
        page, c = self.page, self.c
        search(page, "emergency fund savings")
        page.click("#save-search")
        page.wait_for_selector("#prompt-dialog[open]")
        page.fill("#prompt-input", "Money plan")
        page.click("#prompt-ok")
        c.check(wait_toast(page, "Search saved"), "save search toast")
        page.keyboard.press("Escape")
        page.locator("#search-input").focus()
        page.keyboard.press("Escape")
        wait_view(page, "idle")
        page.wait_for_selector("#saved-list .list-primary:has-text('Money plan')")
        recent = page.locator("#recent-list .list-primary").all_text_contents()
        modes = [t.split(" · ")[0] for t in page.locator("#recent-list .list-secondary").all_text_contents()]
        c.check("emergency fund savings" in recent, f"history lists the search: {recent}")
        pairs = list(zip(recent, modes))
        c.check(len(pairs) == len(set(pairs)), f"history is de-duplicated by query and mode: {pairs}")
        clear_toasts(page)
        clear_toasts(page)
        page.click("#saved-list .list-button:has-text('Money plan')")
        wait_view(page, "results")
        c.check(page.input_value("#search-input") == "emergency fund savings", "saved search runs its query")
        page.locator("#search-input").focus()
        page.keyboard.press("Escape")
        wait_view(page, "idle")
        first = page.locator("#recent-list .list-primary").first.text_content().strip()
        page.click("#recent-list .list-button >> nth=0")
        wait_view(page, "results")
        c.check(page.input_value("#search-input") == first, f"history item re-runs the search ({first!r})")
        page.locator("#search-input").focus()
        page.keyboard.press("Escape")
        wait_view(page, "idle")
        c.shot("15-idle-lists-light")
        # remove the saved search
        page.click("#saved-list button[aria-label^='Remove saved search']")
        page.wait_for_selector("#confirm-dialog[open]")
        page.click("#confirm-ok")
        page.wait_for_selector("#saved-list .list-empty", state="attached")
        c.check(not visible(page, "#saved-list"), "an empty Saved searches card is hidden")
        c.check(page.evaluate("document.activeElement && document.activeElement !== document.body"),
                "focus stays on the page after removing a saved search")
        page.click("#history-clear")
        page.wait_for_selector("#confirm-dialog[open]")
        page.click("#confirm-ok")
        wait_js(page, "document.getElementById('recent-title').textContent === 'Get started'")
        c.check(not visible(page, "#history-clear"), "history cleared")
        c.check(page.locator("#recent-list .tip-row").count() == 3, "an empty history shows getting-started tips")

    def bookmarks_and_recent(self) -> None:
        page, c = self.page, self.c
        bookmarks = page.locator("#bookmarks-list .list-primary").all_text_contents()
        c.check(any("Citrus" in b for b in bookmarks), f"bookmark is on the home screen: {bookmarks}")
        page.click("#bookmarks-list .list-button")
        page.wait_for_selector("#preview:not([hidden])")
        wait_js(page, "document.getElementById('preview-title').textContent !== 'Loading…'")
        c.check("Citrus" in text(page, "#preview-title"), "bookmark opens the preview")
        page.keyboard.press("Escape")
        page.wait_for_selector("#preview", state="hidden")
        docs = page.locator("#docs-list .list-secondary").all_text_contents()
        c.check(bool(docs), f"recent documents have details: {docs[:2]}")
        page.click("#bookmarks-list button[aria-label^='Remove bookmark']")
        c.check(wait_toast(page, "Bookmark removed"), "bookmark removed from home")
        page.wait_for_selector("#bookmarks-list .list-empty", state="attached")
        c.check(not visible(page, "#bookmarks-list"), "an empty Bookmarks card is hidden")

    def export(self) -> None:
        page, c = self.page, self.c
        search(page, "compost")
        page.click("#export-button")
        page.wait_for_selector("#export-menu:not([hidden])")
        with page.expect_download() as info:
            page.click("#export-menu [data-export='markdown']")
        download = info.value
        content = Path(download.path()).read_text(encoding="utf-8")
        c.check(download.suggested_filename == "onefind-compost.md", f"markdown file name {download.suggested_filename}")
        unicode_name = page.evaluate("import('/assets/views-results.js').then((m) => m.exportName('会議 notes', 'md'))")
        c.check(unicode_name == "onefind-会議-notes.md", f"export names keep non-Latin letters: {unicode_name}")
        c.check("# Search results: “compost”" in content and "ompost" in content, "markdown export content")
        page.click("#export-button")
        with page.expect_download() as info:
            page.click("#export-menu [data-export='json']")
        data = json.loads(Path(info.value.path()).read_text(encoding="utf-8"))
        c.check(data.get("query") == "compost" and data.get("results"), "json export content")

    def manage_health(self) -> None:
        page, c = self.page, self.c
        page.locator("#search-input").focus()
        page.keyboard.press("Escape")
        # Change the notes folder behind the app's back.
        (self.notes / "Personal" / "new-note.md").write_text(
            "# Garden plan\n\nTomatoes go next to the basil this spring.\n", encoding="utf-8")
        circadian = self.notes / "Personal" / "health" / "circadian-rhythm.md"
        circadian.write_text(circadian.read_text(encoding="utf-8") + "\nAdded a note about naps.\n", encoding="utf-8")
        page.click("#manage-button")
        page.wait_for_selector("#manage-dialog[open]")
        wait_js(page, "document.getElementById('manage-health-content').textContent.includes('since the last index')",
                               timeout=20000)
        health = text(page, "#manage-health-content")
        c.check("1 new, 1 changed" in health, f"stale changes are reported: {health[:200]!r}")
        c.check("Documents" in health and "Vectors" in health, "health lists documents and vectors")
        c.check(not layout_problems(page), f"layout: {layout_problems(page)}")
        c.shot("16-manage-health-light")
        page.click("#manage-health-content button:has-text('Update index')")
        page.wait_for_selector("#index-dialog[open]")
        page.wait_for_selector("#index-dialog[data-step='done']", timeout=120000)
        summary = {dt.strip(): dd.strip() for dt, dd in zip(
            page.locator("#done-summary dt").all_text_contents(),
            page.locator("#done-summary dd").all_text_contents())}
        c.check(summary.get("Added") == "1" and summary.get("Updated") == "1", f"update summary: {summary}")
        page.click("#index-foot button:has-text('Close')")
        page.wait_for_selector("#index-dialog", state="hidden")
        if not page.locator("#manage-dialog[open]").count():
            page.click("#manage-button")
            page.wait_for_selector("#manage-dialog[open]")
        wait_js(page, "document.getElementById('manage-health-content').textContent.includes('up to date')",
                               timeout=20000)
        c.check(True, "health is up to date after the update")
        tab = page.locator(".sheet-nav [data-section='manage-health']")
        c.check(tab.get_attribute("aria-current") == "true", "the section tab in view is marked current")
        view = search_hidden_check(page)
        c.check(view, "Manage stays open above the app")

    def manage_maintenance(self) -> None:
        page, c = self.page, self.c
        open_manage(page, "maintenance")
        page.click("#manage-maintenance-content button:has-text('Check integrity')")
        page.wait_for_selector("#maint-result:not([hidden])", timeout=30000)
        c.check("No problems found" in text(page, "#maint-result"), f"integrity check: {text(page, '#maint-result')!r}")
        c.shot("17-manage-check-light")
        page.click("#manage-maintenance-content button:has-text('Compact')")
        page.wait_for_selector("#compact-result:not([hidden])", timeout=30000)
        c.check("Compacted" in text(page, "#compact-result"), f"compact: {text(page, '#compact-result')!r}")
        page.wait_for_timeout(600)
        c.check(visible(page, "#compact-result") and visible(page, "#maint-result"),
                "action results stay visible when the stats refresh")
        c.check(page.locator("#manage-maintenance-content .status-line:has-text('Vectors complete')").count() == 1
                and page.locator("#manage-maintenance-content button:has-text('Reindex with vectors')").count() == 0,
                "complete vectors show as a status line, not a disabled button")
        # A failed check offers Repair: fake only the check's answer, the repair is real.
        page.route("**/api/maintenance/check", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"ok": False, "sqlite": "ok", "fts": "1 row out of step",
                             "problems": ["The keyword index is out of step with 1 note."],
                             "suggestions": ["Run Repair to rebuild it."]})))
        try:
            page.click("#manage-maintenance-content button:has-text('Check integrity')")
            page.wait_for_selector("#maint-result button:has-text('Repair')")
            c.check("Some problems were found" in text(page, "#maint-result"), "failed check explains the problem")
            page.click("#maint-result button:has-text('Repair')")
            wait_js(page, "document.getElementById('maint-result').textContent.includes('Repair finished')", timeout=60000)
            c.check("Rebuilt the keyword index" in text(page, "#maint-result"), f"repair report: {text(page, '#maint-result')[:160]!r}")
            c.shot("17b-manage-repair-light")
        finally:
            page.unroute("**/api/maintenance/check")

    def manage_backups(self) -> None:
        page, c = self.page, self.c
        open_manage(page, "backups")
        page.focus("#manage-backups-content button:has-text('Back up now')")
        page.keyboard.press("Enter")
        page.wait_for_selector("#manage-backups-content .backup-item", timeout=30000)
        c.check(wait_toast(page, "Backup created"), f"backup toast: {toast_text(page)!r}")
        page.wait_for_timeout(200)
        c.check(page.evaluate("document.activeElement?.dataset.focusKey") == "backup-now",
                "focus stays on 'Back up now' after the list refreshes")
        box = page.evaluate("""() => { const t = document.querySelector('#toasts .toast'); if (!t) return null;
            const r = t.getBoundingClientRect(); return [r.right, window.innerWidth]; }""")
        c.check(box is not None and box[1] - box[0] < 40, f"toasts sit in the window corner, not inside the panel: {box}")
        c.check(page.locator("#toasts .toast").first.is_visible(), "toast is visible above the open Manage panel")
        top = page.evaluate("""() => { const t = document.querySelector('#toasts .toast');
            if (!t) return null; const r = t.getBoundingClientRect();
            const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
            return Boolean(hit && t.contains(hit)); }""")
        c.check(top is True, "toast is on top of the modal Manage panel (not hidden behind it)")
        c.shot("18-manage-backups-light")
        backups = self.server.api("GET", "/api/backups")["items"]
        c.check(len(backups) == 1, f"one backup listed by the API: {len(backups)}")
        # restore as a new collection
        page.click("#manage-backups-content .backup-item button:has-text('Restore…')")
        page.click("#manage-backups-content .restore-choices button:has-text('Restore as a new collection')")
        page.wait_for_selector("#prompt-dialog[open]")
        page.fill("#prompt-input", "Notes copy")
        page.click("#prompt-ok")
        wait_js(page, "document.getElementById('collection-name').textContent === 'Notes copy'", timeout=20000)
        c.check(True, "restored as a new collection and switched to it")
        stats = self.server.api("GET", "/api/stats")
        c.check(stats["documents"] > 30 and stats["collection"]["name"] == "Notes copy", f"restored stats: {stats['documents']}")
        # replace: back up "Notes copy", then replace it from that backup with a typed confirmation
        open_manage(page, "backups")
        page.click("#manage-backups-content button:has-text('Back up now')")
        wait_js(page, "document.querySelectorAll('#manage-backups-content .backup-item').length >= 1")
        page.wait_for_timeout(400)
        item = page.locator("#manage-backups-content .backup-item").first
        item.locator("button:has-text('Restore…')").click()
        item.locator("button:has-text('Replace current')").click()
        page.wait_for_selector("#confirm-dialog[open]")
        c.check(page.is_disabled("#confirm-ok"), "replace needs the typed name")
        page.fill("#confirm-type-input", "notes copy")
        c.check(page.is_disabled("#confirm-ok"), "typed name must match exactly")
        page.fill("#confirm-type-input", "Notes copy")
        c.check(not page.is_disabled("#confirm-ok"), "exact name enables replace")
        page.wait_for_timeout(350)  # past the dialog's opening motion
        covered = page.evaluate("""() => { const ok = document.getElementById('confirm-ok').getBoundingClientRect();
            return [...document.querySelectorAll('#toasts .toast')].some((t) => { const r = t.getBoundingClientRect();
              return r.left < ok.right && r.right > ok.left && r.top < ok.bottom && r.bottom > ok.top; }); }""")
        c.check(not covered, "no toast covers the confirmation buttons")
        c.shot("19-confirm-replace-light")
        page.click("#confirm-ok")
        c.check(wait_toast(page, "restored from backup"), f"replace toast: {toast_text(page)!r}")
        backups = self.server.api("GET", "/api/backups")["items"]
        c.check(len(backups) >= 2, f"a safety backup was made before replacing ({len(backups)} backups)")

    def collections(self) -> None:
        page, c = self.page, self.c
        clear_toasts(page)
        if page.locator("#manage-dialog[open]").count():
            page.click("#manage-dialog .dialog-head [data-close]")
        # header menu
        page.click("#collection-button")
        page.wait_for_selector("#collection-menu:not([hidden])")
        items = page.locator("#collection-menu [role='menuitemradio'] .menu-primary").all_text_contents()
        c.check(items == ["My Notes", "Notes copy"] or sorted(items) == ["My Notes", "Notes copy"], f"menu lists collections: {items}")
        c.shot("20-collection-menu-light")
        page.click("#collection-menu [role='menuitemradio']:has-text('My Notes')")
        wait_js(page, "document.getElementById('collection-name').textContent === 'My Notes'")
        c.check(wait_toast(page, "Switched to"), "switch toast")
        # create
        page.click("#collection-button")
        page.click("#collection-menu .menu-item:has-text('New collection')")
        page.wait_for_selector("#prompt-dialog[open]")
        page.fill("#prompt-input", "My Notes")
        with c.expecting("POST", r"^/api/collections$", 409):
            page.click("#prompt-ok")
            c.check(wait_toast(page, "already exists"), f"duplicate name is refused: {toast_text(page)!r}")
        clear_toasts(page)
        page.click("#collection-button")
        page.click("#collection-menu .menu-item:has-text('New collection')")
        page.wait_for_selector("#prompt-dialog[open]")
        page.fill("#prompt-input", "Scratch")
        page.click("#prompt-ok")
        page.wait_for_selector("#index-dialog[open]")
        c.check(text(page, "#collection-name") == "Scratch", "new collection becomes active")
        page.click("#index-foot button:has-text('Cancel')")
        page.wait_for_selector("#index-dialog", state="hidden")
        wait_view(page, "onboarding")
        c.check("is empty" in text(page, "#welcome-eyebrow"), f"empty collection onboarding: {text(page, '#welcome-eyebrow')!r}")
        c.check(text(page, "#status-label") == "No index", "empty collection status")
        c.check(not visible(page, "#mode-group") and not visible(page, "#filters-toggle"),
                "search modes and filters stay hidden until there is something to search")
        c.check(page.evaluate("window.scrollY") == 0, "switching collections returns to the top of the page")
        c.shot("21-empty-collection-light")
        # open an existing file from another place
        page.click("#onboard-open")
        page.wait_for_selector("#prompt-dialog[open]")
        page.fill("#prompt-input", str(self.archive_db))
        page.click("#prompt-ok")
        wait_js(page, "document.getElementById('collection-name').textContent === 'archive'", timeout=20000)
        wait_view(page, "idle")
        c.check(page.locator("#mode-group input[value='semantic']").is_disabled(), "keyword-only collection disables meaning mode")
        c.check("Build vectors" in text(page, "#mode-note"), f"mode note offers vectors: {text(page, '#mode-note')!r}")
        c.check(page.get_attribute("#search-input", "placeholder") == "Search your notes by keyword.", "keyword placeholder")
        view = search(page, "governance")
        c.check(view == "results", "opened collection is searchable")
        page.locator("#search-input").focus()
        page.keyboard.press("Escape")
        # rename + remove through Manage
        page.click("#manage-button")
        page.wait_for_selector("#manage-dialog[open]")
        page.click(".sheet-nav [data-section='manage-collections']")
        page.wait_for_selector("#manage-collections-content .collection-row")
        row = page.locator("#manage-collections-content .collection-row:has-text('Scratch')")
        row.locator("button[aria-label^='Rename']").click()
        page.wait_for_selector("#prompt-dialog[open]")
        page.fill("#prompt-input", "Scratch pad")
        page.click("#prompt-ok")
        page.wait_for_selector("#manage-collections-content .collection-row:has-text('Scratch pad')")
        c.check(True, "rename shows in the list")
        c.shot("22-manage-collections-light")
        row = page.locator("#manage-collections-content .collection-row:has-text('Scratch pad')")
        row.locator("button[aria-label^='Remove']").click()
        page.wait_for_selector("#confirm-dialog[open]")
        page.click("#confirm-ok")
        wait_js(page, "!document.getElementById('manage-collections-content').textContent.includes('Scratch pad')")
        c.check(text(page, "#collection-name") == "archive", "removing another collection keeps the active one")
        row = page.locator("#manage-collections-content .collection-row:has-text('Notes copy')")
        c.check(row.locator("button:has-text('Rename')").count() == 1 and row.locator("button:has-text('Delete file')").count() == 1,
                "collection row actions carry visible labels")
        row.locator("button[aria-label^='Delete file']").click()
        page.wait_for_selector("#confirm-dialog[open]")
        c.check(page.is_disabled("#confirm-ok"), "deleting a file needs the typed name")
        page.fill("#confirm-type-input", "Notes copy")
        page.click("#confirm-ok")
        wait_js(page, "!document.getElementById('manage-collections-content').textContent.includes('Notes copy')")
        names = [entry["name"] for entry in self.server.api("GET", "/api/collections")["collections"]]
        c.check(sorted(names) == ["My Notes", "archive"], f"collections after removal: {names}")

    def add_vectors(self) -> None:
        page, c = self.page, self.c
        open_manage(page, "maintenance")
        page.click("#manage-maintenance-content button:has-text('Reindex with vectors')")
        page.wait_for_selector("#index-dialog[open]")
        page.wait_for_selector("#index-dialog[data-step='done']", timeout=180000)
        c.check("keyword and meaning" in text(page, "#done-copy"), f"vectors built: {text(page, '#done-copy')!r}")
        page.click("#index-foot button:has-text('Close')")
        page.wait_for_selector("#index-dialog", state="hidden")
        if page.locator("#manage-dialog[open]").count():
            page.click("#manage-dialog .dialog-head [data-close]")
        wait_js(page, "!document.querySelector(\"#mode-group input[value='semantic']\").disabled", timeout=20000)
        c.check(True, "meaning mode is enabled once vectors exist")
        view = search(page, "who approves changes to the rules")
        c.check(view == "results", "meaning search works on the opened collection")
        page.locator("#search-input").focus()
        page.keyboard.press("Escape")

    def clear_collection(self) -> None:
        page, c = self.page, self.c
        page.click("#manage-button")
        page.wait_for_selector("#manage-dialog[open]")
        page.click(".sheet-nav [data-section='manage-collections']")
        page.click("#manage-collections-content button:has-text('Clear collection')")
        page.wait_for_selector("#confirm-dialog[open]")
        c.check(page.is_disabled("#confirm-ok"), "clear needs the typed name")
        page.fill("#confirm-type-input", "archive")
        page.click("#confirm-ok")
        wait_js(page, "document.getElementById('status-label').textContent === 'No index'", timeout=20000)
        if page.locator("#manage-dialog[open]").count():
            page.click("#manage-dialog .dialog-head [data-close]")
        wait_view(page, "onboarding")
        c.check("is empty" in text(page, "#welcome-eyebrow"), "cleared collection shows the empty onboarding")
        page.click("#collection-button")
        page.click("#collection-menu [role='menuitemradio']:has-text('My Notes')")
        wait_js(page, "document.getElementById('collection-name').textContent === 'My Notes'")
        wait_view(page, "idle")

    def done_summary(self) -> dict[str, str]:
        page = self.page
        return {dt.strip(): dd.strip() for dt, dd in zip(
            page.locator("#done-summary dt").all_text_contents(),
            page.locator("#done-summary dd").all_text_contents())}

    def indexing_more(self) -> None:
        page, c = self.page, self.c
        # Add a second folder to the same collection.
        open_manage(page, "maintenance")
        page.click("#manage-maintenance-content button:has-text('Add a folder')")
        page.wait_for_selector("#index-dialog[open][data-step='choose']")
        c.check(not visible(page, "#index-name-row"), "adding a folder does not ask for a collection name")
        page.fill("#fs-path", str(self.extra))
        page.press("#fs-path", "Enter")
        page.wait_for_selector("#fs-crumbs .crumb-current:has-text('travel')")
        page.click("#index-start")
        page.wait_for_selector("#index-dialog[data-step='done']", timeout=120000)
        summary = self.done_summary()
        c.check(summary.get("Added") == "2", f"added folder summary: {summary}")
        page.click("#index-foot button:has-text('Close')")
        page.wait_for_selector("#index-dialog", state="hidden")
        close_manage(page)
        view = search(page, "tram to the castle")
        titles = result_titles(page)
        c.check(view == "results" and bool(titles) and "Lisbon" in titles[0], f"new folder is searchable: {titles[:2]}")
        page.locator("#search-input").focus()
        page.keyboard.press("Escape")
        # Full rebuild re-reads every file of every folder.
        open_manage(page, "maintenance")
        page.click("#manage-maintenance-content button:has-text('Full rebuild')")
        page.wait_for_selector("#confirm-dialog[open]")
        page.click("#confirm-ok")
        page.wait_for_selector("#index-dialog[data-step='done']", timeout=180000)
        summary = self.done_summary()
        c.check(summary.get("Added", "0") == "0" and summary.get("Unchanged", "0") == "0" and int(summary.get("Updated", "0")) > 0,
                f"full rebuild summary: {summary}")
        page.click("#index-foot button:has-text('Close')")
        page.wait_for_selector("#index-dialog", state="hidden")
        close_manage(page)
        # Cancel a long job in a throwaway collection.
        page.click("#collection-button")
        page.click("#collection-menu .menu-item:has-text('New collection')")
        page.wait_for_selector("#prompt-dialog[open]")
        page.fill("#prompt-input", "Bulk")
        page.click("#prompt-ok")
        page.wait_for_selector("#index-dialog[open][data-step='choose']")
        wait_js(page, "!document.getElementById('fs-list').hasAttribute('aria-busy')")  # the first listing is in
        page.fill("#fs-path", str(self.bulk))
        page.press("#fs-path", "Enter")
        page.wait_for_selector("#fs-crumbs .crumb-current:has-text('bulk')")
        page.click("#index-start")
        page.wait_for_selector("#index-foot button:has-text('Cancel indexing')", timeout=20000)
        page.click("#index-foot button:has-text('Cancel indexing')")
        page.wait_for_selector("#index-dialog[data-step='failed'], #index-dialog[data-step='done']", timeout=60000)
        step = page.get_attribute("#index-dialog", "data-step")
        c.check(step == "failed" and "cancelled" in text(page, "#index-title").lower(),
                f"cancelled job is reported (step={step}, title={text(page, '#index-title')!r})")
        c.shot("24-indexing-cancelled-light")
        c.check(page.locator("#index-foot button:has-text('Try again')").count() == 1, "offers to try again")
        page.click("#index-foot button:has-text('Close')")
        page.wait_for_selector("#index-dialog", state="hidden")
        wait_js(page, "!['Indexing', 'Connecting'].some((s) => document.getElementById('status-label').textContent.startsWith(s))",
                timeout=20000)
        c.check(text(page, "#status-label") in ("Ready", "No index", "Vectors missing"),
                f"status settles after cancelling: {text(page, '#status-label')!r}")
        page.click("#collection-button")
        page.click("#collection-menu [role='menuitemradio']:has-text('My Notes')")
        wait_js(page, "document.getElementById('collection-name').textContent === 'My Notes'")
        open_manage(page, "collections")
        row = page.locator("#manage-collections-content .collection-row:has-text('Bulk')")
        row.locator("button[aria-label^='Delete file']").click()
        page.wait_for_selector("#confirm-dialog[open]")
        page.fill("#confirm-type-input", "Bulk")
        page.click("#confirm-ok")
        wait_js(page, "!document.getElementById('manage-collections-content').textContent.includes('Bulk')")
        close_manage(page)
        wait_view(page, "idle")

    # -- regression checks for review findings ------------------------------------------

    def _history_queries(self) -> list[str]:
        return [item["query"] for item in self.server.api("GET", "/api/history?limit=200")["items"]]

    def _stats_requests(self) -> int:
        return sum(1 for method, path in self.c.requests if method == "GET" and path == "/api/stats")

    def _wait_job(self, job: dict, timeout: float = 120) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.server.api("GET", f"/api/jobs/{job['id']}")
            if job["status"] in ("succeeded", "failed", "cancelled"):
                return job
            time.sleep(0.2)
        raise TimeoutError(f"job {job['id']} did not finish")

    def _module(self, name: str, call: str):
        """Call into one of the page's own ES modules (same instance the app uses)."""
        return self.page.evaluate(f"import('/assets/{name}').then((m) => {call})")

    def deep_link(self) -> None:
        page, c = self.page, self.c
        probe = "citrus deep link probe"
        page.goto(self.server.url + "/?q=" + urllib.parse.quote_plus(probe))
        wait_js(page, "['results', 'empty'].includes(document.getElementById('results-pane').dataset.view)", timeout=30000)
        c.check(page.input_value("#search-input") == probe, "a link with ?q= fills the search box and searches")
        page.wait_for_timeout(2300)  # longer than the deferred history commit
        c.check(probe not in self._history_queries(), "a search opened from a link is not recorded on its own")
        page.locator("#search-input").press("Enter")
        deadline = time.time() + 10
        while probe not in self._history_queries() and time.time() < deadline:
            time.sleep(0.2)
        c.check(probe in self._history_queries(), "pressing Enter on it records it")
        page.locator("#search-input").focus()
        page.keyboard.press("Escape")
        wait_view(page, "idle")

    def short_query_abort(self) -> None:
        page, c = self.page, self.c
        # Every search waits 1.5 s in the page before it is sent (and honours abort).
        page.evaluate("""() => {
            window.__origFetch = window.fetch;
            window.fetch = async (url, init) => {
              if (String(url).includes('/api/search')) {
                await new Promise((resolve) => setTimeout(resolve, 1500));
                if (init?.signal?.aborted) throw new DOMException('The operation was aborted.', 'AbortError');
              }
              return window.__origFetch(url, init);
            };
        }""")
        try:
            box = page.locator("#search-input")
            box.click()
            box.press_sequentially("rollback", delay=30)
            page.wait_for_timeout(450)  # the debounced search is now running
            for _ in range(7):
                box.press("Backspace")
            page.wait_for_timeout(2600)
            c.check(page.input_value("#search-input") == "r", "the box holds one character")
            view = current_view(page)
            c.check(view in ("idle", "typing"), f"results for the longer query never replace the home screen (view={view})")
            c.check(page.locator("#results-list .result-card").count() == 0 or view != "results", "no stale results shown")
        finally:
            page.evaluate("() => { if (window.__origFetch) window.fetch = window.__origFetch; }")
        page.locator("#search-input").focus()
        page.keyboard.press("Escape")
        wait_view(page, "idle")

    def job_stats_and_failure(self) -> None:
        page, c = self.page, self.c
        job_id = "fakejob00001"
        fake = {"status": "running"}

        def job_body() -> dict:
            body = {"id": job_id, "kind": "index", "status": fake["status"], "phase": "read", "done": 3,
                    "total": 10, "root": str(self.notes), "message": "Reading", "health": {"vectors": 0}}
            if fake["status"] == "failed":
                body["error"] = {"code": "model_unavailable", "action": "switch_mode", "retryable": True,
                                 "message": "The meaning-search model could not be loaded."}
            return body

        page.route(f"**/api/jobs/{job_id}", lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(job_body())))
        try:
            before = self._stats_requests()
            self._module("views-index.js", f"m.trackJob({json.dumps(job_body())})")
            page.wait_for_timeout(4700)
            during = self._stats_requests() - before
            c.check(during >= 2, f"collection status refreshes while a job runs ({during} /api/stats requests in 4.7 s)")
            fake["status"] = "failed"
            wait_js(page, "import('/assets/views-index.js').then((m) => !m.jobRunning())", timeout=10000)
        finally:
            page.unroute(f"**/api/jobs/{job_id}")
        clear_toasts(page)
        # The failure screen follows the server's hint instead of only "Try again".
        self._module("views-index.js", "m.showJobProgress()")
        page.wait_for_selector("#index-dialog[open][data-step='failed']")
        foot = [t.strip() for t in page.locator("#index-foot button").all_text_contents()]
        c.check("Index for keyword search" in " ".join(foot) and "Try again" in " ".join(foot),
                f"a model failure offers keyword-only indexing: {foot}")
        page.click("#index-foot button:has-text('Index for keyword search')")
        page.wait_for_selector("#index-dialog[data-step='choose']")
        c.check(not page.is_checked("#index-embed"), "the retry is set up without vectors")
        page.click("#index-foot button:has-text('Cancel')")
        page.wait_for_selector("#index-dialog", state="hidden")
        clear_toasts(page)

    def wizard_keeps_excludes(self) -> None:
        page, c = self.page, self.c
        open_manage(page, "maintenance")
        page.click("#manage-maintenance-content button:has-text('Add a folder')")
        page.wait_for_selector("#index-dialog[open][data-step='choose']")
        wait_js(page, "!document.getElementById('fs-list').hasAttribute('aria-busy')")
        page.fill("#fs-path", str(self.notes))
        page.press("#fs-path", "Enter")
        page.wait_for_selector("#fs-crumbs .crumb-current:has-text('notes')")
        # An already indexed folder shows its saved patterns (they are not sent unless edited).
        wait_js(page, "document.getElementById('index-exclude').value === 'drafts'", timeout=10000)
        c.check(True, "the exclude field is pre-filled with the folder's saved patterns")
        c.check("Options · 1 saved exclusion" in text(page, "#index-options-summary"),
                f"the Options summary says patterns are saved: {text(page, '#index-options-summary')!r}")
        c.check("Editing them replaces the saved patterns" in text(page, "#exclude-help"),
                f"the help explains what editing does: {text(page, '#exclude-help')!r}")
        page.click(".disclosure summary:has-text('Options')")
        c.shot("05b-wizard-saved-excludes-light")
        # Another folder (not indexed yet) clears the pre-fill again.
        page.fill("#fs-path", str(self.extra.parent))
        page.press("#fs-path", "Enter")
        page.wait_for_selector("#fs-crumbs .crumb-current:has-text('elsewhere')")
        c.check(page.input_value("#index-exclude") == "", "a folder without saved patterns shows an empty field")
        page.fill("#fs-path", str(self.notes))
        page.press("#fs-path", "Enter")
        page.wait_for_selector("#fs-crumbs .crumb-current:has-text('notes')")
        wait_js(page, "document.getElementById('index-exclude').value === 'drafts'", timeout=10000)
        page.click(".disclosure summary:has-text('Options')")
        page.click("#index-start")
        page.wait_for_selector("#index-dialog[data-step='done']", timeout=120000)
        summary = self.done_summary()
        c.check(summary.get("Added", "0") == "0", f"re-running the wizard does not index excluded drafts: {summary}")
        newest = self.server.api("GET", "/api/jobs")["jobs"][0]
        c.check(newest.get("options", {}).get("exclude") is None,
                f"an untouched exclude field keeps the saved patterns: {newest.get('options')}")
        page.click("#index-foot button:has-text('Close')")
        page.wait_for_selector("#index-dialog", state="hidden")
        close_manage(page)
        search(page, "zeppelin logistics")
        c.check(page.locator("#results-pane .result-card[data-doc-id^='drafts/']").count() == 0,
                "excluded drafts stay out of search")
        page.locator("#search-input").focus()
        page.keyboard.press("Escape")
        wait_view(page, "idle")

    def single_files(self) -> None:
        page, c = self.page, self.c
        long_dir = self.elsewhere / "long"
        long_dir.mkdir(parents=True, exist_ok=True)
        filler = ("Notes about the weekly market: bread, cheese, apples and pears were on sale, "
                  "and the stalls opened early because of the warm weather.")
        paragraphs = ["# Field journal", "A quokka was mentioned once at the start."]
        paragraphs += [f"Day {n}. {filler}" for n in range(1, 80)]
        paragraphs.insert(70, "The quokka lives on Rottnest Island, where the quokka greets visitors near the "
                              "ferry; Rottnest Island rangers count every quokka each spring and record where the "
                              "families shelter during the hottest weeks of the year.")
        (long_dir / "long-note.md").write_text("\n\n".join(paragraphs) + "\n", encoding="utf-8")
        datasets = self.elsewhere / "datasets"
        datasets.mkdir(parents=True, exist_ok=True)
        rows = [
            {"_id": "fact-1", "title": "Narwhal tusks", "text": "A narwhal tusk is a long spiral tooth."},
            {"_id": "fact-2", "title": "Axolotl gills", "text": "An axolotl breathes through feathery gills."},
            {"_id": "fact-3", "title": "Pangolin scales", "text": "A pangolin is covered in keratin scales."},
        ]
        (datasets / "facts.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

        clear_toasts(page)
        page.click("#collection-button")
        page.click("#collection-menu .menu-item:has-text('New collection')")
        page.wait_for_selector("#prompt-dialog[open]")
        page.fill("#prompt-input", "Files")
        page.click("#prompt-ok")
        page.wait_for_selector("#index-dialog[open][data-step='choose']")
        # A typed file path offers that file instead of "does not exist".
        wait_js(page, "!document.getElementById('fs-list').hasAttribute('aria-busy')")
        page.fill("#fs-path", str(long_dir / "long-note.md"))
        with c.expecting("GET", r"^/api/fs/list$", 404):
            page.press("#fs-path", "Enter")
            page.wait_for_selector("#fs-list .browser-file-button[aria-pressed='true']")
        c.check(not visible(page, "#fs-error"), f"a file path is not reported as missing: {text(page, '#fs-error')!r}")
        c.check("Index “long-note.md”" in text(page, "#index-start"), f"start button names the file: {text(page, '#index-start')!r}")
        c.check("Only “long-note.md”" in text(page, "#fs-count"), f"the wizard says only the file is indexed: {text(page, '#fs-count')!r}")
        page.click("#index-start")
        page.wait_for_selector("#index-dialog[data-step='done']", timeout=120000)
        c.check(self.done_summary().get("Added") == "1", f"single file summary: {self.done_summary()}")
        # Opened from the header menu, whose button stays visible: focus must still land in the search box.
        page.click("#index-foot button:has-text('Start searching')")
        page.wait_for_selector("#index-dialog", state="hidden")
        page.wait_for_timeout(350)
        c.check(page.evaluate("document.activeElement.id") == "search-input",
                f"'Start searching' focuses the search box (focus on {page.evaluate('document.activeElement.id')!r})")
        page.keyboard.type("quokka Rottnest Island")
        page.keyboard.press("Enter")
        wait_js(page, "document.getElementById('results-pane').dataset.view === 'results'", timeout=60000)
        c.check(page.input_value("#search-input") == "quokka Rottnest Island", "typing goes into the search box")
        # The preview opens at the passage the result was found in.
        page.locator("#results-list .result-card").first.locator("button:has-text('Preview')").click()
        page.wait_for_selector("#preview:not([hidden])")
        wait_js(page, "document.getElementById('preview-title').textContent !== 'Loading…'")
        page.wait_for_timeout(400)
        where = page.evaluate("""() => {
            const body = document.getElementById('preview-body');
            const mark = body.querySelector('mark.is-current');
            if (!mark) return null;
            const b = body.getBoundingClientRect(), m = mark.getBoundingClientRect();
            return { scrolled: body.scrollTop, inView: m.top >= b.top && m.bottom <= b.bottom,
                     paragraph: mark.closest('p').textContent.slice(0, 40),
                     count: document.getElementById('preview-find-count').textContent };
        }""")
        c.check(bool(where) and where["scrolled"] > 0 and where["inView"], f"the preview scrolls to the match: {where}")
        c.check(bool(where) and "Rottnest" in where["paragraph"], f"it lands on the snippet's passage: {where}")
        c.check(bool(where) and re.match(r"^\d+ of \d+$", where["count"] or "") is not None, f"match counter: {where}")
        page.keyboard.press("Escape")
        page.wait_for_selector("#preview", state="hidden")
        page.locator("#search-input").focus()
        page.keyboard.press("Escape")
        # A JSONL dataset can be picked in the folder browser.
        open_manage(page, "maintenance")
        page.click("#manage-maintenance-content button:has-text('Add a folder')")
        page.wait_for_selector("#index-dialog[open][data-step='choose']")
        wait_js(page, "!document.getElementById('fs-list').hasAttribute('aria-busy')")  # the first listing is in
        page.fill("#fs-path", str(datasets))
        page.press("#fs-path", "Enter")
        page.wait_for_selector("#fs-crumbs .crumb-current:has-text('datasets')")
        page.click("#fs-list .browser-file-button:has-text('facts.jsonl')")
        c.check("Index “facts.jsonl”" in text(page, "#index-start"), f"a JSONL dataset can be picked: {text(page, '#index-start')!r}")
        page.click("#index-start")
        page.wait_for_selector("#index-dialog[data-step='done']", timeout=120000)
        c.check(self.done_summary().get("Added") == "3", f"dataset summary: {self.done_summary()}")
        page.click("#index-foot button:has-text('Close')")
        page.wait_for_selector("#index-dialog", state="hidden")
        open_manage(page, "collections")
        info = text(page, "#manage-collection-content")
        c.check("What does not travel" in info and "indexed a second time" in info,
                f"portability copy says what does not travel: {info[-200:]!r}")
        close_manage(page)
        self._module("views-header.js", "m.switchCollection('My Notes')")
        wait_js(page, "document.getElementById('collection-name').textContent === 'My Notes'")
        self.server.api("POST", "/api/collections/remove", {"name": "Files", "delete_file": True, "confirm_name": "Files"})
        clear_toasts(page)

    def long_notes(self) -> None:
        """A collection made before passage search: the gentle offer, the rebuild job, the passage preview."""
        page, c = self.page, self.c
        from onefind.store import Index

        folder = self.elsewhere / "field-journal"
        folder.mkdir(parents=True, exist_ok=True)
        filler = ("Notes about the weekly market: bread, cheese, apples and pears were on sale, "
                  "and the stalls opened early because of the warm weather.")
        paragraphs = ["# Island field journal", "The ferry left on time and the crossing was calm."]
        paragraphs += [f"Day {n}. {filler}" for n in range(1, 50)]
        paragraphs.insert(40, "On Rottnest Island the quokka colony shelters under the peppermint "
                              "trees; rangers count every quokka each spring near the salt lakes.")
        (folder / "island-journal.md").write_text("\n\n".join(paragraphs) + "\n", encoding="utf-8")
        (folder / "packing.md").write_text("# Packing\n\nSunscreen, hat and a water bottle.\n", encoding="utf-8")

        created = self.server.api("POST", "/api/collections", {"name": "Long notes"})
        db = created.get("db") or next(item["db"] for item in self.server.api("GET", "/api/collections")["collections"]
                                       if item["name"] == "Long notes")
        with Index.open(db) as index:  # as collections made before 1.2.1: one vector per note
            index.set_chunking(None)
        self._module("views-header.js", "m.switchCollection('Long notes')")
        wait_js(page, "document.getElementById('collection-name').textContent === 'Long notes'")
        job = self.server.api("POST", "/api/index", {"path": str(folder), "embed": True,
                                                     "collection": "Long notes", "auto_sync": False})
        c.check(self._wait_job(job, timeout=180)["status"] == "succeeded", "the old-style collection was indexed")
        stats = self.server.api("GET", "/api/stats")
        c.check(stats["chunking"] is None and stats["vectors_need_rebuild"]["recommended"] is True,
                f"stats recommend passage search: {stats['chunking']} {stats['vectors_need_rebuild']}")
        self._module("app.js", "m.refreshStats()")

        # 1. The gentle offer in Manage -> Health.
        open_manage(page, "health")
        page.wait_for_selector("#passage-callout[data-kind='improve']", timeout=15000)
        callout = text(page, "#passage-callout")
        c.check("Improve meaning search for long notes" in callout and "1 note is longer than one passage" in callout,
                f"the callout explains the offer: {callout!r}")
        c.check("only the beginning" in text(page, "#manage-health-content"), "the Long notes row says what is missing")
        c.check(not layout_problems(page), f"layout: {layout_problems(page)}")
        c.shot("29-manage-long-notes-light")

        # 2. "Split long notes" runs a rebuild job in the progress dialog.
        page.click("#passage-callout button:has-text('Split long notes')")
        page.wait_for_selector("#index-dialog[open]")
        page.wait_for_selector("#index-dialog[data-step='progress'], #index-dialog[data-step='done']", timeout=20000)
        labels = [t.strip() for t in page.locator("#job-steps .stepper-label").all_text_contents()]
        c.check(labels == ["Find long notes", "Split long notes", "Build vectors", "Ready"] or
                page.get_attribute("#index-dialog", "data-step") == "done",
                f"the stepper uses rebuild labels: {labels}")
        if page.get_attribute("#index-dialog", "data-step") == "progress":
            c.check(page.locator("#index-foot button:has-text('Cancel rebuild')").count() == 1,
                    "the rebuild can be cancelled")
        page.wait_for_selector("#index-dialog[data-step='done']", timeout=180000)
        c.check(text(page, "#index-title") == "Vectors rebuilt", f"done title: {text(page, '#index-title')!r}")
        c.check(self.done_summary().get("Notes updated") == "1", f"rebuild summary: {self.done_summary()}")
        c.check("passage by passage" in text(page, "#done-copy"), f"done copy: {text(page, '#done-copy')!r}")
        c.shot("29b-rebuild-done-light")
        page.click("#index-foot button:has-text('Close')")
        page.wait_for_selector("#index-dialog", state="hidden")
        wait_js(page, "!document.getElementById('passage-callout')", timeout=20000)
        c.check(True, "the offer goes away once long notes are split")
        health_text = page.evaluate("[...document.getElementById('manage-health-content').childNodes]"
                                    ".filter((n) => n.nodeType === 3).map((n) => n.textContent).join('|')")
        c.check(not health_text.strip(), f"Health has no stray text (e.g. 'null'): {health_text!r}")
        c.check("searched passage by passage" in text(page, "#manage-health-content"),
                f"Health says long notes are split: {text(page, '#manage-health-content')[:300]!r}")
        stats = self.server.api("GET", "/api/stats")
        c.check(stats["chunking"] is not None and stats["passages"] > stats["documents"],
                f"passages after the rebuild: {stats['passages']} for {stats['documents']} notes")

        # 3. A pending rebuild (the setting changed) offers "Rebuild vectors" in Maintenance.
        with Index.open(db) as index:
            index.set_chunking({"max_words": 120, "overlap_words": 20})
        self._module("app.js", "m.refreshStats()")
        page.wait_for_selector("#passage-callout[data-kind='rebuild']", timeout=15000)
        c.check("Vectors need a rebuild" in text(page, "#passage-callout"), f"rebuild callout: {text(page, '#passage-callout')!r}")
        page.click(".sheet-nav [data-section='manage-maintenance']")
        page.wait_for_selector("#manage-maintenance-content button:has-text('Rebuild vectors')")
        page.click("#manage-maintenance-content button:has-text('Rebuild vectors')")
        page.wait_for_selector("#index-dialog[data-step='done']", timeout=180000)
        c.check(text(page, "#index-title") == "Vectors rebuilt", "the pending rebuild finishes")
        page.click("#index-foot button:has-text('Close')")
        page.wait_for_selector("#index-dialog", state="hidden")
        wait_js(page, "!document.getElementById('passage-callout')", timeout=20000)
        close_manage(page)

        # 4. Results and preview point at the passage further down the note.
        page.check("#mode-group input[value='semantic']", force=True)
        view = search(page, "where do the quokkas shelter on the island")
        titles = result_titles(page)
        c.check(view == "results" and bool(titles) and titles[0] == "Island field journal",
                f"meaning search finds the answer deep in the long note: {titles[:3]}")
        gap = page.evaluate("""() => {
            const head = document.getElementById('results-head').getBoundingClientRect().top;
            const modes = document.getElementById('mode-group').getBoundingClientRect().bottom;
            const between = [...document.querySelectorAll('.search-area *, #results-pane > *')]
              .filter((n) => n.getClientRects().length && n.getBoundingClientRect().top > modes
                && n.getBoundingClientRect().bottom < head && n.getBoundingClientRect().height > 8)
              .map((n) => (n.id ? '#' + n.id : n.className) + ':' + Math.round(n.getBoundingClientRect().height));
            return { space: Math.round(head - modes), between: between.slice(0, 8) };
        }""")
        c.check(gap["space"] < 120, f"no empty band above the results: {gap}")
        card = page.locator("#results-list .result-card").first
        card.locator(".why-button").click()
        reasons = " | ".join(card.locator(".why-reasons li").all_text_contents())
        c.check(re.search(r"Closest passage is further down the note \(passage \d+ of \d+\)", reasons) is not None,
                f"the explanation names the passage: {reasons!r}")
        card.locator(".why-button").click()
        card.locator("button:has-text('Preview')").click()
        page.wait_for_selector("#preview:not([hidden])")
        wait_js(page, "document.getElementById('preview-title').textContent !== 'Loading…'")
        page.wait_for_timeout(400)
        where = page.evaluate("""() => {
            const body = document.getElementById('preview-body');
            const label = document.getElementById('preview-passage-label');
            const passage = [...body.querySelectorAll('.preview-paragraph.is-passage')];
            if (!label || !passage.length) return null;
            const b = body.getBoundingClientRect(), l = label.getBoundingClientRect();
            return { label: label.textContent, paragraphs: passage.length,
                     text: passage.map((p) => p.textContent).join(' '),
                     scrolled: body.scrollTop, inView: l.bottom > b.top && l.top < b.bottom };
        }""")
        c.check(bool(where) and where["paragraphs"] > 0 and "Rottnest" in where["text"],
                f"the preview marks the matched passage: {where and where['label']} ({where and where['paragraphs']} paragraphs)")
        c.check(bool(where) and re.match(r"^Closest passage · \d+ of \d+$", where["label"]) is not None,
                f"the passage is labelled: {where and where['label']!r}")
        c.check(bool(where) and where["scrolled"] > 0 and where["inView"], f"the preview scrolls to the passage: {where and {k: where[k] for k in ('scrolled', 'inView')}}")
        c.check(not layout_problems(page), f"layout: {layout_problems(page)}")
        c.shot("29c-preview-passage-light")
        page.keyboard.press("Escape")
        page.wait_for_selector("#preview", state="hidden")
        page.locator("#search-input").focus()
        page.keyboard.press("Escape")
        wait_view(page, "idle")
        # Back to the default mode (a plain click: the app may re-render the group meanwhile).
        page.locator("#mode-group input[value='hybrid']").evaluate("(node) => node.click()")
        wait_js(page, "document.querySelector(\"#mode-group input[value='hybrid']\").checked", timeout=10000)
        self._module("views-header.js", "m.switchCollection('My Notes')")
        wait_js(page, "document.getElementById('collection-name').textContent === 'My Notes'")
        self.server.api("POST", "/api/collections/remove", {"name": "Long notes", "delete_file": True, "confirm_name": "Long notes"})
        wait_view(page, "idle")
        clear_toasts(page)

    def folder_queue(self) -> None:
        page, c = self.page, self.c
        folders = [self.elsewhere / f"queue-{number}" for number in (1, 2, 3)]
        for number, folder in enumerate(folders, 1):
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"note-{number}.md").write_text(f"# Queue note {number}\n\nKestrel sighting number {number}.\n",
                                                      encoding="utf-8")
        self.server.api("POST", "/api/collections", {"name": "Queue"})
        self._module("views-header.js", "m.switchCollection('Queue')")
        wait_js(page, "document.getElementById('collection-name').textContent === 'Queue'")
        for folder in folders:
            self._wait_job(self.server.api("POST", "/api/index", {"path": str(folder), "embed": False}))
        page.wait_for_timeout(300)

        # 1. A queued multi-folder update stops when the collection changes.
        fake = {"status": "running", "posts": 0}

        def index_route(route):
            if route.request.method == "POST" and fake["posts"] == 0:
                fake["posts"] += 1
                route.fulfill(status=202, content_type="application/json", body=json.dumps(
                    {"id": "fakequeue001", "kind": "index", "status": "running", "phase": "read",
                     "root": str(folders[0]), "done": 0, "total": 1}))
            else:
                route.continue_()

        def job_route(route):
            route.fulfill(status=200, content_type="application/json", body=json.dumps(
                {"id": "fakequeue001", "kind": "index", "status": fake["status"], "phase": "read",
                 "root": str(folders[0]), "done": 1, "total": 1, "result": {"added": 0, "total_documents": 1}}))

        page.route("**/api/index", index_route)
        page.route("**/api/jobs/fakequeue001", job_route)
        try:
            self._module("views-index.js", "m.reindex({})")
            page.wait_for_selector("#index-dialog[open][data-step='progress']")
            page.click("#index-foot button:has-text('Close')")
            page.wait_for_selector("#index-dialog", state="hidden")
            self._module("views-header.js", "m.switchCollection('My Notes')")
            wait_js(page, "document.getElementById('collection-name').textContent === 'My Notes'")
            posts_before = sum(1 for method, path in c.requests if method == "POST" and path == "/api/index")
            fake["status"] = "succeeded"
            wait_js(page, "import('/assets/views-index.js').then((m) => !m.jobRunning())", timeout=10000)
            page.wait_for_timeout(1500)
            posts_after = sum(1 for method, path in c.requests if method == "POST" and path == "/api/index")
        finally:
            page.unroute("**/api/index")
            page.unroute("**/api/jobs/fakequeue001")
        c.check(posts_after == posts_before, f"no queued folder is indexed after switching collections ({posts_after - posts_before} started)")
        roots = [item["root"] for item in self.server.api("GET", "/api/index/changes")["roots"]]
        c.check(not any("queue-" in root for root in roots), f"My Notes did not gain the other collection's folder: {roots}")
        clear_toasts(page)

        # 2. A slow home-screen refresh from before a switch does not overwrite the new lists.
        wait_view(page, "idle")
        page.evaluate("""() => {
            window.__origFetch = window.fetch;
            let delayed = false;
            window.fetch = async (url, init) => {
              const response = await window.__origFetch(url, init);
              if (!delayed && String(url).includes('/api/recent-documents')) {
                delayed = true;
                await new Promise((resolve) => setTimeout(resolve, 2000));
              }
              return response;
            };
        }""")
        try:
            page.evaluate("import('/assets/views-idle.js').then((m) => { m.refreshIdle(); })")
            page.wait_for_timeout(150)
            self._module("views-header.js", "m.switchCollection('Queue')")
            wait_js(page, "document.getElementById('collection-name').textContent === 'Queue'")
            page.wait_for_timeout(2600)
        finally:
            page.evaluate("() => { if (window.__origFetch) window.fetch = window.__origFetch; }")
        shown = [t.strip() for t in page.locator("#docs-list .list-primary").all_text_contents()]
        expected = {item["title"] for item in self.server.api("GET", "/api/recent-documents?limit=8")["items"]}
        c.check(bool(shown) and all(title in expected for title in shown),
                f"home lists show the active collection only: {shown} vs {sorted(expected)}")

        # 3. Missing folders no longer block updating the others; a moved one
        #    can be located (its notes keep their ids), a deleted one removed.
        moved = self.elsewhere / "queue-1-moved"
        shutil.move(str(folders[0]), str(moved))
        shutil.rmtree(folders[2])
        (folders[1] / "note-2b.md").write_text("# Queue note 2b\n\nA second kestrel.\n", encoding="utf-8")
        clear_toasts(page)
        open_manage(page, "health")
        wait_js(page, "document.getElementById('manage-health-content').textContent.includes('could not be found')",
                timeout=20000)
        health = page.locator("#manage-health-content")
        c.check(health.locator("button:has-text('Locate')").count() == 2
                and health.locator("button:has-text('Remove from collection')").count() == 2,
                "each missing folder can be located or removed")
        page.click("#manage-health-content button:has-text('Update index')")
        c.check(wait_toast(page, "Skipped 2 folders"), f"the skipped folders are named: {toast_text(page)!r}")
        page.wait_for_selector("#index-dialog[open]")
        page.wait_for_selector("#index-dialog[data-step='done'], #index-dialog[data-step='failed']", timeout=60000)
        c.check(page.get_attribute("#index-dialog", "data-step") == "done",
                f"the other folder is updated (step={page.get_attribute('#index-dialog', 'data-step')}, {text(page, '#index-title')!r})")
        c.check(self.done_summary().get("Added") == "1", f"update summary: {self.done_summary()}")
        page.click("#index-foot button:has-text('Close')")
        page.wait_for_selector("#index-dialog", state="hidden")
        clear_toasts(page)
        open_manage(page, "health")
        locate = "#manage-health-content .missing-root:has-text('queue-1') button:has-text('Locate')"
        page.wait_for_selector(locate, timeout=20000)
        page.click(locate)
        page.wait_for_selector("#index-dialog[open][data-step='choose']")
        c.check("new location" in text(page, "#index-title"), f"Locate asks for the new place: {text(page, '#index-title')!r}")
        wait_js(page, "!document.getElementById('fs-list').hasAttribute('aria-busy')")
        page.fill("#fs-path", str(moved))
        page.press("#fs-path", "Enter")
        page.wait_for_selector("#fs-crumbs .crumb-current:has-text('queue-1-moved')")
        page.click("#index-start")
        page.wait_for_selector("#index-dialog[data-step='done'], #index-dialog[data-step='failed']", timeout=60000)
        c.check(page.get_attribute("#index-dialog", "data-step") == "done", f"the moved folder is updated: {text(page, '#failed-message')!r}")
        c.check(self.done_summary().get("Added", "0") == "0", f"a located folder is not indexed a second time: {self.done_summary()}")
        page.click("#index-foot button:has-text('Close')")
        page.wait_for_selector("#index-dialog", state="hidden")
        ids = [item["doc_id"] for item in self.server.api("GET", "/api/recent-documents?limit=50")["items"]]
        c.check("note-1.md" in ids and not any(doc.startswith("~") for doc in ids), f"the located notes keep their ids: {ids}")
        clear_toasts(page)
        open_manage(page, "health")
        remove = "#manage-health-content .missing-root:has-text('queue-3') button:has-text('Remove from collection')"
        page.wait_for_selector(remove, timeout=20000)
        page.click(remove)
        page.wait_for_selector("#confirm-dialog[open]")
        page.click("#confirm-ok")
        wait_js(page, "document.getElementById('manage-health-content').textContent.includes('up to date')", timeout=20000)
        c.check(True, "removing the deleted folder clears the warning")
        try:
            self.server.api("GET", "/api/documents/note-3.md")
            removed = False
        except urllib.error.HTTPError as exc:
            removed = exc.code == 404
        c.check(removed, "the removed folder's notes leave the index")
        close_manage(page)
        clear_toasts(page)
        self._module("views-header.js", "m.switchCollection('My Notes')")
        wait_js(page, "document.getElementById('collection-name').textContent === 'My Notes'")
        self.server.api("POST", "/api/collections/remove", {"name": "Queue", "delete_file": True, "confirm_name": "Queue"})
        wait_view(page, "idle")
        clear_toasts(page)

    def remove_document(self) -> None:
        page, c = self.page, self.c
        doc_id = "Personal/music/jazz-turnarounds.md"
        source = self.notes / doc_id
        search(page, "jazz turnarounds")
        card = page.locator(f"#view-results .result-card[data-doc-id='{doc_id}']")
        c.check(card.count() == 1, "the note is in the results")
        card.locator(".more-button").click()
        page.wait_for_selector("#card-menu:not([hidden])")
        page.click("#card-menu .menu-item:has-text('Remove from index')")
        page.wait_for_selector("#confirm-dialog[open]")
        c.check("not touched" in text(page, "#confirm-message"), f"the confirmation says the file stays: {text(page, '#confirm-message')!r}")
        page.click("#confirm-ok")
        wait_js(page, f"!document.querySelector(\"#view-results .result-card[data-doc-id='{doc_id}']\")")
        c.check(wait_toast(page, "Removed from index"), f"removal toast: {toast_text(page)!r}")
        try:
            self.server.api("GET", "/api/documents/" + doc_id)
            gone = False
        except urllib.error.HTTPError as exc:
            gone = exc.code == 404
        c.check(gone, "the note is no longer in the index")
        c.check(source.exists(), "the file on disk is untouched")
        active = page.evaluate("document.activeElement?.closest?.('.result-card') ? 'card' : document.activeElement?.id")
        c.check(active in ("card", "search-input"), f"focus moves to a neighbour after removal ({active!r})")
        search(page, "vitamin c immune system")
        page.locator("#results-list .result-card").first.locator("button:has-text('Preview')").click()
        page.wait_for_selector("#preview-actions button:has-text('Remove from index')")
        c.check(True, "the preview offers 'Remove from index' too")
        page.keyboard.press("Escape")
        page.wait_for_selector("#preview", state="hidden")
        page.locator("#search-input").focus()
        page.keyboard.press("Escape")
        wait_view(page, "idle")
        # Updating the index adds it back, as the confirmation said.
        clear_toasts(page)
        open_manage(page, "health")
        wait_js(page, "document.getElementById('manage-health-content').textContent.includes('since the last index')",
                timeout=20000)
        page.click("#manage-health-content button:has-text('Update index')")
        page.wait_for_selector("#index-dialog[open][data-step='done']", timeout=120000)
        page.click("#index-foot button:has-text('Close')")
        page.wait_for_selector("#index-dialog", state="hidden")
        close_manage(page)
        c.check(self.server.api("GET", "/api/documents/" + doc_id).get("doc_id") == doc_id, "updating the index adds it back")
        clear_toasts(page)

    def auto_sync(self) -> None:
        page, c = self.page, self.c
        status = self.server.api("GET", "/api/autosync")
        c.check(status.get("enabled") is False, f"auto-sync was left off by the wizard: {status}")
        c.check(not visible(page, "#status-sync"), "the pill has no auto-sync note while it is off")
        open_manage(page, "health")
        page.wait_for_selector("#autosync-toggle")
        block = text(page, "#manage-autosync")
        c.check("checks your folders every few seconds" in block and "stays on this computer" in block,
                f"the switch explains itself: {block!r}")
        c.check(not page.is_checked("#autosync-toggle"), "the switch shows auto-sync off")
        page.check("#autosync-toggle")
        deadline = time.time() + 10
        while time.time() < deadline and not self.server.api("GET", "/api/autosync").get("enabled"):
            page.wait_for_timeout(100)
        c.check(self.server.api("GET", "/api/autosync").get("enabled") is True, "the switch turns auto-sync on")
        wait_js(page, "document.getElementById('autosync-status').textContent.startsWith('On')", timeout=10000)
        c.check(True, f"Manage says it is on: {text(page, '#autosync-status')!r}")
        c.shot("27-manage-autosync-light")
        close_manage(page)
        wait_js(page, "document.getElementById('status-sync').textContent.includes('Auto-sync on')", timeout=15000)
        c.check(visible(page, "#status-sync"), "the header pill says auto-sync is on")
        c.check(text(page, "#status-label") == "Ready", f"the pill still leads with Ready: {text(page, '#status-label')!r}")

        # A note written into the watched folder becomes searchable on its own.
        note = self.notes / "Personal" / "quetzal-sighting.md"
        note.write_text("# Quetzal sighting\n\nA resplendent quetzal in the cloud forest canopy.\n",
                        encoding="utf-8")
        labels: set[str] = set()
        found = False
        deadline = time.time() + 60
        while time.time() < deadline:
            labels.add(text(page, "#status-label"))
            hits = self.server.api("POST", "/api/search", {"query": "quetzal", "mode": "lexical", "k": 5})
            if any(item["doc_id"].endswith("quetzal-sighting.md") for item in hits.get("results", [])):
                found = True
                break
            page.wait_for_timeout(150)
        c.check(found, "the new note was indexed by auto-sync")
        jobs = self.server.api("GET", "/api/jobs")["jobs"]
        c.check(any(job.get("label") == "Auto-sync" and job.get("status") == "succeeded" for job in jobs),
                "an 'Auto-sync' job did the work")
        c.check(not any(label.startswith("Indexing") for label in labels),
                f"auto-sync never shows the full indexing status: {sorted(labels)}")
        wait_js(page, "document.getElementById('status-label').textContent === 'Ready'", timeout=20000)
        wait_js(page, "document.getElementById('status-sync').textContent.includes('Auto-sync on')", timeout=20000)
        c.check(True, "the pill settles back on 'Ready · Auto-sync on'")
        c.check(not page.locator("#index-dialog[open]").count(), "no indexing dialog popped up")
        c.check(not re.search(r"Index ready", toast_text(page)), f"no 'Index ready' toast: {toast_text(page)!r}")
        view = search(page, "quetzal cloud forest")
        titles = result_titles(page)
        c.check(view == "results" and "Quetzal sighting" in titles, f"the UI finds the new note: {view} {titles}")
        c.shot("28-autosync-searchable-light")
        page.locator("#search-input").focus()
        page.keyboard.press("Escape")
        wait_view(page, "idle")

        # Deleting it is picked up too; then auto-sync is turned off again.
        note.unlink()
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                self.server.api("GET", "/api/documents/Personal/quetzal-sighting.md")
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    break
            page.wait_for_timeout(150)
        else:
            c.check(False, "the deleted note left the index")
        open_manage(page, "health")
        wait_js(page, "document.getElementById('autosync-status').textContent.includes('last synced')", timeout=20000)
        c.check(True, f"Manage shows the last sync: {text(page, '#autosync-status')!r}")
        page.uncheck("#autosync-toggle")
        deadline = time.time() + 10
        while time.time() < deadline and self.server.api("GET", "/api/autosync").get("enabled"):
            page.wait_for_timeout(100)
        c.check(self.server.api("GET", "/api/autosync").get("enabled") is False, "the switch turns auto-sync off")
        close_manage(page)
        wait_js(page, "document.getElementById('status-sync').hidden", timeout=15000)
        c.check(text(page, "#status-label") == "Ready", "the pill is back to plain 'Ready'")
        clear_toasts(page)

    def theme_and_motion(self) -> None:
        page, c = self.page, self.c
        seen = []
        for _ in range(3):
            page.click("#theme-toggle")
            seen.append((page.get_attribute("html", "data-theme-pref"), page.get_attribute("html", "data-theme")))
        c.check([pref for pref, _ in seen] == ["light", "dark", "system"], f"theme cycles: {seen}")
        c.check(seen[1][1] == "dark" and seen[0][1] == "light", "light and dark apply")
        page.emulate_media(color_scheme="dark")
        page.wait_for_timeout(100)
        c.check(page.get_attribute("html", "data-theme") == "dark", "system theme follows the OS")
        page.emulate_media(color_scheme="light")
        set_theme(page, "dark")
        page.reload()
        wait_view(page, "idle")
        c.check(page.get_attribute("html", "data-theme") == "dark", "theme choice survives a reload")
        bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
        c.check(bg not in ("rgb(255, 255, 255)", "rgba(0, 0, 0, 0)"), f"dark background applied: {bg}")
        set_theme(page, "system")
        page.click("#manage-button")
        page.wait_for_selector("#manage-dialog[open]")
        page.click(".sheet-nav [data-section='manage-prefs']")
        page.check("#pref-reduce-motion")
        c.check(page.get_attribute("html", "data-motion") == "reduced", "reduce motion applies")
        page.check("#theme-pref-group input[value='dark']", force=True)
        c.check(page.get_attribute("html", "data-theme") == "dark", "theme preference radio")
        page.check("#theme-pref-group input[value='system']", force=True)
        page.click("#manage-dialog .dialog-head [data-close]")
        view = search(page, "compost")
        c.check(view == "results", "search works with reduced motion")
        anims = page.evaluate("""() => document.getAnimations().filter((a) => a.playState === 'running'
            && (a.effect?.getComputedTiming().activeDuration || 0) > 5).map((a) => a.animationName || 'anim')""")
        c.check(not anims, f"no running animations with reduced motion: {anims}")
        page.reload()
        wait_view(page, "idle")
        c.check(page.get_attribute("html", "data-motion") == "reduced", "reduce motion survives a reload")
        page.click("#manage-button")
        page.click(".sheet-nav [data-section='manage-prefs']")
        page.uncheck("#pref-reduce-motion")
        c.check(page.get_attribute("html", "data-motion") == "full", "motion restored")
        page.click("#manage-dialog .dialog-head [data-close]")

    def responsive(self) -> None:
        page, c = self.page, self.c
        for width, height in WIDTHS:
            page.set_viewport_size({"width": width, "height": height})
            page.locator("#search-input").focus()
            page.keyboard.press("Escape")
            wait_view(page, "idle")
            page.wait_for_timeout(200)
            problems = layout_problems(page)
            c.check(not problems, f"{width}px idle: {problems}")
            search(page, "vitamin c immune system")
            page.wait_for_timeout(300)
            problems = layout_problems(page)
            c.check(not problems, f"{width}px results: {problems}")
            card = page.locator("#results-list .result-card").first
            card.locator(".why-button").click()
            problems = layout_problems(page)
            c.check(not problems, f"{width}px explain: {problems}")
            card.locator(".why-button").click()
            card.locator("button:has-text('Preview')").click()
            page.wait_for_selector("#preview:not([hidden])")
            wait_js(page, "document.getElementById('preview-title').textContent !== 'Loading…'")
            page.wait_for_timeout(300)
            box = page.locator("#preview").bounding_box()
            c.check(box is not None and box["x"] >= -1 and box["x"] + box["width"] <= width + 1,
                    f"{width}px preview fits: {box}")
            problems = layout_problems(page)
            c.check(not problems, f"{width}px preview: {problems}")
            if width < 1100 and box is not None:
                # The drawer is modal: a wheel over the scrim does not scroll the page behind it.
                if box["x"] > 8:
                    point = (box["x"] / 2, height / 2)
                elif box["x"] + box["width"] < width - 8:
                    point = ((box["x"] + box["width"] + width) / 2, height / 2)
                else:
                    head = page.locator("#preview .preview-head").bounding_box() or box
                    point = (head["x"] + head["width"] / 2, head["y"] + head["height"] / 2)
                scrollable = page.evaluate("document.documentElement.scrollHeight > innerHeight + 50")
                before = page.evaluate("window.scrollY")
                page.mouse.move(*point)
                page.mouse.wheel(0, 500)
                page.wait_for_timeout(250)
                after = page.evaluate("window.scrollY")
                c.check(after == before, f"{width}px drawer keeps the page behind it still "
                                         f"(scrollY {before} -> {after}, page scrollable={scrollable})")
            page.keyboard.press("Escape")
            page.wait_for_selector("#preview", state="hidden")
            page.click("#manage-button")
            page.wait_for_selector("#manage-dialog[open]")
            page.wait_for_timeout(300)
            problems = layout_problems(page)
            c.check(not problems, f"{width}px manage: {problems}")
            sheet = page.locator("#manage-dialog").bounding_box()
            c.check(sheet is not None and sheet["width"] <= width + 1, f"{width}px manage fits: {sheet}")
            page.click("#manage-dialog .dialog-head [data-close]")
            # the index wizard (choose step)
            open_manage(page, "maintenance")
            page.click("#manage-maintenance-content button:has-text('Add a folder')")
            page.wait_for_selector("#index-dialog[open][data-step='choose']")
            page.wait_for_selector("#fs-list li")
            page.wait_for_timeout(300)
            problems = layout_problems(page)
            c.check(not problems, f"{width}px index wizard: {problems}")
            wizard = page.locator("#index-dialog").bounding_box()
            c.check(wizard is not None and wizard["x"] >= -1 and wizard["x"] + wizard["width"] <= width + 1,
                    f"{width}px wizard fits: {wizard}")
            if width <= 375:
                c.shot(f"25-wizard-{width}-light")
            page.click("#index-foot button:has-text('Cancel')")
            page.wait_for_selector("#index-dialog", state="hidden")
            close_manage(page)
        page.set_viewport_size({"width": 1280, "height": 860})

    def server_down(self) -> None:
        page, c = self.page, self.c
        search(page, "compost")
        self.server.stop()
        c.offline = True
        try:
            view = search(page, "bicycle")
            c.check(view == "error", f"stopped server shows the error state (view={view})")
            c.check("not responding" in text(page, "#error-title"), f"error title: {text(page, '#error-title')!r}")
            c.check(page.input_value("#search-input") == "bicycle", "the query is kept")
            c.check(page.locator("#error-actions button:has-text('Try again')").count() == 1, "offers to try again")
            c.shot("23-error-light")
            wait_js(page, "document.getElementById('status-label').textContent === 'Offline'", timeout=10000)
            c.check(True, "status pill shows Offline right after the failed search")
        finally:
            self.server.start()
            c.offline = False
        page.click("#error-actions button:has-text('Try again')")
        wait_js(page, "document.getElementById('results-pane').dataset.view === 'results'", timeout=30000)
        c.check("Bicycle" in " ".join(result_titles(page)), "search recovers after the server is back")
        wait_js(page, "document.getElementById('status-label').textContent === 'Ready'", timeout=20000)
        c.check(True, "status recovers")

    def final_screenshots(self) -> None:
        page, c = self.page, self.c
        for theme in ("light", "dark"):
            set_theme(page, theme)
            for width, height, label in ((1280, 860, "wide"), (375, 780, "mobile")):
                page.set_viewport_size({"width": width, "height": height})
                tag = f"{theme}-{label}"
                page.locator("#search-input").focus()
                page.keyboard.press("Escape")
                wait_view(page, "idle")
                clear_toasts(page)
                c.shot(f"30-idle-{tag}")
                search(page, "vitamin c immune system")
                c.shot(f"31-results-{tag}")
                card = page.locator("#results-list .result-card").first
                card.locator(".why-button").click()
                c.shot(f"32-explain-{tag}")
                card.locator(".why-button").click()
                card.locator("button:has-text('Preview')").click()
                wait_js(page, "document.getElementById('preview-title').textContent !== 'Loading…'")
                c.shot(f"33-preview-{tag}")
                page.keyboard.press("Escape")
                page.wait_for_selector("#preview", state="hidden")
                page.click("#filters-toggle")
                c.shot(f"34-filters-{tag}")
                page.click("#filters-toggle")
                search(page, "what is the capital of mongolia")
                c.shot(f"35-weak-{tag}")
                page.check("#mode-group input[value='lexical']", force=True)
                search(page, "zzqxv flibbertigibbet")
                c.shot(f"36-empty-{tag}")
                page.check("#mode-group input[value='hybrid']", force=True)
                page.wait_for_timeout(300)
                page.click("#manage-button")
                page.wait_for_selector("#manage-dialog[open]")
                page.wait_for_timeout(300)
                c.shot(f"37-manage-{tag}")
                page.click("#manage-dialog .dialog-head [data-close]")
                page.click("#shortcuts-button") if label == "wide" else None
                if label == "wide":
                    page.wait_for_selector("#shortcuts-dialog[open]")
                    c.shot(f"38-shortcuts-{tag}")
                    page.keyboard.press("Escape")
                page.click("#collection-button")
                page.wait_for_selector("#collection-menu:not([hidden])")
                c.shot(f"39-collection-menu-{tag}")
                page.keyboard.press("Escape")
                # wizard (choose step)
                page.click("#manage-button")
                page.click(".sheet-nav [data-section='manage-collections']")
                page.click("#manage-collections-content button:has-text('New collection')")
                page.wait_for_selector("#prompt-dialog[open]")
                c.shot(f"40-prompt-{tag}")
                page.keyboard.press("Escape")
                page.click("#manage-dialog .dialog-head [data-close]")
        set_theme(page, "system")
        page.set_viewport_size({"width": 1280, "height": 860})

    # -- release screenshot set --------------------------------------------------------

    def release_shots(self) -> None:
        """Every major state, light and dark, 1280 and 375 px wide, numbered."""
        page, c = self.page, self.c
        out: Path = self.args.release
        out.mkdir(parents=True, exist_ok=True)
        for old in out.glob("*.png"):
            old.unlink()

        def shot(name: str) -> None:
            page.wait_for_timeout(250)
            path = out / f"{name}.png"
            page.screenshot(path=str(path), animations="disabled")
            c.shots.append(path)

        def idle() -> None:
            if page.locator("#preview:not([hidden])").count():
                page.keyboard.press("Escape")
            page.locator("#search-input").focus()
            page.keyboard.press("Escape")
            wait_view(page, "idle")
            clear_toasts(page)

        def switch_to(name: str) -> None:
            self.server.api("POST", "/api/collections/active", {"name": name})
            page.reload()
            wait_js(page, f"document.getElementById('collection-name').textContent === {json.dumps(name)}")

        def settled() -> None:
            wait_js(page, "!document.getElementById('results-pane').classList.contains('is-refreshing')", timeout=30000)

        combos = [(theme, width, height) for theme in ("light", "dark") for width, height in ((1280, 860), (375, 780))]
        for index, (theme, width, height) in enumerate(combos):
            tag = f"{theme}-{width}"
            page.set_viewport_size({"width": width, "height": height})
            set_theme(page, theme)
            # 01-03: a fresh collection: welcome, folder picker, indexing progress.
            fresh = f"Fresh {index + 1}"
            self.server.api("POST", "/api/collections", {"name": fresh})
            switch_to(fresh)
            wait_view(page, "onboarding")
            clear_toasts(page)
            shot(f"01-onboarding-{tag}")
            page.click("#onboard-index")
            page.wait_for_selector("#index-dialog[open][data-step='choose']")
            wait_js(page, "!document.getElementById('fs-list').hasAttribute('aria-busy')")  # the first listing is in
            page.fill("#fs-path", str(self.bulk))
            page.press("#fs-path", "Enter")
            page.wait_for_selector("#fs-crumbs .crumb-current:has-text('bulk')")
            shot(f"02-index-folder-{tag}")
            page.click("#index-start")
            page.wait_for_selector("#index-dialog[data-step='progress']", timeout=20000)
            wait_js(page, "document.querySelector('#job-steps [data-status=\"active\"]') !== null", timeout=20000)
            page.wait_for_timeout(900)
            shot(f"03-index-progress-{tag}")
            if page.locator("#index-foot button:has-text('Cancel indexing')").count():
                page.click("#index-foot button:has-text('Cancel indexing')")
            page.wait_for_selector("#index-dialog[data-step='failed'], #index-dialog[data-step='done']", timeout=60000)
            page.click("#index-foot button:has-text('Close')")
            page.wait_for_selector("#index-dialog", state="hidden")
            wait_js(page, "!document.getElementById('status-label').textContent.startsWith('Indexing')", timeout=30000)
            switch_to("My Notes")
            self.server.api("POST", "/api/collections/remove", {"name": fresh, "delete_file": True, "confirm_name": fresh})
            # 04: ready home screen.
            idle()
            shot(f"04-ready-{tag}")
            # 05: loading stages, captured while the search request is held back.
            loading_name = f"05-loading-{tag}"

            def hold(route, *_args) -> None:
                page.wait_for_timeout(700)
                shot(loading_name)
                route.continue_()

            page.route("**/api/search/stream", hold)
            try:
                page.fill("#search-input", "how do plants turn sunlight into energy")
                page.press("#search-input", "Enter")
                page.wait_for_selector("#view-loading:not([hidden])", timeout=5000)
            finally:
                wait_js(page, "document.getElementById('results-pane').dataset.view === 'results'", timeout=30000)
                page.unroute("**/api/search/stream")
            # 06-08: results, explanation, preview.
            search(page, "vitamin c immune system")
            settled()
            shot(f"06-results-{tag}")
            card = page.locator("#results-list .result-card").first
            card.locator(".why-button").click()
            shot(f"07-explain-{tag}")
            card.locator(".why-button").click()
            card.locator("button:has-text('Preview')").click()
            wait_js(page, "document.getElementById('preview-title').textContent !== 'Loading…'")
            page.wait_for_timeout(300)
            shot(f"08-preview-{tag}")
            page.keyboard.press("Escape")
            page.wait_for_selector("#preview", state="hidden")
            # 09: filters open with one active.
            search(page, "policy")
            settled()
            page.click("#filters-toggle")
            work = page.evaluate("""() => [...document.querySelectorAll('#filter-folder option')]
                .find((o) => o.textContent.startsWith('Work'))?.value || ''""")
            page.select_option("#filter-folder", work)
            page.wait_for_selector("#filter-chips .chip-item")
            settled()
            shot(f"09-filters-{tag}")
            page.click("#filters-clear")
            settled()
            page.click("#filters-toggle")
            # 10-11: no strong matches, no results.
            search(page, "what is the capital of mongolia")
            settled()
            shot(f"10-weak-{tag}")
            page.check("#mode-group input[value='lexical']", force=True)
            settled()
            search(page, "zzqxv flibbertigibbet")
            shot(f"11-no-results-{tag}")
            page.check("#mode-group input[value='hybrid']", force=True)
            page.wait_for_timeout(400)
            # 12: the service stops answering.
            c.offline = True
            for pattern in ("**/api/search/stream", "**/api/search", "**/api/stats"):
                page.route(pattern, lambda route: route.abort("connectionrefused"))
            try:
                view = search(page, "bicycle")
                c.check(view == "error", f"release shot: offline error state (view={view})")
                page.wait_for_timeout(300)
                shot(f"12-error-{tag}")
            finally:
                for pattern in ("**/api/search/stream", "**/api/search", "**/api/stats"):
                    page.unroute(pattern)
                c.offline = False
            page.click("#error-actions button:has-text('Try again')")
            wait_js(page, "document.getElementById('results-pane').dataset.view === 'results'", timeout=30000)
            wait_js(page, "document.getElementById('status-label').textContent === 'Ready'", timeout=30000)
            idle()
            # 13-15: manage panel, collections menu, shortcuts.
            page.click("#manage-button")
            page.wait_for_selector("#manage-dialog[open]")
            shot(f"13-manage-{tag}")
            page.click("#manage-dialog .dialog-head [data-close]")
            page.wait_for_selector("#manage-dialog", state="hidden")
            page.click("#collection-button")
            page.wait_for_selector("#collection-menu:not([hidden])")
            shot(f"14-collections-menu-{tag}")
            page.keyboard.press("Escape")
            page.locator("body").click(position={"x": 4, "y": height - 4})
            page.keyboard.press("?")
            page.wait_for_selector("#shortcuts-dialog[open]")
            shot(f"15-shortcuts-{tag}")
            page.keyboard.press("Escape")
            page.wait_for_selector("#shortcuts-dialog", state="hidden")
        set_theme(page, "system")
        page.set_viewport_size({"width": 1280, "height": 860})


def search_hidden_check(page: Page) -> bool:
    return bool(page.locator("#manage-dialog[open]").count())


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    walkthrough = Walkthrough(args)
    ok = walkthrough.run()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
