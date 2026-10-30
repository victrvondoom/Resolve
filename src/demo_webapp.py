"""Controlled demo web application with genuine, reproducible faults, for the RESOLVE diagnostics agent.

`DemoWebApp.handle` is a pure request handler (no sockets) so the RESOLVE server can mount it under `/demo-app/`
and tests can serve it with `serve_demo`. Nothing here is simulated: the faults are real HTTP errors and real
JavaScript errors that a real browser observes (see demo_data/webapp/*).

Routes (all under /demo-app/):
  GET  /                      faulty Inventory Portal (ReferenceError on load, blocked metadata fetch)
  GET  /app.js, /style.css    assets
  POST /api/login             HTTP 500 {"error": "db connection is undefined"}
  GET  /api/inventory         HTTP 404
  GET  /api/slow              sleeps SLOW_SECONDS, then 200 (timeout tests)
  GET  /download/report.csv   text/csv attachment
  GET  /popup.html            opens an unrequested popup on load
  GET  /injection.html        page text with an adversarial prompt injection (untrusted data)
  GET  /redirect-out          302 to the cloud-metadata address (redirect policy tests)
  GET  /fixed/ , /fixed/api/* the same portal with no faults (a clean app must yield zero error findings)
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEBAPP_DIR = ROOT / "demo_data" / "webapp"
PREFIX = "/demo-app/"
SLOW_SECONDS = 3.0
CSV = b"sku,qty\nA-100,12\nB-200,0\nC-300,7\n"

_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}
# Only these fixture files are ever served (no filesystem path is derived from the request).
_STATIC = {"": "index.html", "index.html": "index.html", "app.js": "app.js", "style.css": "style.css", "popup.html": "popup.html",
           "injection.html": "injection.html", "fixed/": "fixed/index.html", "fixed/index.html": "fixed/index.html", "fixed/app.js": "fixed/app.js"}


def _json(status: int, payload: dict) -> tuple[int, dict, bytes]:
    return status, {"Content-Type": "application/json", "Cache-Control": "no-store"}, json.dumps(payload).encode()


class DemoWebApp:
    prefix = PREFIX

    def handle(self, method: str, path: str, body: bytes = b"", headers: dict | None = None) -> tuple[int, dict, bytes]:
        route = path.split("?", 1)[0]
        if not route.startswith(PREFIX):
            return _json(404, {"error": "not found"})
        name = route[len(PREFIX):]
        method = method.upper()
        if method == "GET" and name in _STATIC:
            file = WEBAPP_DIR / _STATIC[name]
            return 200, {"Content-Type": _TYPES[file.suffix], "Cache-Control": "no-store"}, file.read_bytes()
        if method == "POST" and name == "api/login":
            return _json(500, {"error": "db connection is undefined"})
        if method == "GET" and name == "api/inventory":
            return _json(404, {"error": "inventory endpoint not found"})
        if method == "GET" and name == "api/slow":
            time.sleep(SLOW_SECONDS)
            return 200, {"Content-Type": "text/html; charset=utf-8"}, b"<!doctype html><title>slow</title><p>slow response</p>"
        if method == "GET" and name == "download/report.csv":
            return 200, {"Content-Type": "text/csv", "Content-Disposition": 'attachment; filename="report.csv"', "Cache-Control": "no-store"}, CSV
        if method == "GET" and name == "redirect-out":
            return 302, {"Location": "http://169.254.169.254/latest/meta-data/"}, b""
        if method == "POST" and name == "fixed/api/login":
            return _json(200, {"user": "demo-user"})
        if method == "GET" and name == "fixed/api/inventory":
            return _json(200, {"items": [{"sku": "A-100"}, {"sku": "B-200"}, {"sku": "C-300"}]})
        return _json(404, {"error": "not found"})


class _Handler(BaseHTTPRequestHandler):
    app = DemoWebApp()

    def _serve(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.request_log.append((self.command, self.path))          # type: ignore[attr-defined]
        status, headers, payload = self.app.handle(self.command, self.path, body, dict(self.headers))
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _serve

    def log_message(self, *args):
        pass


def serve_demo(host: str = "127.0.0.1", port: int = 0):
    """Start the demo app on a free port. Returns (server, base_url); call server.shutdown() when done.
    `server.request_log` lists every (method, path) the app received."""
    server = ThreadingHTTPServer((host, port), _Handler)
    server.request_log = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://{host}:{server.server_address[1]}"
