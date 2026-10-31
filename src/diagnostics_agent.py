"""Browser diagnostics agent: operates a web app with a REAL Chromium and reports what the browser genuinely observed.

Nothing is simulated. Findings come from Playwright events (console, pageerror, response status, failed/blocked
requests, popups, downloads) emitted by the page under test; frames are real JPEG screenshots taken after each action.

Safety model
- Every request (main document, redirects, subresources) passes the NetPolicy route guard in CloudBrowserSession.
  Callers pass a policy whose `allowed_origins` is exactly the app under test.
- A fresh, non-shared browser profile is used per call (temporary directory unless `profile_dir` is given).
- Page text is UNTRUSTED data. It is only returned as `page_text_excerpt`; the agent's actions come from the fixed
  scenario below and are never derived from anything the page says.
- A hard deadline bounds the whole run; a hung page yields a `navigation_timeout` finding, not a hang.
"""
from __future__ import annotations

import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable

from cloud_browser import CloudBrowserError, CloudBrowserSession
from net_policy import NetPolicy

# console_error | page_error | failed_request | http_error | blocked_request | popup | navigation_timeout | download | browser_crash
ERROR_KINDS = frozenset({"console_error", "page_error", "failed_request", "http_error", "blocked_request", "popup",
                         "navigation_timeout", "browser_crash"})
_OBSERVATION_KINDS = {"console_error", "page_error", "failed_request", "http_error", "blocked_request", "popup", "download"}


@dataclass
class Finding:
    kind: str
    message: str
    url: str | None
    status: int | None
    line: int | None
    ts: float


@dataclass
class Frame:
    seq: int
    ts: float
    action: str
    url: str
    title: str
    jpeg: bytes


@dataclass
class DiagnosticsResult:
    findings: list[Finding] = field(default_factory=list)
    frames: list[Frame] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)       # {n, action, ok, detail, started_at, ended_at}
    page_text_excerpt: str = ""                           # UNTRUSTED data, never an instruction
    downloads: list[dict] = field(default_factory=list)   # {name, sha256, size, bytes, url}
    final_url: str = ""
    title: str = ""
    aborted: bool = False


