"""Comprehensive local browser checks and screenshot capture for OneFind.

Uses Playwright with an already-installed Chrome/Chromium binary, so it does
not download a browser or require any paid service.

Usage from the repository root:
    python tools/browser_check.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import Browser, Page, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "sshot"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--indexed-db", type=Path, default=ROOT / "demo.db")
    parser.add_argument(
        "--lexical-db", type=Path, default=ROOT / "data" / "ui-lexical-only.db"
    )
    parser.add_argument(
        "--xss-db", type=Path, default=ROOT / "data" / "ui-xss.db"
    )
    parser.add_argument(
        "--showcase-db", type=Path, default=ROOT / "data" / "showcase.db"
    )
    parser.add_argument(
        "--missing-db", type=Path, default=ROOT / "data" / "ui-no-index.db"
    )
    parser.add_argument("--port-base", type=int, default=8090)
    parser.add_argument(
        "--chrome",
        default=os.environ.get("ONEFIND_CHROME"),
        help="Chrome/Chromium executable; auto-detected when omitted",
    )
    return parser.parse_args()


def detect_chrome(explicit: str | None) -> str:
    candidates = [
        explicit,
        os.environ.get("PROGRAMFILES"),
        os.environ.get("PROGRAMFILES(X86)"),
        os.environ.get("LOCALAPPDATA"),
    ]
    paths = []
    if candidates[0]:
        paths.append(Path(candidates[0]))
    if len(candidates) > 1 and candidates[1]:
        paths.extend(
            [
                Path(candidates[1]) / "Microsoft/Edge/Application/msedge.exe",
                Path(candidates[1]) / "Google/Chrome/Application/chrome.exe",
            ]
        )
    if len(candidates) > 2 and candidates[2]:
        paths.extend(
            [
                Path(candidates[2]) / "Google/Chrome/Application/chrome.exe",
                Path(candidates[2]) / "Microsoft/Edge/Application/msedge.exe",
            ]
        )
    for command in ("chrome", "google-chrome", "chromium", "chromium-browser", "msedge"):
        found = shutil.which(command)
        if found:
            return found
    for path in paths:
        if path.is_file():
            return str(path)
    raise RuntimeError("Chrome/Chromium not found; set ONEFIND_CHROME")


def remove_sqlite(path: Path) -> None:
    for candidate in (
        path,
        Path(str(path) + "-wal"),
        Path(str(path) + "-shm"),
    ):
        candidate.unlink(missing_ok=True)


def prepare_fixtures(args: argparse.Namespace) -> None:
    from onefind.ingest import ingest_path
    from onefind.store import Document, Index

    args.output.mkdir(parents=True, exist_ok=True)
    for old in args.output.glob("*.png"):
        old.unlink()

    if not args.indexed_db.exists():
        subprocess.run(
            [
                sys.executable,
                "-m",
                "onefind.cli",
                "index",
                str(ROOT / "sample-data"),
                "--db",
                str(args.indexed_db),
                "--embed",
            ],
            cwd=ROOT,
            check=True,
        )

    remove_sqlite(args.showcase_db)
    args.showcase_db.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "onefind.cli",
            "index",
            str(ROOT / "showcase-data"),
            "--db",
            str(args.showcase_db),
            "--embed",
        ],
        cwd=ROOT,
        check=True,
    )

    remove_sqlite(args.lexical_db)
    args.lexical_db.parent.mkdir(parents=True, exist_ok=True)
    with Index.open(args.lexical_db) as index:
        ingest_path(index, ROOT / "sample-data")

    remove_sqlite(args.xss_db)
    with Index.open(args.xss_db) as index:
        index.add_documents(
            [
                Document(
                    "unsafe.md",
                    '<img src=x onerror="alert(1)"> Unsafe title',
                    'trigger <img src=x onerror="alert(1)"> <script>alert(2)</script>',
                    source="security-test.md",
                )
            ]
        )

    remove_sqlite(args.missing_db)


def start_services(args: argparse.Namespace) -> list[tuple[subprocess.Popen, int, Path]]:
    services = [
        (args.showcase_db, args.port_base),
        (args.lexical_db, args.port_base + 1),
        (args.missing_db, args.port_base + 2),
        (args.xss_db, args.port_base + 3),
    ]
    processes: list[tuple[subprocess.Popen, int, Path]] = []
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    for db_path, port in services:
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-m",
                "onefind.cli",
                "serve",
                "--db",
                str(db_path),
                "--port",
                str(port),
            ],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        processes.append((process, port, db_path))
    return processes


def wait_for_services(processes, timeout: float = 45.0) -> None:
    deadline = time.monotonic() + timeout
    for process, port, _db in processes:
        url = f"http://127.0.0.1:{port}/api/stats"
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"service on port {port} exited early")
            try:
                with urllib.request.urlopen(url, timeout=1) as response:
                    if response.status == 200:
                        break
            except Exception:
                time.sleep(0.2)
        else:
            raise RuntimeError(f"service on port {port} did not become ready")


def stop_services(processes) -> None:
    for process, _port, _db in processes:
        if process.poll() is None:
            process.terminate()
    deadline = time.monotonic() + 8
    for process, _port, _db in processes:
        remaining = max(0.1, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            process.kill()


def new_page(browser: Browser, *, width=1440, height=1000, dark_os=False, mobile=False):
    context = browser.new_context(
        viewport={"width": width, "height": height},
        color_scheme="dark" if dark_os else "light",
        is_mobile=mobile,
        has_touch=mobile,
        device_scale_factor=1,
    )
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(f"pageerror: {error}"))
    page.on(
        "console",
        lambda message: errors.append(f"console.{message.type}: {message.text}")
        if message.type == "error"
        else None,
    )
    return context, page, errors


def wait_ready(page: Page, url: str, expected: str) -> None:
    page.goto(url, wait_until="domcontentloaded")
    page.locator("#service-status").wait_for(state="visible")
    page.get_by_text(expected, exact=False).first.wait_for(state="visible")


def wait_results(page: Page, timeout=120_000) -> None:
    page.locator("#results-section").wait_for(state="visible")
    page.locator('#results-section[aria-busy="false"]').wait_for(
        state="visible", timeout=timeout
    )
    page.locator(
        "#results-list:visible, #empty-state:visible, #error-state:visible"
    ).first.wait_for(state="visible", timeout=timeout)


def select_mode(page: Page, mode: str) -> None:
    page.locator(f'input[name="mode"][value="{mode}"]').check(force=True)


def select_precision(page: Page, precision: str) -> None:
    open_technical(page)
    page.locator(f'input[name="precision"][value="{precision}"]').check(force=True)


def open_technical(page: Page) -> None:
    panel = page.locator("#advanced-settings")
    if panel.count() == 1 and not panel.evaluate("(element) => element.open"):
        panel.locator("summary").click()


def run_search(
    page: Page,
    query: str,
    mode: str,
    precision: str = "float",
    k: int = 10,
    expected_text: str | None = None,
) -> None:
    select_mode(page, mode)
    if mode != "lexical":
        select_precision(page, precision)
    open_technical(page)
    page.locator("#result-count").select_option(str(k))
    page.locator("#search-input").fill(query)
    page.locator("#search-button").click()
    wait_results(page)
    if expected_text:
        page.locator("#results-list").get_by_text(expected_text, exact=False).first.wait_for()


def capture(page: Page, output: Path, name: str, manifest: list[dict], full=True) -> None:
    path = output / name
    page.screenshot(path=str(path), full_page=full, animations="disabled")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest.append(
        {"screenshot": name, "bytes": path.stat().st_size, "sha256": digest}
    )


def run_checks(browser: Browser, args: argparse.Namespace) -> dict:
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    base = args.port_base
    indexed = f"http://127.0.0.1:{base}"
    showcase = f"http://127.0.0.1:{base}"
    lexical = f"http://127.0.0.1:{base + 1}"
    missing = f"http://127.0.0.1:{base + 2}"
    xss = f"http://127.0.0.1:{base + 3}"
    manifest: list[dict] = []
    browser_errors: dict[str, list[str]] = {}
    assertions: list[dict] = []

    def passed(name: str, detail: str = "") -> None:
        assertions.append({"check": name, "status": "passed", "detail": detail})

    # 1. Indexed idle + capability controls.
    context, page, errors = new_page(browser)
    response = page.goto(indexed, wait_until="domcontentloaded")
    page.get_by_text("Index ready", exact=False).first.wait_for(state="visible")
    assert response is not None
    csp = response.headers.get("content-security-policy", "")
    assert "script-src 'self'" in csp
    assert response.headers.get("x-frame-options") == "DENY"
    assert page.locator('input[name="mode"][value="semantic"]').is_enabled()
    assert page.locator('input[name="mode"][value="hybrid"]').is_enabled()
    assert page.locator('input[name="precision"][value="int8"]').is_enabled()
    assert page.locator('input[name="mode"][value="hybrid"]').is_checked()
    assert not page.locator("#advanced-settings").evaluate("element => element.open")
    open_technical(page)
    assert page.locator("#advanced-settings").evaluate("element => element.open")
    assert page.locator("#result-count").is_visible()
    capture(page, output, "02a-technical-settings-open.png", manifest)
    page.locator("#advanced-settings summary").click()
    assert not page.locator("#advanced-settings").evaluate("element => element.open")
    capture(page, output, "01-indexed-idle.png", manifest)
    passed("indexed idle, hybrid default, and tucked technical settings", csp)
    browser_errors["01-indexed-idle"] = errors

    # 2. First semantic request: actual lazy model loading state.
    select_mode(page, "semantic")
    select_precision(page, "float")
    page.locator("#search-input").fill("How should our team handle rollback with clear ownership and evidence?")
    page.locator("#search-button").click()
    page.locator("#loading-state").wait_for(state="visible", timeout=10_000)
    page.wait_for_timeout(250)
    assert page.locator("#results-section").get_attribute("aria-busy") == "true"
    capture(page, output, "02-first-semantic-loading.png", manifest)
    wait_results(page, timeout=180_000)
    page.locator("#results-list").get_by_text("rollback", exact=False).first.wait_for()
    capture(page, output, "03-semantic-float-results.png", manifest)
    passed("first semantic lazy-load and float result")
    browser_errors["02-first-semantic-loading"] = errors
    context.close()

    # 3. Every retrieval mode and precision exposed by the UI.
    combinations = [
        ("keyword", "lexical", None, "PROD-001 feature flags", "prod-001", "04-keyword-results.png"),
        ("semantic-int8", "semantic", "int8", "How should our team handle rollback with clear ownership and evidence?", "rollback", "05-semantic-int8-results.png"),
        ("semantic-binary", "semantic", "binary", "How do we safely handle hostile HTML content?", "inert html", "06-semantic-binary-results.png"),
        ("hybrid-float", "hybrid", "float", "CSP remote binding reset safety", "content security policy", "07-hybrid-float-results.png"),
        ("hybrid-int8", "hybrid", "int8", "How are int8 vectors evaluated?", "int8 quantization", "08-hybrid-int8-results.png"),
        ("hybrid-binary", "hybrid", "binary", "How is a failed deployment rolled back?", "rollback procedure", "09-hybrid-binary-results.png"),
    ]
    context, page, errors = new_page(browser)
    for label, mode, precision, query, expected, filename in combinations:
        wait_ready(page, indexed, "Index ready")
        run_search(page, query, mode, precision or "float", 10, expected)
        capture(page, output, filename, manifest)
        passed(label, f"expected result contains {expected}")
    browser_errors["retrieval-combinations"] = errors
    context.close()

    # 4. No-result state.
    context, page, errors = new_page(browser)
    wait_ready(page, indexed, "Index ready")
    run_search(page, "asdfqwer nonexistentterm", "lexical", k=10)
    assert page.locator("#empty-state").is_visible()
    capture(page, output, "10-no-results.png", manifest)
    passed("no-result state")
    browser_errors["10-no-results"] = errors
    context.close()

    # 5. All result-count controls, with extreme layouts captured.
    context, page, errors = new_page(browser)
    wait_ready(page, indexed, "Index ready")
    for k in (5, 10, 20, 30):
        run_search(page, "PROD-001 feature flags", "lexical", k=k)
        assert page.locator("#result-count").input_value() == str(k)
    run_search(page, "PROD-001 feature flags", "lexical", k=5)
    capture(page, output, "11-results-k5.png", manifest)
    run_search(page, "PROD-001 feature flags", "lexical", k=30)
    capture(page, output, "12-results-k30.png", manifest)
    passed("result count controls", "5, 10, 20, 30")
    browser_errors["result-counts"] = errors

    # 6. Shareable URL state.
    page.goto(
        showcase
        + "/?q=How%20is%20a%20failed%20deployment%20rolled%20back%3F&mode=hybrid&precision=binary&k=5",
        wait_until="domcontentloaded",
    )
    wait_results(page)
    assert page.locator("#search-input").input_value().startswith("How is a failed")
    assert page.locator('input[name="mode"][value="hybrid"]').is_checked()
    assert page.locator('input[name="precision"][value="binary"]').is_checked()
    capture(page, output, "13-shareable-url-state.png", manifest)
    passed("shareable URL state")
    browser_errors["shareable-url"] = errors
    context.close()

    # 7. Light-only UI under a dark operating-system preference.
    context, page, errors = new_page(browser, dark_os=True)
    wait_ready(page, showcase, "Index ready")
    run_search(page, "How is a failed deployment rolled back?", "hybrid", "float", 10, "rollback")
    background = page.locator("body").evaluate("element => getComputedStyle(element).backgroundColor")
    assert background == "rgb(248, 247, 245)"
    capture(page, output, "14-dark-os-light-ui.png", manifest)
    passed("light-only UI under dark OS preference", background)
    browser_errors["dark-os-light-ui"] = errors
    context.close()

    # 8. Mobile responsive layout.
    context, page, errors = new_page(browser, width=430, height=932, mobile=True)
    wait_ready(page, showcase, "Index ready")
    run_search(page, "PROD-001 feature flags", "hybrid", "float", 5, "prod-001")
    assert page.locator("#results-list .result-card").count() > 0
    capture(page, output, "15-mobile-hybrid-results.png", manifest)
    passed("mobile responsive layout", "430x932 touch viewport")
    browser_errors["mobile"] = errors
    context.close()

    # 9. Lexical-only capability state and valid keyword search.
    context, page, errors = new_page(browser)
    wait_ready(page, lexical, "Index ready")
    assert page.locator('input[name="mode"][value="semantic"]').is_disabled()
    assert page.locator('input[name="mode"][value="hybrid"]').is_disabled()
    assert page.locator('input[name="precision"][value="float"]').is_disabled()
    capture(page, output, "16-lexical-only-capabilities.png", manifest)
    run_search(page, "chlorophyll", "lexical", k=10, expected_text="photosynthesis.md")
    capture(page, output, "17-lexical-only-results.png", manifest)
    passed("lexical-only capability restrictions and search")
    browser_errors["lexical-only"] = errors
    context.close()

    # 10. No-index state.
    context, page, errors = new_page(browser)
    wait_ready(page, missing, "No index")
    assert page.locator("#no-index-state").is_visible()
    assert page.locator("#search-input").is_disabled()
    assert page.locator("#search-button").is_disabled()
    capture(page, output, "18-no-index.png", manifest)
    passed("no-index state and disabled controls")
    browser_errors["no-index"] = errors
    context.close()

    # 11. Network failure after a valid page load.
    context, page, errors = new_page(browser)
    wait_ready(page, showcase, "Index ready")
    context.set_offline(True)
    page.locator("#search-input").fill("PROD-001 feature flags")
    page.locator("#search-button").click()
    page.locator("#error-state").wait_for(state="visible", timeout=15_000)
    capture(page, output, "19-network-error.png", manifest)
    passed("network failure error state")
    browser_errors["network-error"] = errors
    context.close()

    # 12. API validation error caused by an overlong query.
    context, page, errors = new_page(browser)
    wait_ready(page, indexed, "Index ready")
    page.locator("#search-input").evaluate(
        "(element, value) => { element.removeAttribute('maxlength'); element.value = value; }",
        "x" * 513,
    )
    page.locator("#search-form").evaluate("form => form.requestSubmit()")
    page.locator("#error-state").wait_for(state="visible", timeout=15_000)
    assert page.locator("#error-message").text_content()
    capture(page, output, "20-validation-error.png", manifest)
    passed("422 validation error state")
    browser_errors["validation-error"] = errors
    context.close()

    # 13. Untrusted HTML remains inert text.
    context, page, errors = new_page(browser)
    wait_ready(page, xss, "Index ready")
    run_search(page, "trigger", "lexical", k=10)
    result_text = page.locator("#results-list").inner_text()
    assert "<img" in result_text and "onerror" in result_text
    assert page.locator('#results-list img[src="x"]').count() == 0
    assert page.locator("#results-list script").count() == 0
    capture(page, output, "21-xss-rendered-as-text.png", manifest)
    passed("untrusted HTML rendered inert", "no executable img/script nodes")
    browser_errors["xss-inert"] = errors
    context.close()

    # 14. Keyboard focus and clear shortcuts on a fresh page.
    context, page, errors = new_page(browser)
    wait_ready(page, showcase, "Index ready")
    page.locator("body").press("/")
    assert page.locator("#search-input").evaluate("element => element === document.activeElement")
    page.locator("#search-input").fill("shortcut clear")
    page.keyboard.press("Escape")
    assert page.locator("#search-input").input_value() == ""
    assert page.locator("#idle-state").is_visible()
    capture(page, output, "22-keyboard-shortcut-clear.png", manifest)
    passed("keyboard shortcut and clear state", "/ focuses and Escape clears")
    browser_errors["keyboard-shortcuts"] = errors
    context.close()

    # 15. Extreme narrow responsive layout and gallery presence.
    context, page, errors = new_page(browser, width=320, height=760, mobile=True)
    wait_ready(page, showcase, "Index ready")
    assert page.locator("#search-input").is_visible()
    assert page.locator("#memories").is_visible()
    page.locator("#memories").scroll_into_view_if_needed()
    capture(page, output, "23-narrow-gallery.png", manifest)
    passed("320px narrow responsive gallery", "320x760 touch viewport")
    browser_errors["narrow"] = errors
    context.close()

    # 16. Reduced motion remains usable.
    context, page, errors = new_page(browser)
    context.set_default_timeout(10_000)
    page.emulate_media(reduced_motion="reduce")
    wait_ready(page, showcase, "Index ready")
    run_search(page, "PROD-001 feature flags", "hybrid", "float", 5, "prod-001")
    capture(page, output, "24-reduced-motion-results.png", manifest)
    passed("reduced motion results", "motion is minimized")
    browser_errors["reduced-motion"] = errors
    context.close()

    expected_console_tokens = {
        "network-error": ("ERR_INTERNET_DISCONNECTED",),
        "validation-error": ("422",),
    }
    unexpected_errors: dict[str, list[str]] = {}
    expected_errors: dict[str, list[str]] = {}
    for scenario, messages in browser_errors.items():
        if not messages:
            continue
        allowed = expected_console_tokens.get(scenario, ())
        unexpected = [message for message in messages if not any(token in message for token in allowed)]
        expected = [message for message in messages if any(token in message for token in allowed)]
        if unexpected:
            unexpected_errors[scenario] = unexpected
        if expected:
            expected_errors[scenario] = expected
    assert not unexpected_errors, unexpected_errors

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "browser": "Chrome/Chromium via Playwright",
        "base_urls": {
            "showcase": showcase,
            "lexical_only": lexical,
            "no_index": missing,
            "xss_fixture": xss,
        },
        "screenshot_count": len(manifest),
        "screenshots": manifest,
        "assertions": assertions,
        "browser_console_errors": unexpected_errors,
        "expected_browser_console_errors": expected_errors,
        "status": "passed",
    }


def main() -> int:
    args = parse_args()
    args.output = args.output.resolve()
    chrome = detect_chrome(args.chrome)
    prepare_fixtures(args)
    processes = start_services(args)
    try:
        wait_for_services(processes)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=chrome,
                headless=True,
                args=["--disable-gpu", "--no-first-run"],
            )
            try:
                report = run_checks(browser, args)
            finally:
                browser.close()
    finally:
        stop_services(processes)

    report["chrome_executable"] = chrome
    manifest_path = args.output / "manifest.json"
    manifest_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"browser checks: {report['status']}")
    print(f"screenshots: {report['screenshot_count']} -> {args.output}")
    print(f"manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
