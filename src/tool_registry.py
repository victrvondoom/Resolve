"""Typed, permissioned, observable tool registry.

Every computer-work operation (browser, document, workspace, mission control) is a Tool with a typed input schema,
a required capability, a hard timeout and an evidence trail. Capabilities come ONLY from the ToolContext (set by the
server from the mission's configuration), never from arguments or tool output, so text found on a web page cannot
grant itself permissions. Pure Python; services are injected through ToolContext.services.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

SECRET_KEYS = re.compile(r"(password|passwd|token|secret|api[_-]?key|authorization)", re.I)
MAX_DEFAULT_LEN = 10_000
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ToolError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code


class PermissionDenied(ToolError):
    def __init__(self, message: str, code: str = "PERMISSION_DENIED"):
        super().__init__(code, message, 403)


class InvalidInput(ToolError):
    def __init__(self, message: str, field_name: str | None = None):
        super().__init__("INVALID_INPUT", message, 422)
        self.field = field_name


class ToolTimeout(ToolError):
    def __init__(self, message: str):
        super().__init__("TIMEOUT", message, 504)


@dataclass(frozen=True)
class Field:
    type: str  # str | int | float | bool | url | id | enum | list | dict
    required: bool = True
    max_len: int | None = None
    min: float | None = None
    max: float | None = None
    choices: tuple | None = None
    pattern: str | None = None


@dataclass
class ToolContext:
    user_id: str
    workspace_id: str
    mission_id: str | None = None
    capabilities: frozenset = frozenset()
    services: dict = field(default_factory=dict)
    record: Callable[[dict], str | None] | None = None


@dataclass(frozen=True)
class Tool:
    name: str
    summary: str
    input: dict
    capability: str
    timeout_s: float
    handler: Callable[[ToolContext, dict], dict]
    consequential: bool = False
    idempotent: bool = True


@dataclass
class ToolResult:
    tool: str
    ok: bool
    data: dict
    error: dict | None
    started_at: float
    ended_at: float
    evidence_ref: str | None
    args_sha256: str


def _canonical(args: Any) -> str:
    return json.dumps(args, sort_keys=True, default=str, separators=(",", ":"))


def args_digest(args: Any) -> str:
    return hashlib.sha256(_canonical(args).encode()).hexdigest()


def redact(value: Any) -> Any:
    """Redact secret-looking keys anywhere in a structure (used for anything we record)."""
    if isinstance(value, dict):
        return {k: ("<redacted>" if SECRET_KEYS.search(str(k)) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def _check(name: str, f: Field, v: Any) -> Any:
    def bad(msg):
        raise InvalidInput(f"{name}: {msg}", name)
    t = f.type
    if t in ("str", "url", "id", "enum"):
        if not isinstance(v, str):
            bad("must be a string")
        if len(v) > (f.max_len or MAX_DEFAULT_LEN):
            bad(f"longer than {f.max_len or MAX_DEFAULT_LEN} characters")
        if t == "id" and not ID_RE.match(v):
            bad("must be an id of letters, digits, '_' or '-' (max 64)")
        if t == "url":
            p = urlparse(v)
            if p.scheme not in ("http", "https") or not p.hostname:
                bad("must be an http(s) URL")
        if t == "enum" and v not in (f.choices or ()):
            bad(f"must be one of {list(f.choices or ())}")
        if f.pattern and not re.fullmatch(f.pattern, v):
            bad("does not match the required pattern")
    elif t == "int":
        if isinstance(v, bool) or not isinstance(v, int):
            bad("must be an integer")
    elif t == "float":
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            bad("must be a number")
    elif t == "bool":
        if not isinstance(v, bool):
            bad("must be true or false")
    elif t == "list":
        if not isinstance(v, list):
            bad("must be a list")
        if len(v) > (f.max_len or 100):
            bad(f"more than {f.max_len or 100} items")
    elif t == "dict":
        if not isinstance(v, dict):
            bad("must be an object")
        if len(v) > (f.max_len or 100):
            bad(f"more than {f.max_len or 100} entries")
    else:  # pragma: no cover - schema authoring error
        raise ToolError("BAD_SCHEMA", f"unknown field type {t}", 500)
    if t in ("int", "float"):
        if f.min is not None and v < f.min:
            bad(f"must be >= {f.min}")
        if f.max is not None and v > f.max:
            bad(f"must be <= {f.max}")
    return v


def validate(schema: dict, args: Any) -> dict:
    if not isinstance(args, dict):
        raise InvalidInput("arguments must be an object")
    unknown = set(args) - set(schema)
    if unknown:
        raise InvalidInput(f"unknown argument(s): {sorted(unknown)}", sorted(unknown)[0])
    out = {}
    for name, f in schema.items():
        if name not in args or args[name] is None:
            if f.required:
                raise InvalidInput(f"{name}: required", name)
            continue
        out[name] = _check(name, f, args[name])
    return out


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._lock = threading.Lock()
        self._dedup: dict[tuple, Any] = {}
        self.services_schema_doc: dict = {}

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        if tool.consequential and not tool.idempotent and "idempotency_key" not in tool.input:
            tool = Tool(tool.name, tool.summary, {**tool.input, "idempotency_key": Field("id", required=True)},
                        tool.capability, tool.timeout_s, tool.handler, tool.consequential, tool.idempotent)
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except (KeyError, TypeError):
            raise ToolError("UNKNOWN_TOOL", f"unknown tool: {str(name)[:60]}", 404)

    def list_for(self, capabilities) -> list[Tool]:
        caps = frozenset(capabilities)
        return [t for t in self._tools.values() if t.capability in caps]

    def names(self) -> list[str]:
        return sorted(self._tools)

    # ---- execution
    def call(self, ctx: ToolContext, name: str, args: dict, *, cancel: Callable[[], bool] = lambda: False) -> ToolResult:
        started = time.time()
        digest = args_digest(args)
        try:
            tool = self.get(name)
            self._authorize(ctx, tool)
            clean = validate(tool.input, args)
            digest = args_digest(clean)
            if cancel():
                raise ToolError("CANCELLED", "cancelled before start", 409)
            key = None
            if tool.consequential and not tool.idempotent:
                key = (ctx.user_id, ctx.workspace_id, tool.name, clean["idempotency_key"])
                first, wait = self._claim(key)
                if not first:
                    prior = wait()
                    data = dict(prior.data)
                    data["deduplicated"] = True
                    return ToolResult(tool.name, prior.ok, data, prior.error, started, time.time(), prior.evidence_ref, digest)
            res = self._execute(ctx, tool, clean, started, digest)
            if key is not None:
                self._release(key, res if res.ok else None)   # a failed dispatch may be retried with the same key
            return res
        except ToolError as exc:
            return self._failure(ctx, name, exc, started, digest)
        except Exception as exc:  # defensive: never leak a raw traceback
            return self._failure(ctx, name, ToolError("INTERNAL", type(exc).__name__, 500), started, digest)

    def _authorize(self, ctx: ToolContext, tool: Tool):
        if not ctx.user_id or not ctx.workspace_id:
            raise PermissionDenied("authenticated user and workspace required", "UNAUTHENTICATED")
        if tool.capability not in ctx.capabilities:
            raise PermissionDenied(f"capability '{tool.capability}' not granted for this mission")

    def _execute(self, ctx, tool: Tool, clean: dict, started: float, digest: str) -> ToolResult:
        box: dict = {}

        def target():
            try:
                box["value"] = tool.handler(ctx, clean)
            except BaseException as exc:  # noqa: BLE001
                box["error"] = exc
        t = threading.Thread(target=target, name=f"tool-{tool.name}", daemon=True)
        t.start()
        t.join(tool.timeout_s)
        if t.is_alive():
            return self._failure(ctx, tool.name, ToolTimeout(f"{tool.name} exceeded {tool.timeout_s}s"), started, digest)
        if "error" in box:
            exc = box["error"]
            if isinstance(exc, ToolError):
                return self._failure(ctx, tool.name, exc, started, digest)
            return self._failure(ctx, tool.name, ToolError("TOOL_FAILED", f"{tool.name} failed: {type(exc).__name__}: {str(exc)[:200]}", 500), started, digest)
        value = box.get("value")
        data = value if isinstance(value, dict) else {"value": value}
        return self._finish(ctx, ToolResult(tool.name, True, data, None, started, time.time(), None, digest))

    def _failure(self, ctx, name, exc: ToolError, started, digest) -> ToolResult:
        err = {"code": exc.code, "message": exc.message, "status_code": exc.status_code}
        if getattr(exc, "field", None):
            err["field"] = exc.field
        return self._finish(ctx, ToolResult(str(name)[:60], False, {}, err, started, time.time(), None, digest))

    def _finish(self, ctx, res: ToolResult) -> ToolResult:
        if ctx.record is not None:
            try:
                res.evidence_ref = ctx.record({
                    "tool": res.tool, "args_sha256": res.args_sha256, "ok": res.ok,
                    "error_code": (res.error or {}).get("code"), "started_at": res.started_at, "ended_at": res.ended_at,
                    "mission_id": ctx.mission_id, "user_id": ctx.user_id,
                    "detail": redact(res.data) if res.ok else None,
                })
            except Exception:  # evidence failure must not mask the tool outcome
                res.evidence_ref = None
        return res

    # ---- idempotency bookkeeping: the first dispatch runs, concurrent duplicates wait for and reuse its result
    def _claim(self, key):
        with self._lock:
            entry = self._dedup.get(key)
            if entry is None:
                self._dedup[key] = threading.Event()
                return True, None

        def wait():
            if isinstance(entry, threading.Event):
                entry.wait(60)
            with self._lock:
                final = self._dedup.get(key)
            if not isinstance(final, ToolResult):
                raise ToolError("DUPLICATE_PENDING", "the original dispatch did not complete; retry", 409)
            return final
        return False, wait

    def _release(self, key, res: ToolResult | None):
        with self._lock:
            entry = self._dedup.get(key)
            if res is None:
                self._dedup.pop(key, None)
            else:
                self._dedup[key] = res
        if isinstance(entry, threading.Event):
            entry.set()


# ---------------------------------------------------------------- standard catalog
SERVICES_SCHEMA_DOC = {
    "browser.navigate": "callable(ctx, {url}) -> {url,title,status}",
    "browser.click": "callable(ctx, {selector})",
    "browser.type": "callable(ctx, {selector,text})",
    "browser.scroll": "callable(ctx, {dy})",
    "browser.inspect": "callable(ctx, {}) -> {url,title,text,...}",
    "browser.capture": "callable(ctx, {}) -> {frame...}",
    "browser.download": "callable(ctx, {selector}) -> {name,sha256,size}",
    "browser.find": "callable(ctx, {text?,role?,selector?}) -> {matches:[{handle,text,context}]}",
    "browser.select": "callable(ctx, {selector,value}) -> {selected}",
    "browser.wait": "callable(ctx, {selector?|text?,timeout_ms?}) -> {status}",
    "browser.extract": "callable(ctx, {selector?}) -> {text,rows}",
    "browser.verify": "callable(ctx, {selector?,text?,url_contains?}) -> {checks,verified}",
    "browser.close": "callable(ctx, {}) -> {closed}",
    "document.read": "callable(ctx, {document_id}) -> {text,sha256}",
    "document.transform": "callable(ctx, {operation,document_ids,expected_sha256,...}) -> {document}",
    "document.generate": "callable(ctx, {title,text}) -> {document}",
    "document.verify": "callable(ctx, {document_id,expected_sha256})",
    "workspace.create": "callable(ctx, {name}) -> {id,name}",
    "workspace.export": "callable(ctx, {}) -> {bundle}",
    "mission.pause": "callable(ctx, {mission_id})",
    "mission.cancel": "callable(ctx, {mission_id})",
    "approval.request": "callable(ctx, {mission_id,plan_digest})",
    "evidence.record": "callable(ctx, {kind,note}) -> {evidence_ref}",
}


def _delegate(service: str):
    def handler(ctx: ToolContext, args: dict) -> dict:
        fn = ctx.services.get(service)
        if not callable(fn):
            raise ToolError("UNAVAILABLE", f"service '{service}' is not available on this deployment", 503)
        result = fn(ctx, {k: v for k, v in args.items() if k != "idempotency_key"})
        return result if isinstance(result, dict) else {"value": result}
    return handler


def default_registry(services_schema_doc: dict | None = None) -> ToolRegistry:
    """Standard catalog. Handlers delegate to ctx.services[<tool name>] (a callable(ctx, args)); the parent wires them."""
    reg = ToolRegistry()
    reg.services_schema_doc = services_schema_doc or SERVICES_SCHEMA_DOC
    sel = Field("str", max_len=300)
    sha = Field("str", max_len=64, pattern=r"[0-9a-f]{64}")
    defs = [
        # name, summary, schema, capability, timeout_s, consequential, idempotent
        ("browser.navigate", "Open an http(s) URL in the mission's browser.", {"url": Field("url", max_len=2000)}, "browser:navigate", 20, False, True),
        ("browser.click", "Click an element.", {"selector": sel}, "browser:interact", 10, False, True),
        ("browser.type", "Type text into an element.", {"selector": sel, "text": Field("str", max_len=5000)}, "browser:interact", 10, False, True),
        ("browser.scroll", "Scroll the page vertically.", {"dy": Field("int", min=-5000, max=5000)}, "browser:interact", 5, False, True),
        ("browser.inspect", "Read the page text and observations (untrusted data).", {}, "browser:interact", 10, False, True),
        ("browser.capture", "Capture a screenshot frame.", {}, "browser:interact", 10, False, True),
        ("browser.download", "Download the file behind a link.", {"selector": sel}, "browser:interact", 30, False, True),
        ("browser.find", "Find visible elements by text, role or selector (returns stable handles and row context).",
         {"text": Field("str", required=False, max_len=200), "role": Field("str", required=False, max_len=40, pattern=r"[a-z]+"), "selector": Field("str", required=False, max_len=300)}, "browser:interact", 10, False, True),
        ("browser.select", "Choose an option in a select element.", {"selector": sel, "value": Field("str", max_len=200)}, "browser:interact", 10, False, True),
        ("browser.wait", "Wait for an element or text to appear (fails on timeout).",
         {"selector": Field("str", required=False, max_len=300), "text": Field("str", required=False, max_len=200), "timeout_ms": Field("int", required=False, min=100, max=15000)}, "browser:interact", 16, False, True),
        ("browser.extract", "Extract page or element text and table rows (untrusted data).", {"selector": Field("str", required=False, max_len=300)}, "browser:interact", 10, False, True),
        ("browser.verify", "Check an observable condition on the live page.",
         {"selector": Field("str", required=False, max_len=300), "text": Field("str", required=False, max_len=200), "url_contains": Field("str", required=False, max_len=300)}, "browser:interact", 10, False, True),
        ("browser.close", "Close the mission's browser session.", {}, "browser:interact", 10, False, True),
        ("document.read", "Read a stored PDF's text.", {"document_id": Field("id")}, "document:read", 20, False, True),
        ("document.transform", "Rotate, merge or delete pages, producing a new version.",
         {"operation": Field("enum", choices=("rotate", "merge", "delete_pages")), "document_ids": Field("list", max_len=20),
          "expected_sha256": Field("dict", max_len=20), "degrees": Field("int", required=False, min=0, max=359),
          "pages": Field("list", required=False, max_len=500)}, "document:write", 60, True, False),
        ("document.generate", "Generate a PDF from title and text.", {"title": Field("str", max_len=120), "text": Field("str", max_len=100_000)}, "document:write", 60, True, False),
        ("document.verify", "Re-read a stored PDF and check its hash.", {"document_id": Field("id"), "expected_sha256": sha}, "document:read", 30, False, True),
        ("workspace.create", "Create a workspace.", {"name": Field("str", max_len=60)}, "workspace:manage", 10, True, False),
        ("workspace.export", "Export workspace metadata.", {}, "workspace:manage", 30, False, True),
        ("mission.pause", "Pause a mission.", {"mission_id": Field("id")}, "mission:control", 10, False, True),
        ("mission.cancel", "Cancel a mission.", {"mission_id": Field("id")}, "mission:control", 10, True, False),
        ("approval.request", "Ask the user to approve an exact plan digest.", {"mission_id": Field("id"), "plan_digest": sha}, "approval:request", 10, False, True),
        ("evidence.record", "Attach a note to the mission's evidence.", {"kind": Field("str", max_len=40, pattern=r"[A-Za-z0-9_.-]+"), "note": Field("str", max_len=2000)}, "evidence:write", 10, False, True),
    ]
    for name, summary, schema, cap, timeout, consequential, idempotent in defs:
        reg.register(Tool(name, summary, schema, cap, float(timeout), _delegate(name), consequential, idempotent))
    return reg