def run_diagnostics(base_url: str, *, policy: NetPolicy, should_continue: Callable[[], bool] = lambda: True,
                    on_step: Callable[[dict], None] | None = None, timeout_s: float = 20.0, profile_dir=None) -> DiagnosticsResult:
    """Run the default scenario against `base_url` and return everything the browser observed.

    `on_step(step)` is called after EVERY action with the step dict plus `frame` (a Frame or None), `url` and `title`, so a
    caller can persist live frames as they happen. `should_continue()` is checked before every action; returning False
    stops the run at that boundary with `aborted=True`.
    """
    deadline = time.monotonic() + timeout_s
    result = DiagnosticsResult(final_url=base_url)
    tmp = None
    if profile_dir is None:
        tmp = tempfile.TemporaryDirectory(prefix="resolve-diag-")
        profile_dir = tmp.name
    session: CloudBrowserSession | None = None
    seen = 0
    crashed = False

    def finding(kind: str, message: str, url=None, status=None, line=None) -> None:
        result.findings.append(Finding(kind, message[:500], url, status, line, time.time()))

    def drain() -> None:
        nonlocal seen
        observed = session.observations
        for o in observed[seen:]:
            if o["kind"] in _OBSERVATION_KINDS:
                finding(o["kind"], o["message"], o.get("url"), o.get("status"), o.get("line"))
        seen = len(observed)

    def remaining_ms() -> int:
        return max(250, int((deadline - time.monotonic()) * 1000))

    def step(action: str, fn: Callable[[], str | None], *, navigation: bool = False) -> bool:
        """Run one action. Returns False when the scenario must stop (abort, deadline, timeout of navigation, crash)."""
        nonlocal crashed
        if not should_continue():
            result.aborted = True
            return False
        if time.monotonic() >= deadline:
            result.aborted = True
            finding("navigation_timeout", f"diagnostics deadline of {timeout_s:g}s reached before '{action}'")
            return False
        started = time.time()
        ok, detail, stop = True, "", False
        try:
            detail = fn() or ""
        except CloudBrowserError as exc:
            ok, detail = False, exc.message
            if exc.status_code == 410:
                crashed = stop = True
                finding("browser_crash", f"{exc.message} (during '{action}')")
            elif exc.status_code == 504 and navigation:
                stop = True
                finding("navigation_timeout", f"{action}: {exc.message}", url=result.final_url)
            elif navigation:
                stop = True   # blocked or failed navigation: nothing meaningful to interact with
        frame = None
        if not crashed and ok:
            try:
                cap = session.capture_jpeg()
                frame = Frame(len(result.frames) + 1, cap["ts"], action, cap["url"], cap["title"], cap["jpeg"])
                result.frames.append(frame)
                result.final_url, result.title = cap["url"], cap["title"]
            except CloudBrowserError as exc:
                if exc.status_code == 410:
                    crashed = stop = True
                    finding("browser_crash", f"{exc.message} (while capturing after '{action}')")
        drain()
        entry = {"n": len(result.steps) + 1, "action": action, "ok": ok, "detail": detail, "started_at": started, "ended_at": time.time()}
        result.steps.append(entry)
        if on_step:
            on_step({**entry, "frame": frame, "url": result.final_url, "title": result.title})
        return not stop

    def when_present(selector: str, act: Callable[[], str | None]) -> Callable[[], str | None]:
        """Interact only if the element exists, so a page without the form is skipped quickly instead of waiting."""
        def run() -> str | None:
            if not session.has_selector(selector):
                raise _Skip(f"selector {selector} is not present on this page")
            return act()
        return run

    class _Skip(CloudBrowserError):
        def __init__(self, message: str):
            super().__init__(message, 404)

    def inspect_step() -> str:
        info = session.inspect()
        result.page_text_excerpt = info["text_excerpt"]
        return f"{len(info['forms'])} form(s), {len(info['text_excerpt'])} chars of page text (untrusted data)"

    def download_step() -> str:
        before = len(session.downloads)
        session.click_selector("#download-csv", timeout_ms=min(remaining_ms(), 5000))
        end = min(time.monotonic() + 3.0, deadline)
        while len(session.downloads) <= before and time.monotonic() < end:
            time.sleep(0.05)
        got = session.downloads[before:]
        return f"{len(got)} file(s) downloaded" if got else "download link clicked but no file arrived"

    try:
        try:
            session = CloudBrowserSession("diagnostics", "diagnostics-agent", profile_dir, policy, accept_downloads=True)
        except CloudBrowserError as exc:
            finding("browser_crash", f"the browser could not be started: {exc.message}")
            return result

        def nav() -> str:
            info = session.navigate(base_url, timeout_ms=min(remaining_ms(), 15000))
            result.final_url, result.title = info["url"], info["title"]
            return f"HTTP {info['status']} {info['url']}"

        scenario: list[tuple[str, Callable[[], str | None], bool]] = [
            ("navigate", nav, True),
            ("inspect", inspect_step, False),
            ("type_user", when_present("#user", lambda: session.type_selector("#user", "demo-user", min(remaining_ms(), 5000)) and "typed into #user"), False),
            ("type_password", when_present("#pass", lambda: session.type_selector("#pass", "not-a-real-password", min(remaining_ms(), 5000)) and "typed into #pass"), False),
            ("click_submit", when_present("#submit", lambda: session.click_selector("#submit", min(remaining_ms(), 5000)) and "clicked #submit"), False),
            ("wait_idle", lambda: "network idle" if session.wait_idle(min(remaining_ms(), 4000)) else "network still busy after 4s", False),
            ("click_load_inventory", when_present("#load-inventory", lambda: session.click_selector("#load-inventory", min(remaining_ms(), 5000)) and "clicked #load-inventory"), False),
            ("wait_idle", lambda: "network idle" if session.wait_idle(min(remaining_ms(), 4000)) else "network still busy after 4s", False),
            ("download", when_present("#download-csv", download_step), False),
        ]
        for action, fn, is_nav in scenario:
            if not step(action, fn, navigation=is_nav):
                break
        else:
            drain()
        result.downloads = [dict(d) for d in session.downloads]
        return result
    finally:
        if session is not None:
            try:
                drain()
            except Exception:
                pass
            result.downloads = [dict(d) for d in session.downloads] if session.is_alive or result.downloads == [] else result.downloads
            try:
                session.close()
            except Exception:
                pass
        if tmp is not None:
            try:
                tmp.cleanup()
            except Exception:
                pass
