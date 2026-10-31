"""Isolated cloud browser runtime using Playwright for RESOLVE multi-tenant sandboxes."""
from __future__ import annotations

import base64
import hashlib
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path
from urllib.parse import urljoin

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # Playwright is optional: production ships without a browser
    from playwright.sync_api import BrowserContext, Page, Playwright

from net_policy import NetPolicy, PolicyViolation

ROOT = Path(__file__).resolve().parents[1]
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024     # downloads are kept in memory only; larger ones are recorded but not read
MAX_OBSERVATIONS = 500                   # bounded: a page that logs in a loop cannot grow memory without limit
DEFAULT_ACTION_TIMEOUT_MS = 8000


def _chromium_executable() -> str | None:
    """Honor RESOLVE_CHROMIUM_PATH, else a pre-installed /opt/pw-browsers build when the
    pip Playwright revision has no matching download."""
    explicit = os.environ.get("RESOLVE_CHROMIUM_PATH")
    if explicit:
        return explicit
    base = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers"))
    for pattern in ("chromium-*/chrome-linux*/chrome", "chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"):
        found = sorted(base.glob(pattern))
        if found:
            return str(found[-1])
    return None


class CloudBrowserError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class CloudBrowserSession:
    def __init__(self, sandbox_id: str, owner_user_id: str, profile_dir: Path | str, policy: NetPolicy | None = None,
                 accept_downloads: bool = False, workspace_id: str = ""):
        self.policy = policy or NetPolicy()
        self.blocked_requests: list[dict] = []
        self._accept_downloads = accept_downloads
        self._obs: list[dict] = []
        self._downloads: list[dict] = []
        self._obs_lock = threading.Lock()      # separate from _lock: event handlers run while an action holds _lock
        self.sandbox_id = sandbox_id
        self.owner_user_id = owner_user_id
        self.workspace_id = workspace_id       # a session belongs to one user AND one workspace
        self.profile_dir = Path(profile_dir)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

        self._pw: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self.cursor_pos = [100, 100]
        self.last_action = "INITIALIZED"
        self.is_paused = False
        # Playwright's sync API is bound to the thread that created it. Every Playwright call therefore runs on this
        # session's own single worker thread, so any request thread (or the tool registry's timeout thread) can use it.
        self._exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"pw-{sandbox_id[:12]}")
        self._pw_thread: int | None = None
        self._on_worker(self._init_browser)

    def _on_worker(self, fn, *args, **kwargs):
        if self._pw_thread is not None and threading.get_ident() == self._pw_thread:
            return fn(*args, **kwargs)

        def run():
            self._pw_thread = threading.get_ident()
            return fn(*args, **kwargs)
        try:
            return self._exec.submit(run).result(timeout=90)
        except FutureTimeout as exc:
            raise CloudBrowserError("the browser did not respond in time", 504) from exc
        except RuntimeError as exc:   # executor shut down
            raise CloudBrowserError("The browser session is closed", 410) from exc

    def _init_browser(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise CloudBrowserError('Playwright is not installed on this deployment', status_code=503) from exc
        self._pw = sync_playwright().start()
        args = ["--disable-dev-shm-usage"]
        # Chromium's own sandbox stays ON unless the host cannot provide user namespaces
        # (typical root-in-container). Opt out explicitly; never silently.
        if os.environ.get("RESOLVE_BROWSER_NO_SANDBOX") == "1":
            args.append("--no-sandbox")
        kwargs = {}
        exe = _chromium_executable()
        if exe:
            kwargs["executable_path"] = exe
        self._context = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=True,
            viewport={"width": 1280, "height": 800},
            args=args,
            service_workers="block",
            accept_downloads=self._accept_downloads,
            **kwargs,
        )
        # Every request (navigations, redirects, subresources) passes the policy.
        self._context.route("**/*", self._route_guard)
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._attach_observers()

    # ------------------------------------------------------------------ observation hooks
    def _observe(self, kind: str, message: str, url: str | None = None, status: int | None = None, line: int | None = None) -> None:
        with self._obs_lock:
            if len(self._obs) < MAX_OBSERVATIONS:
                self._obs.append({"kind": kind, "message": str(message)[:500], "url": url, "status": status, "line": line, "ts": time.time()})

    @property
    def observations(self) -> list[dict]:
        """Snapshot (copy) of everything the page did that an operator should know about, oldest first."""
        with self._obs_lock:
            return [dict(o) for o in self._obs]

    @property
    def downloads(self) -> list[dict]:
        with self._obs_lock:
            return [dict(d) for d in self._downloads]

    def _attach_observers(self) -> None:
        page = self._page
        page.on("console", self._on_console)
        page.on("pageerror", self._on_pageerror)
        page.on("requestfailed", self._on_requestfailed)
        page.on("response", self._on_response)
        page.on("download", self._on_download)
        self._context.on("page", self._on_new_page)

    def _on_console(self, msg) -> None:
        if msg.type not in ("error", "warning"):
            return
        text = msg.text or ""
        if text.startswith("Failed to load resource"):
            return   # reported with status and URL by the response / requestfailed hooks
        loc = msg.location or {}
        line = (loc.get("lineNumber") + 1) if isinstance(loc.get("lineNumber"), int) else None
        self._observe("console_error" if msg.type == "error" else "console_warning", text, url=loc.get("url") or None, line=line)

    def _on_pageerror(self, err) -> None:
        stack = getattr(err, "stack", "") or ""
        m = re.search(r":(\d+):\d+", stack)
        self._observe("page_error", getattr(err, "message", str(err)), url=self._page.url if self._page else None, line=int(m.group(1)) if m else None)

    def _on_requestfailed(self, req) -> None:
        failure = req.failure or ""
        if "ERR_ABORTED" in str(failure):
            return   # downloads and cancelled navigations are not failures
        if any(b["url"] == req.url[:200] for b in list(self.blocked_requests)):
            return   # already reported as blocked_request with the policy reason
        self._observe("failed_request", f"{req.method} {req.url[:200]} failed: {failure}", url=req.url[:300])

    def _on_response(self, resp) -> None:
        if resp.status >= 400:
            self._observe("http_error", f"HTTP {resp.status} {resp.request.method} {resp.url[:200]}", url=resp.url[:300], status=resp.status)

    def _on_new_page(self, page) -> None:
        if page is self._page:
            return
        opener = self._page.url if self._page else None
        self._observe("popup", f"unrequested popup window opened by {opener} was closed without being followed", url=page.url or opener)
        try:
            page.close()
        except Exception:
            pass

    def _on_download(self, dl) -> None:
        name = dl.suggested_filename
        try:
            path = dl.path()   # waits for the transfer to finish
            size = os.path.getsize(path)
            if size > MAX_DOWNLOAD_BYTES:
                self._observe("download", f"{name}: {size} bytes exceeds the {MAX_DOWNLOAD_BYTES}-byte cap and was not kept", url=dl.url)
                return
            data = Path(path).read_bytes()
        except Exception as exc:
            self._observe("download", f"{name}: download failed ({exc})", url=dl.url)
            return
        sha = hashlib.sha256(data).hexdigest()
        with self._obs_lock:
            self._downloads.append({"name": name, "sha256": sha, "size": len(data), "bytes": data, "url": dl.url})
        self._observe("download", f"{name} ({len(data)} bytes, sha256 {sha[:16]})", url=dl.url)

    def _pw_error(self, exc: Exception, what: str) -> CloudBrowserError:
        """Translate Playwright failures into a bounded, readable CloudBrowserError."""
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeout
        except ImportError:
            PlaywrightTimeout = ()   # type: ignore[assignment]
        text = str(exc)
        first = text.splitlines()[0][:200] if text else exc.__class__.__name__
        if PlaywrightTimeout and isinstance(exc, PlaywrightTimeout):
            return CloudBrowserError(f"{what} timed out", 504)
        low = text.lower()
        if any(k in low for k in ("closed", "crashed", "connection closed", "disconnected", "has been closed")):
            return CloudBrowserError(f"The browser page was closed or crashed during {what}", 410)
        return CloudBrowserError(f"{what} failed: {first}", 502)

    def _require_page(self):
        if not self._page or self._page.is_closed():
            raise CloudBrowserError("The browser page is closed (tab closed or browser crashed)", 410)
        return self._page

    @property
    def is_alive(self) -> bool:
        return bool(self._page) and not self._page.is_closed()

    def _route_guard(self, route) -> None:
        """Validate the request, then fetch it with redirects disabled and validate every
        Location hop ourselves; Chromium does not re-route redirects it follows internally."""
        url = route.request.url
        if url.startswith("about:blank"):
            return route.continue_()
        try:
            self.policy.check_url(url)
            resp = route.fetch(max_redirects=0, timeout=15000)
            if 300 <= resp.status < 400 and resp.headers.get("location"):
                self.policy.check_url(urljoin(url, resp.headers["location"]))
        except PolicyViolation as exc:
            self.blocked_requests.append({"url": url[:200], "reason": exc.message, "t": time.time()})
            self._observe("blocked_request", f"blocked by network policy: {exc.message}", url=url[:300])
            return route.abort("blockedbyclient")
        except Exception as exc:  # fetch failure: fail closed
            self.blocked_requests.append({"url": url[:200], "reason": f"fetch failed: {exc}"[:200], "t": time.time()})
            self._observe("failed_request", f"{route.request.method} {url[:200]} failed: {str(exc).splitlines()[0][:160] if str(exc) else exc.__class__.__name__}", url=url[:300])
            return route.abort("failed")
        return route.fulfill(response=resp)

    def _validate_url(self, url: str) -> str:
        if "://" not in url and not url.startswith(("data:", "file:", "javascript:", "about:")):
            url = f"https://{url}"
        try:
            return self.policy.check_url(url)
        except PolicyViolation as exc:
            raise CloudBrowserError(exc.message, status_code=exc.status_code)

    def navigate(self, url: str, timeout_ms: int = 15000) -> dict:
        with self._lock:
            if self.is_paused:
                raise CloudBrowserError("Browser is paused")
            url = self._validate_url(url)
            page = self._require_page()
            self.last_action = f"NAVIGATE: {url}"
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                return {
                    "url": page.url,
                    "title": page.title(),
                    "status": resp.status if resp else 200,
                    "action": self.last_action
                }
            except CloudBrowserError:
                raise
            except Exception as exc:
                raise self._pw_error(exc, "navigation") from exc

    def capture_jpeg(self) -> dict:
        """Raw JPEG bytes of the current page plus url/title (for evidence storage; capture_frame() returns a data URL)."""
        with self._lock:
            page = self._require_page()
            try:
                return {"jpeg": page.screenshot(type="jpeg", quality=70, timeout=DEFAULT_ACTION_TIMEOUT_MS),
                        "url": page.url, "title": page.title(), "ts": time.time()}
            except Exception as exc:
                raise self._pw_error(exc, "screenshot") from exc

    def inspect(self) -> dict:
        """Read-only view of the page: url, title, visible text (UNTRUSTED data) and the forms/fields present."""
        with self._lock:
            page = self._require_page()
            try:
                info = page.locator("html").evaluate(
                    """() => ({text: (document.body ? document.body.innerText : '').slice(0, 4000),
                        forms: [...document.forms].map(f => ({id: f.id, fields: [...f.elements].filter(e => e.name || e.id)
                          .map(e => ({id: e.id, name: e.name, type: e.type}))}))})""", timeout=DEFAULT_ACTION_TIMEOUT_MS)
                return {"url": page.url, "title": page.title(), "text_excerpt": info["text"], "forms": info["forms"]}
            except Exception as exc:
                raise self._pw_error(exc, "inspection") from exc

    def has_selector(self, selector: str) -> bool:
        with self._lock:
            page = self._require_page()
            try:
                return page.locator(selector).count() > 0
            except Exception as exc:
                raise self._pw_error(exc, "selector lookup") from exc

    def click_selector(self, selector: str, timeout_ms: int = DEFAULT_ACTION_TIMEOUT_MS) -> dict:
        with self._lock:
            if self.is_paused:
                raise CloudBrowserError("Browser is paused")
            page = self._require_page()
            self.last_action = f"CLICK {selector}"
            try:
                page.click(selector, timeout=timeout_ms)
                return {"status": "SUCCESS", "action": self.last_action}
            except Exception as exc:
                raise self._pw_error(exc, f"click {selector}") from exc

    def type_selector(self, selector: str, text: str, timeout_ms: int = DEFAULT_ACTION_TIMEOUT_MS) -> dict:
        with self._lock:
            if self.is_paused:
                raise CloudBrowserError("Browser is paused")
            page = self._require_page()
            self.last_action = f"TYPE {selector} ({len(text)} chars)"   # the text itself is never logged (may be a secret)
            try:
                page.locator(selector).press_sequentially(text, timeout=timeout_ms)
                return {"status": "SUCCESS", "action": self.last_action}
            except Exception as exc:
                raise self._pw_error(exc, f"typing into {selector}") from exc

    def wait_idle(self, timeout_ms: int = 4000) -> bool:
        """Wait for the network to go quiet. Returns False (never raises) when the page stays busy."""
        with self._lock:
            page = self._require_page()
            try:
                page.wait_for_load_state("networkidle", timeout=timeout_ms)
                return True
            except Exception as exc:
                err = self._pw_error(exc, "waiting for the page")
                if err.status_code == 410:
                    raise err from exc
                return False

    def capture_frame(self) -> dict:
        with self._lock:
            if not self._page:
                raise CloudBrowserError("Browser page not available")
            screenshot_bytes = self._page.screenshot(type="jpeg", quality=75)
            data_url = f"data:image/jpeg;base64,{base64.b64encode(screenshot_bytes).decode('ascii')}"
            return {
                "sandbox_id": self.sandbox_id,
                "timestamp": time.time(),
                "url": self._page.url,
                "title": self._page.title(),
                "viewport": {"width": 1280, "height": 800},
                "cursor": {"x": self.cursor_pos[0], "y": self.cursor_pos[1]},
                "last_action": self.last_action,
                "is_paused": self.is_paused,
                "frame": data_url
            }

    def click(self, x: int, y: int) -> dict:
        with self._lock:
            if self.is_paused:
                raise CloudBrowserError("Browser is paused")
            self.cursor_pos = [int(x), int(y)]
            self.last_action = f"CLICK ({x}, {y})"
            self._require_page().mouse.click(x, y)
            return {"status": "SUCCESS", "cursor": self.cursor_pos, "action": self.last_action}

    def type_text(self, text: str) -> dict:
        with self._lock:
            if self.is_paused:
                raise CloudBrowserError("Browser is paused")
            self.last_action = f"TYPE ({len(text)} chars)"
            self._require_page().keyboard.type(text)
            return {"status": "SUCCESS", "action": self.last_action}

    def scroll(self, delta_x: int, delta_y: int) -> dict:
        with self._lock:
            self.last_action = f"SCROLL ({delta_x}, {delta_y})"
            self._require_page().mouse.wheel(delta_x, delta_y)
            return {"status": "SUCCESS", "action": self.last_action}

    def extract_text(self) -> str:
        with self._lock:
            return self._require_page().inner_text("body", timeout=DEFAULT_ACTION_TIMEOUT_MS) if self._page else ""

    # ---- generalized semantic operations (DOM-level, not physical cursor movement)
    def find(self, text: str | None = None, role: str | None = None, selector: str | None = None, limit: int = 20) -> dict:
        """Locate visible elements by visible text, ARIA role or CSS selector. Returns stable handles (a unique CSS
        path) plus the text of each match and its row/context text, so a caller can disambiguate by context."""
        with self._lock:
            page = self._require_page()
            try:
                return {"url": page.url, "matches": page.evaluate(
                    """([text, role, selector, limit]) => {
                      const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
                      const path = e => { const parts = []; for (let n = e; n && n.nodeType === 1 && n !== document.body; n = n.parentElement) { let i = 1; for (let p = n.previousElementSibling; p; p = p.previousElementSibling) if (p.tagName === n.tagName) i++; parts.unshift(n.tagName.toLowerCase() + ':nth-of-type(' + i + ')'); } return 'body > ' + parts.join(' > '); };
                      let els = selector ? [...document.querySelectorAll(selector)] : [...document.querySelectorAll('a,button,input,select,textarea,tr,li,h1,h2,h3,[role],[data-testid],td,th,p,span,div')];
                      if (role) els = els.filter(e => (e.getAttribute('role') || ({A:'link',BUTTON:'button',INPUT:'textbox',SELECT:'combobox',TR:'row',LI:'listitem'})[e.tagName] || '') === role);
                      if (text) { const t = text.toLowerCase(); els = els.filter(e => (e.innerText || e.value || '').toLowerCase().includes(t)); if (!role && !selector) els = els.filter(e => ![...e.children].some(c => (c.innerText || '').toLowerCase().includes(t))); }
                      return els.filter(vis).slice(0, limit).map(e => ({ handle: path(e), tag: e.tagName.toLowerCase(), id: e.id || null, testid: e.getAttribute('data-testid'), text: (e.innerText || e.value || '').trim().slice(0, 200), context: ((e.closest('tr,li,article,section') || e.parentElement || e).innerText || '').trim().slice(0, 300) }));
                    }""", [text, role, selector, limit])}
            except Exception as exc:
                raise self._pw_error(exc, "find") from exc

    def select_option(self, selector: str, value: str, timeout_ms: int = DEFAULT_ACTION_TIMEOUT_MS) -> dict:
        with self._lock:
            if self.is_paused:
                raise CloudBrowserError("Browser is paused")
            page = self._require_page()
            self.last_action = f"SELECT {selector}"
            try:
                chosen = page.locator(selector).select_option(value, timeout=timeout_ms)
                return {"status": "SUCCESS", "action": self.last_action, "selected": chosen}
            except Exception as exc:
                raise self._pw_error(exc, f"select in {selector}") from exc

    def wait_for(self, selector: str | None = None, text: str | None = None, timeout_ms: int = 8000) -> dict:
        """Wait until a selector is visible or the text appears. Raises a 504 CloudBrowserError on timeout (never a silent pass)."""
        with self._lock:
            page = self._require_page()
            try:
                if selector:
                    page.locator(selector).first.wait_for(state="visible", timeout=timeout_ms)
                elif text:
                    page.get_by_text(text, exact=False).first.wait_for(state="visible", timeout=timeout_ms)
                else:
                    page.wait_for_load_state("load", timeout=timeout_ms)
                return {"status": "SUCCESS", "url": page.url}
            except Exception as exc:
                raise self._pw_error(exc, "waiting") from exc

    def extract(self, selector: str | None = None, limit: int = 8000) -> dict:
        """Text of an element (or the page) and the rows of the first table within it. Untrusted data."""
        with self._lock:
            page = self._require_page()
            try:
                return page.evaluate(
                    """([selector, limit]) => { const root = selector ? document.querySelector(selector) : document.body; if (!root) return {found: false};
                      const rows = [...root.querySelectorAll('table tr')].slice(0, 200).map(r => [...r.children].map(c => (c.innerText || '').trim()));
                      return {found: true, url: location.href, text: (root.innerText || '').slice(0, limit), rows}; }""", [selector, limit])
            except Exception as exc:
                raise self._pw_error(exc, "extract") from exc

    def verify_state(self, selector: str | None = None, text: str | None = None, url_contains: str | None = None) -> dict:
        """Check an observable condition on the live page. Returns each requested check and an overall boolean."""
        with self._lock:
            page = self._require_page()
            try:
                checks = {}
                if url_contains is not None:
                    checks["url_contains"] = url_contains in page.url
                if selector is not None:
                    checks["selector_visible"] = page.locator(selector).count() > 0 and page.locator(selector).first.is_visible()
                if text is not None:
                    checks["text_present"] = text.lower() in (page.inner_text("body", timeout=DEFAULT_ACTION_TIMEOUT_MS) or "").lower()
                return {"checks": checks, "verified": bool(checks) and all(checks.values()), "url": page.url}
            except Exception as exc:
                raise self._pw_error(exc, "verify") from exc

    def download_via(self, selector: str, timeout_ms: int = 15000) -> dict:
        """Click a download link and wait for the captured file (name, size, sha256 of the real bytes)."""
        before = len(self.downloads)
        self.click_selector(selector)
        end = time.time() + timeout_ms / 1000
        while time.time() < end:
            d = self.downloads
            if len(d) > before:
                last = d[-1]
                return {"name": last["name"], "size": last["size"], "sha256": last["sha256"]}
            time.sleep(0.1)
        raise CloudBrowserError("download did not complete in time", 504)

    def pause(self) -> bool:
        with self._lock:
            self.is_paused = True
            self.last_action = "PAUSED_BY_USER"
            return True

    def resume(self) -> bool:
        with self._lock:
            self.is_paused = False
            self.last_action = "RESUMED_BY_USER"
            return True

    def close(self) -> None:
        try:
            self._on_worker(self._close_on_worker)
        except CloudBrowserError:
            pass
        self._exec.shutdown(wait=False)

    def _close_on_worker(self) -> None:
        with self._lock:
            try:
                if self._context:
                    self._context.close()
                if self._pw:
                    self._pw.stop()
            except Exception:
                pass
            self._context = None
            self._page = None
            self._pw = None


