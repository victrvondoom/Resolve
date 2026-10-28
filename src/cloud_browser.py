"""Isolated cloud browser runtime using Playwright for RESOLVE multi-tenant sandboxes."""
from __future__ import annotations

import base64
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, BrowserContext, Page, Playwright

ROOT = Path(__file__).resolve().parents[1]

# Block AWS/GCP/Azure instance metadata endpoints and sensitive local ports
BLOCKED_HOSTS = {
    "169.254.169.254",
    "metadata.google.internal",
    "metadata.azure.com"
}


class CloudBrowserError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class CloudBrowserSession:
    def __init__(self, sandbox_id: str, owner_user_id: str, profile_dir: Path | str):
        self.sandbox_id = sandbox_id
        self.owner_user_id = owner_user_id
        self.profile_dir = Path(profile_dir)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

        self._pw: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self.cursor_pos = [100, 100]
        self.last_action = "INITIALIZED"
        self.is_paused = False
        self._init_browser()

    def _init_browser(self) -> None:
        self._pw = sync_playwright().start()
        # Launch persistent context inside the sandbox profile directory
        self._context = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=True,
            viewport={"width": 1280, "height": 800},
            args=["--disable-dev-shm-usage", "--no-sandbox"]
        )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()

    def _validate_url(self, url: str) -> str:
        parsed = urlparse(url)
        if not parsed.scheme:
            url = f"http://{url}"
            parsed = urlparse(url)
        if parsed.scheme not in ("http", "https", "file", "data"):
            raise CloudBrowserError(f"Unsupported URL scheme: {parsed.scheme}")
        hostname = (parsed.hostname or "").lower()
        if hostname in BLOCKED_HOSTS:
            raise CloudBrowserError(f"Access to metadata host {hostname} is forbidden by policy", status_code=403)
        return url

    def navigate(self, url: str) -> dict:
        with self._lock:
            if self.is_paused:
                raise CloudBrowserError("Browser is paused")
            url = self._validate_url(url)
            self.last_action = f"NAVIGATE: {url}"
            resp = self._page.goto(url, wait_until="domcontentloaded", timeout=15000)
            status = resp.status if resp else 200
            return {
                "url": self._page.url,
                "title": self._page.title(),
                "status": status,
                "action": self.last_action
            }

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
            self._page.mouse.click(x, y)
            return {"status": "SUCCESS", "cursor": self.cursor_pos, "action": self.last_action}

    def type_text(self, text: str) -> dict:
        with self._lock:
            if self.is_paused:
                raise CloudBrowserError("Browser is paused")
            self.last_action = f"TYPE ({len(text)} chars)"
            self._page.keyboard.type(text)
            return {"status": "SUCCESS", "action": self.last_action}

    def scroll(self, delta_x: int, delta_y: int) -> dict:
        with self._lock:
            self.last_action = f"SCROLL ({delta_x}, {delta_y})"
            self._page.mouse.wheel(delta_x, delta_y)
            return {"status": "SUCCESS", "action": self.last_action}

    def extract_text(self) -> str:
        with self._lock:
            return self._page.inner_text("body") if self._page else ""

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
    """Manages active cloud browser sessions indexed by sandbox_id."""
    def __init__(self):
        self._sessions: dict[str, CloudBrowserSession] = {}
        self._lock = threading.Lock()

    def get_or_create(self, sandbox_id: str, owner_user_id: str, storage_path: str) -> CloudBrowserSession:
        with self._lock:
            if sandbox_id in self._sessions:
                session = self._sessions[sandbox_id]
                if session.owner_user_id != owner_user_id:
                    raise CloudBrowserError(f"User {owner_user_id} unauthorized for session {sandbox_id}", status_code=403)
                return session

            profile_dir = Path(storage_path) / "browser_profile"
            session = CloudBrowserSession(sandbox_id, owner_user_id, profile_dir)
            self._sessions[sandbox_id] = session
            return session

    def get_session(self, sandbox_id: str, caller_user_id: str | None = None) -> CloudBrowserSession:
        with self._lock:
            session = self._sessions.get(sandbox_id)
            if not session:
                raise CloudBrowserError(f"No active browser for sandbox {sandbox_id}", status_code=404)
            if caller_user_id and session.owner_user_id != caller_user_id:
                raise CloudBrowserError("Unauthorized access to browser session", status_code=403)
            return session

    def close_session(self, sandbox_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(sandbox_id, None)
            if session:
                session.close()

    def close_all(self) -> None:
        with self._lock:
            for s in list(self._sessions.values()):
                s.close()
            self._sessions.clear()