class CloudBrowserPool:
    """Active cloud browser sessions, bound to (sandbox, user, workspace). Every lookup requires BOTH identities, and a
    mismatch is reported exactly like a missing session, so one tenant cannot learn whether another tenant's session exists."""

    def __init__(self, policy: NetPolicy | None = None):
        self._policy = policy
        self._sessions: dict[str, CloudBrowserSession] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _require_identity(user_id: str, workspace_id: str) -> None:
        if not user_id or not workspace_id:
            raise CloudBrowserError("authenticated user and workspace are required", status_code=401)

    def get_or_create(self, sandbox_id: str, owner_user_id: str, storage_path: str, workspace_id: str) -> CloudBrowserSession:
        self._require_identity(owner_user_id, workspace_id)
        with self._lock:
            if sandbox_id in self._sessions:
                session = self._sessions[sandbox_id]
                if session.owner_user_id != owner_user_id or session.workspace_id != workspace_id:
                    raise CloudBrowserError(f"No active browser for sandbox {sandbox_id}", status_code=404)
                return session

            profile_dir = Path(storage_path) / "browser_profile"
            session = CloudBrowserSession(sandbox_id, owner_user_id, profile_dir, self._policy, workspace_id=workspace_id)
            self._sessions[sandbox_id] = session
            return session

    def get_session(self, sandbox_id: str, caller_user_id: str, caller_workspace_id: str) -> CloudBrowserSession:
        self._require_identity(caller_user_id, caller_workspace_id)
        with self._lock:
            session = self._sessions.get(sandbox_id)
            if not session or session.owner_user_id != caller_user_id or session.workspace_id != caller_workspace_id:
                raise CloudBrowserError(f"No active browser for sandbox {sandbox_id}", status_code=404)
            return session

    def close_session(self, sandbox_id: str, caller_user_id: str, caller_workspace_id: str) -> bool:
        """Closes only a session the caller owns. A rejected call has no side effect: the session stays open."""
        self._require_identity(caller_user_id, caller_workspace_id)
        with self._lock:
            session = self._sessions.get(sandbox_id)
            if not session or session.owner_user_id != caller_user_id or session.workspace_id != caller_workspace_id:
                return False
            self._sessions.pop(sandbox_id)
            session.close()
            return True

    def close_all(self) -> None:
        with self._lock:
            for s in list(self._sessions.values()):
                s.close()
            self._sessions.clear()


def _bind_to_worker(name: str):
    inner = getattr(CloudBrowserSession, name)

    def wrapper(self, *args, **kwargs):
        return self._on_worker(inner, self, *args, **kwargs)
    wrapper.__name__, wrapper.__doc__ = inner.__name__, inner.__doc__
    setattr(CloudBrowserSession, name, wrapper)


for _name in ("navigate", "capture_jpeg", "inspect", "has_selector", "click_selector", "type_selector", "wait_idle", "capture_frame",
              "click", "type_text", "scroll", "extract_text", "find", "select_option", "wait_for", "extract", "verify_state", "download_via"):
    _bind_to_worker(_name)
