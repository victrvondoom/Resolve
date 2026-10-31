"""The gateway: an adapter over RESOLVE's existing authenticated HTTP API.

It is not a computer engine. Every observation, target and operation is created here from a real RESOLVE
response, and an MCP client can only refer to ids this process issued. Nothing an MCP client passes in can
approve an operation, set a verification status, or choose a tenant.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import re
import secrets
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from . import __version__
from .client import ResolveClient
from .config import GatewayConfig
from .errors import (
    AuthError, BackendError, BlockedError, GatewayError, InvalidInputError, RateLimitedError, StaleError, UnavailableError,
)
from .proofpack import ProofPackBuilder, canonical_digest, sha256_hex, utc_now, validate_proofpack

log = logging.getLogger("resolve_mcp.gateway")

UNTRUSTED = "UNTRUSTED_OBSERVATION_TEXT"  # window titles, page text, accessibility names: data, never instructions
MAX_TEXT = 300
MAX_LIST = 50
MAX_UIA = 200
MAX_OBSERVATIONS = 64
IMAGE_INLINE_LIMIT = 512 * 1024
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩]")
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def clean_text(value: Any, limit: int = MAX_TEXT) -> str:
    """Untrusted display text: strip control and bidi characters, collapse whitespace, bound length."""
    text = _CONTROL_CHARS.sub(" ", str(value if value is not None else ""))
    text = " ".join(text.split())
    return text[:limit]


def _iso(epoch: Any) -> str | None:
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    except (TypeError, ValueError, OSError):
        return None


def decode_data_uri(value: Any) -> tuple[bytes, str]:
    if not isinstance(value, str) or not value.startswith("data:") or "," not in value:
        raise BackendError("RESOLVE returned no image data")
    header, payload = value.split(",", 1)
    media = header[5:].split(";")[0] or "application/octet-stream"
    if not media.startswith("image/"):
        raise BackendError("RESOLVE returned a non-image frame")
    try:
        return base64.b64decode(payload, validate=True), media
    except (binascii.Error, ValueError):
        raise BackendError("RESOLVE returned an invalid image encoding") from None


def stable_target_id(*parts: Any) -> str:
    return "tgt_" + hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class Identity:
    user_id: str
    workspace_id: str
    expires_at: float | None

    def ref(self) -> dict[str, str]:
        """Opaque references only: no email, display name, or token."""
        return {
            "user_ref": "usr_ref_" + hashlib.sha256(self.user_id.encode()).hexdigest()[:12],
            "workspace_ref": "ws_ref_" + hashlib.sha256(self.workspace_id.encode()).hexdigest()[:12],
        }


@dataclass
class Target:
    target_id: str
    application: str
    fingerprint: str
    description: dict[str, Any]


@dataclass
class Observation:
    observation_id: str
    kind: str  # CLOUD_BROWSER_FRAME | DESKTOP_FRAME | UIA_SNAPSHOT
    created_mono: float
    captured_at: str
    frame_sha256: str | None
    uia_sha256: str | None
    targets: dict[str, Target]
    summary: dict[str, Any]
    image: bytes | None = None
    media_type: str | None = None


@dataclass
class Operation:
    operation_id: str
    observation_id: str
    target_id: str
    intent_digest: str
    task_digest: str
    task_length: int
    action: dict[str, Any]
    route: str
    pack: dict[str, Any]
    created_mono: float


class ResolveGateway:
    def __init__(self, config: GatewayConfig, *, client: ResolveClient | None = None, clock: Callable[[], float] = time.monotonic):
        self.config = config
        self.client = client or ResolveClient(config)
        self._clock = clock
        self._identity: Identity | None = None
        self._identity_checked: float = -1e9
        self._observations: OrderedDict[str, Observation] = OrderedDict()
        self._operations: dict[str, Operation] = {}
        self._calls: deque[float] = deque()

    # ------------------------------------------------------------------ infrastructure
    def _rate_limit(self) -> None:
        now = self._clock()
        while self._calls and now - self._calls[0] > 60:
            self._calls.popleft()
        if len(self._calls) >= self.config.rate_per_minute:
            raise RateLimitedError("too many calls to the RESOLVE gateway; retry later")
        self._calls.append(now)

    def identity(self, *, refresh: bool = False) -> Identity:
        """Tenant identity as RESOLVE reports it for the bound credential. Never taken from tool arguments."""
        now = self._clock()
        if not refresh and self._identity is not None and now - self._identity_checked < 30:
            return self._identity
        data = self.client.get("/api/auth/me").data
        if not isinstance(data, dict) or not data.get("user_id") or not data.get("workspace_id"):
            raise AuthError("RESOLVE returned no tenant identity for this credential")
        expires = data.get("expires_at")
        if isinstance(expires, (int, float)) and expires <= time.time():
            raise AuthError("the RESOLVE session has expired; issue a new session and update the credential file")
        self._identity = Identity(str(data["user_id"]), str(data["workspace_id"]), float(expires) if isinstance(expires, (int, float)) else None)
        self._identity_checked = now
        return self._identity

    def _authed(self, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Run a backend call; on a 401, re-read the credential once (reconnect) and retry once."""
        try:
            self.identity()
            return fn()
        except AuthError:
            self._identity = None
            self.identity(refresh=True)  # raises AuthError again if the rotated credential is still invalid
            return fn()

    def _observation(self, observation_id: str, *, fresh: bool = True) -> Observation:
        obs = self._observations.get(observation_id)
        if obs is None:
            raise InvalidInputError("unknown observation_id: observations are issued only by this gateway; observe first")
        if fresh and self._is_stale(obs):
            raise StaleError("observation is older than the gateway freshness bound; observe again", details={"observation_id": observation_id})
        return obs

    def _is_stale(self, obs: Observation) -> bool:
        return self._clock() - obs.created_mono > self.config.observation_ttl_s

    def _store_observation(self, obs: Observation) -> None:
        self._observations[obs.observation_id] = obs
        while len(self._observations) > MAX_OBSERVATIONS:
            self._observations.popitem(last=False)

    def safe(self, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Turn any outcome into a structured, token-free result. Never raises into the MCP transport."""
        try:
            self._rate_limit()
            result = fn()
            return {"ok": True, **result}
        except GatewayError as exc:
            return exc.to_result()
        except Exception as exc:  # noqa: BLE001 - last resort, must not leak internals
            log.error("internal gateway error: %s", type(exc).__name__)
            return {"ok": False, "status": "INTERNAL_ERROR", "error": {"code": "INTERNAL_ERROR", "message": "gateway internal error; see stderr diagnostics"}}

    # ------------------------------------------------------------------ status / identity / capabilities
    def status(self) -> dict[str, Any]:
        health = self.client.get("/api/health").data
        readiness: dict[str, Any] | None
        try:
            ready = self.client.get("/api/readyz").data
            readiness = {"ready": bool(ready.get("ready")), "optional": ready.get("optional", {})}
        except GatewayError:
            readiness = None
        return {
            "service": health.get("name", "RESOLVE"),
            "reachable": True,
            "mode": health.get("mode"),
            "version": health.get("version"),
            "features": health.get("features", {}),
            "readiness": readiness,
            "gateway_version": __version__,
        }

    def session(self) -> dict[str, Any]:
        """Current session, redacted: opaque refs and expiry only."""
        def run() -> dict[str, Any]:
            ident = self.identity()
            return {"authenticated": True, **ident.ref(), "expires_at": _iso(ident.expires_at)}
        return self._authed(run)

    def capabilities(self) -> dict[str, Any]:
        """Each capability is probed against RESOLVE. Nothing is reported AVAILABLE without a live answer."""
        def probe(path: str) -> tuple[str, str]:
            try:
                resp = self.client.get(path)
                return "PROBED_OK", f"HTTP {resp.status}"
            except UnavailableError as exc:
                return "PROBED_UNAVAILABLE", str(exc)
            except BlockedError as exc:
                return "PROBED_BLOCKED", str(exc)

        def run() -> dict[str, Any]:
            caps: list[dict[str, Any]] = []
            ident = self.identity()
            caps.append({"capability": "tenant_identity", "status": "AVAILABLE", "measured": True, "reason": "bound credential resolved"})
            state, why = probe("/api/sandboxes")
            caps.append({"capability": "cloud_browser_observe", "status": "AVAILABLE" if state == "PROBED_OK" else "UNAVAILABLE", "measured": True,
                         "reason": "sandbox listing answered; observe a sandbox_id RESOLVE created for this user" if state == "PROBED_OK" else why,
                         "endpoint": "GET /api/sandboxes/{id}/frame"})
            caps.append({"capability": "cloud_browser_evidence", "status": caps[-1]["status"], "measured": True, "reason": "frames are the evidence artifact", "endpoint": "gateway registry"})
            state, why = probe("/api/windows")
            caps.append({"capability": "desktop_windows", "status": "AVAILABLE" if state == "PROBED_OK" else "UNAVAILABLE", "measured": True, "reason": why if state != "PROBED_OK" else "window list answered", "endpoint": "GET /api/windows"})
            uia = self.client.get("/api/uia/availability").data
            uia_ok = bool(isinstance(uia, dict) and uia.get("available"))
            caps.append({"capability": "desktop_uia", "status": "AVAILABLE" if uia_ok else "UNAVAILABLE", "measured": True,
                         "reason": "UI Automation adapter reports available" if uia_ok else str(uia.get("reason", "not available")) if isinstance(uia, dict) else "not available",
                         "endpoint": "GET /api/uia/availability"})
            caps.append({"capability": "action_execution", "status": "UNAVAILABLE", "measured": True,
                         "reason": "no RESOLVE route can prove a human approval for an external-agent request; execution is refused until one exists"})
            caps.append({"capability": "emergency_stop", "status": "UNVERIFIED", "measured": False,
                         "reason": "POST-only route; not probed to avoid side effects", "endpoint": "POST /api/emergency-stop"})
            return {"identity": ident.ref(), "capabilities": caps}

        return self._authed(run)

    # ------------------------------------------------------------------ desktop (Windows adapter via RESOLVE)
    def list_windows(self) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            data = self.client.get("/api/windows").data
            items = data.get("windows", []) if isinstance(data, dict) else data
            windows = []
            for item in (items or [])[:MAX_LIST]:
                if isinstance(item, dict):
                    windows.append({"title": clean_text(item.get("title")), "process": clean_text(item.get("process", item.get("process_name", "")), 80), "visible": bool(item.get("visible", True))})
            return {"status": "OBSERVED", "windows": windows, "count": len(windows), "trust": UNTRUSTED}
        return self._authed(run)

    def _desktop_snapshot_request(self, mission_id: str, intent_revision: int, hwnd: int, snapshot_id: str) -> dict[str, Any]:
        return {"mission_id": mission_id, "intent_revision": intent_revision, "hwnd": hwnd, "snapshot_id": snapshot_id}

    def observe_desktop(self, mission_id: str, intent_revision: int, hwnd: int, snapshot_id: str) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            data = self.client.post("/api/windows/capture", self._desktop_snapshot_request(mission_id, intent_revision, hwnd, snapshot_id)).data
            frame = data.get("frame") or data.get("image") if isinstance(data, dict) else None
            image, media = decode_data_uri(frame)
            if len(image) > self.config.max_frame_bytes:
                raise BlockedError("frame exceeds the gateway size bound", code="FRAME_TOO_LARGE")
            obs = Observation(
                observation_id="obs_" + secrets.token_hex(12), kind="DESKTOP_FRAME", created_mono=self._clock(),
                captured_at=_iso(data.get("timestamp")) or utc_now(), frame_sha256=sha256_hex(image), uia_sha256=None,
                targets={}, summary={"window_title": clean_text(data.get("title")), "trust": UNTRUSTED}, image=image, media_type=media)
            self._store_observation(obs)
            return {"status": "OBSERVED", "observation_id": obs.observation_id, "captured_at": obs.captured_at, "frame_sha256": obs.frame_sha256, "bytes": len(image), "trust": UNTRUSTED}
        return self._authed(run)

    def inspect_window(self, mission_id: str, intent_revision: int, hwnd: int, snapshot_id: str) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            raw = self.client.post("/api/uia/inspect", self._desktop_snapshot_request(mission_id, intent_revision, hwnd, snapshot_id)).data
            if not isinstance(raw, dict):
                raise BackendError("RESOLVE returned no UI Automation snapshot")
            uia_sha = canonical_digest(raw)
            controls: dict[str, Target] = {}
            listed = []
            for el in (raw.get("elements") or raw.get("controls") or [])[:MAX_UIA]:
                if not isinstance(el, dict):
                    continue
                name, role, auto_id = clean_text(el.get("name")), clean_text(el.get("role") or el.get("control_type"), 60), clean_text(el.get("automation_id"), 120)
                tid = stable_target_id("uia", hwnd, role, auto_id, name)
                fingerprint = canonical_digest({"role": role, "automation_id": auto_id, "name": name})
                controls[tid] = Target(tid, clean_text(raw.get("application") or raw.get("title"), 120), fingerprint, {"role": role, "name": name})
                listed.append({"target_id": tid, "name": name, "role": role, "automation_id": auto_id})
            obs = Observation(
                observation_id="uia_" + secrets.token_hex(12), kind="UIA_SNAPSHOT", created_mono=self._clock(),
                captured_at=_iso(raw.get("timestamp")) or utc_now(), frame_sha256=None, uia_sha256=uia_sha, targets=controls,
                summary={"element_count": len(listed), "trust": UNTRUSTED})
            self._store_observation(obs)
            return {"status": "OBSERVED", "observation_id": obs.observation_id, "captured_at": obs.captured_at, "uia_snapshot_sha256": uia_sha,
                    "element_count": len(listed), "elements": listed[:MAX_LIST], "trust": UNTRUSTED}
        return self._authed(run)

    def find_controls(self, observation_id: str, name_contains: str = "", role: str = "") -> dict[str, Any]:
        obs = self._observation(observation_id)
        if obs.kind != "UIA_SNAPSHOT":
            raise InvalidInputError("find_controls needs a UI Automation observation")
        needle, want_role = name_contains.lower().strip(), role.lower().strip()
        matches = []
        for tid, t in obs.targets.items():
            if needle and needle not in str(t.description.get("name", "")).lower():
                continue
            if want_role and want_role != str(t.description.get("role", "")).lower():
                continue
            matches.append({"target_id": tid, "name": t.description.get("name"), "role": t.description.get("role")})
        return {"status": "OBSERVED", "observation_id": obs.observation_id, "matches": matches[:MAX_LIST], "match_count": len(matches), "stale": self._is_stale(obs), "trust": UNTRUSTED}

    # ------------------------------------------------------------------ cloud browser (RESOLVE Playwright sandbox)
    def observe_cloud_browser(self, sandbox_id: str) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            data = self.client.get(f"/api/sandboxes/{sandbox_id}/frame").data
            if not isinstance(data, dict):
                raise BackendError("RESOLVE returned no browser frame")
            image, media = decode_data_uri(data.get("frame"))
            if len(image) > self.config.max_frame_bytes:
                raise BlockedError("frame exceeds the gateway size bound", code="FRAME_TOO_LARGE")
            url = clean_text(data.get("url"), 500)
            origin = "/".join(url.split("/")[:3]) if "://" in url else url[:80]
            application = "Chromium (RESOLVE cloud browser)"
            target = Target(stable_target_id("cloud", sandbox_id, origin), application,
                            canonical_digest({"sandbox_id": sandbox_id, "origin": origin, "title": clean_text(data.get("title"))}),
                            {"origin": origin})
            obs = Observation(
                observation_id="obs_" + secrets.token_hex(12), kind="CLOUD_BROWSER_FRAME", created_mono=self._clock(),
                captured_at=_iso(data.get("timestamp")) or utc_now(), frame_sha256=sha256_hex(image), uia_sha256=None,
                targets={target.target_id: target},
                summary={"sandbox_id": sandbox_id, "url": url, "title": clean_text(data.get("title")), "trust": UNTRUSTED}, image=image, media_type=media)
            self._store_observation(obs)
            viewport = data.get("viewport") if isinstance(data.get("viewport"), dict) else {}
            obs.summary["viewport"] = {"width": _int(viewport.get("width")), "height": _int(viewport.get("height"))}
            return {
                "status": "OBSERVED", "observation_id": obs.observation_id, "captured_at": obs.captured_at,
                "captured_at_source": "backend" if data.get("timestamp") else "gateway", "frame_sha256": obs.frame_sha256,
                "frame_bytes": len(image), "media_type": media, "url": url, "title": clean_text(data.get("title")),
                "session": {"is_paused": str(data.get("is_paused")).lower() == "true", "last_action": clean_text(data.get("last_action"), 120)},
                "viewport": {"width": _int(viewport.get("width")), "height": _int(viewport.get("height"))},
                "targets": [{"target_id": target.target_id, "application": application, "fingerprint": target.fingerprint}],
                "method": "DOM_LEVEL_PLAYWRIGHT_FRAME", "trust": UNTRUSTED,
            }
        return self._authed(run)

    def capture_evidence(self, observation_id: str, include_image: bool = False) -> dict[str, Any]:
        obs = self._observation(observation_id, fresh=False)
        out = {"status": "EVIDENCE", "evidence_ref": obs.observation_id, "kind": obs.kind, "captured_at": obs.captured_at,
               "stale": self._is_stale(obs), "digest": obs.frame_sha256 or obs.uia_sha256, "bytes": len(obs.image or b""),
               "media_type": obs.media_type, "path": None, "trust": UNTRUSTED}
        if include_image:
            if obs.image is None or len(obs.image) > IMAGE_INLINE_LIMIT:
                raise BlockedError("image is absent or larger than the inline bound; use the digest reference", code="IMAGE_NOT_INLINED")
            out["image_base64"] = base64.b64encode(obs.image).decode("ascii")
        return out

    # ------------------------------------------------------------------ proposal / status / execution
    def propose_action(self, task: str, observation_id: str, target_id: str, action_kind: str,
                       x: int | None = None, y: int | None = None, text: str | None = None) -> dict[str, Any]:
        task = task.strip()
        if not 1 <= len(task) <= 2000:
            raise InvalidInputError("task must be 1 to 2000 characters")
        if action_kind not in {"CLICK", "TYPE"}:
            raise InvalidInputError("action_kind must be CLICK or TYPE")
        obs = self._observation(observation_id)  # rejects unknown and stale observations
        target = obs.targets.get(target_id)
        if target is None:
            raise BlockedError("target is not present in this observation (ambiguous or no longer present)", code="TARGET_NOT_IN_OBSERVATION")
        action: dict[str, Any] = {"kind": action_kind}
        if action_kind == "CLICK":
            if not (isinstance(x, int) and isinstance(y, int) and 0 <= x < 10000 and 0 <= y < 10000):
                raise InvalidInputError("CLICK needs integer x and y")
            viewport = obs.summary.get("viewport") or {}
            w, h = viewport.get("width"), viewport.get("height")
            if isinstance(w, int) and isinstance(h, int) and not (x < w and y < h):
                raise BlockedError("click point is outside the observed viewport", code="OUTSIDE_VIEWPORT")
            action.update({"x": x, "y": y})
            route = "/api/cloud-browser/click"
        else:
            if not text or len(text) > 500:
                raise InvalidInputError("TYPE needs text of 1 to 500 characters")
            action["text_sha256"] = sha256_hex(text.encode("utf-8"))  # the typed text itself is not stored or logged
            route = "/api/cloud-browser/type"
        task_digest = sha256_hex(task.encode("utf-8"))
        intent_digest = canonical_digest({
            "task_digest": task_digest, "observation_id": obs.observation_id, "frame_sha256": obs.frame_sha256,
            "target_id": target_id, "target_fingerprint": target.fingerprint, "action": action,
        })
        builder = ProofPackBuilder(task_digest=task_digest, task_length=len(task), environment="CLOUD_BROWSER")
        pack = builder.build(
            status="AWAITING_APPROVAL", intent_digest=intent_digest, intent_source="external_agent_request", user_approved=False,
            observation=_observation_block(obs), target={"application": target.application, "identity": target_id, "fingerprint": target.fingerprint},
            authorization_decision="PENDING", authorization_checked_via="NONE", method="CLOUD_BROWSER", execution_route="UNAVAILABLE",
            notes=("PROPOSAL_ONLY", "NO_RESOLVE_APPROVAL_RECORD"), evidence=(obs.observation_id,))
        op = Operation(builder.operation_id, obs.observation_id, target_id, intent_digest, task_digest, len(task), action, route, pack, self._clock())
        self._operations[op.operation_id] = op
        return {"status": "AWAITING_APPROVAL", "operation_id": op.operation_id, "intent_digest": intent_digest,
                "approval": "The operation stays AWAITING_APPROVAL until a RESOLVE user approves this exact digest in RESOLVE. The gateway cannot approve it.",
                "proofpack": pack}

    def _operation(self, operation_id: str) -> Operation:
        op = self._operations.get(operation_id)
        if op is None:
            raise InvalidInputError("unknown operation_id")
        return op

    def _refresh_stale(self, op: Operation) -> dict[str, Any]:
        obs = self._observations.get(op.observation_id)
        if op.pack["status"] == "AWAITING_APPROVAL" and (obs is None or self._is_stale(obs)):
            pack = dict(op.pack)
            pack["status"] = "STALE"
            pack["notes"] = sorted(set(pack["notes"]) | {"OBSERVATION_STALE"})
            op.pack = validate_proofpack(pack)
        return op.pack

    def action_status(self, operation_id: str) -> dict[str, Any]:
        op = self._operation(operation_id)
        pack = self._refresh_stale(op)
        return {"status": pack["status"], "operation_id": op.operation_id, "proofpack": pack}

    def execute_approved_action(self, operation_id: str, intent_digest: str) -> dict[str, Any]:
        op = self._operation(operation_id)
        if intent_digest != op.intent_digest:
            pack = dict(op.pack)
            pack["status"] = "BLOCKED"
            pack["notes"] = sorted(set(pack["notes"]) | {"INTENT_DIGEST_MISMATCH"})
            return {"ok": False, "status": "BLOCKED", "operation_id": op.operation_id, "proofpack": validate_proofpack(pack),
                    "error": {"code": "INTENT_DIGEST_MISMATCH", "message": "the digest does not match the proposed operation"}}
        pack = self._refresh_stale(op)
        if pack["status"] == "STALE":
            return {"ok": False, "status": "STALE", "operation_id": op.operation_id, "proofpack": pack,
                    "error": {"code": "STALE", "message": "observation is stale; observe and propose again"}}
        approval = self._resolve_approval_record(op)
        if approval is None:
            # Nothing has been executed. The pack stays AWAITING_APPROVAL, because no RESOLVE approval can be established.
            return {"ok": False, "status": "AWAITING_APPROVAL", "operation_id": op.operation_id, "proofpack": pack,
                    "error": {"code": "NO_RESOLVE_APPROVAL", "message": "no RESOLVE approval record can be verified for this operation; nothing was executed"}}
        raise UnavailableError("RESOLVE approval exists but no execution route is exposed to the gateway")

    def _resolve_approval_record(self, op: Operation) -> dict[str, Any] | None:
        """Look for a RESOLVE-recorded human approval bound to this exact digest. RESOLVE exposes none to the gateway:
        its approve endpoints are themselves the approval act, so calling them here would let the gateway approve for
        the user. Returns None, so the operation remains AWAITING_APPROVAL."""
        return None

    def emergency_stop(self) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            data = self.client.post("/api/emergency-stop", {}).data
            return {"status": data.get("status", "STOPPED") if isinstance(data, dict) else "STOPPED",
                    "scope": "process-wide scheduler of this RESOLVE instance (not tenant-scoped in this build)", "trust": "RESOLVE_BACKEND"}
        return self._authed(run)

    def mission_state(self, mission_id: str) -> dict[str, Any]:
        if not _ID.match(mission_id):
            raise InvalidInputError("mission_id has an invalid format")
        def run() -> dict[str, Any]:
            data = self.client.get(f"/api/brief-missions/{mission_id}").data
            if not isinstance(data, dict):
                raise BackendError("RESOLVE returned no mission state")
            return {"mission_id": mission_id, "state": clean_text(data.get("state"), 40), "revision": _int(data.get("intent_revision", data.get("revision"))), "trust": "RESOLVE_BACKEND"}
        return self._authed(run)

    def proofpack(self, operation_id: str) -> dict[str, Any]:
        return self._refresh_stale(self._operation(operation_id))

    def observation_index(self) -> list[dict[str, Any]]:
        """Metadata only: no image bytes, no page text beyond the bounded, untrusted summary."""
        return [
            {"observation_id": o.observation_id, "kind": o.kind, "captured_at": o.captured_at, "stale": self._is_stale(o),
             "frame_sha256": o.frame_sha256, "uia_snapshot_sha256": o.uia_sha256, "target_count": len(o.targets), "trust": UNTRUSTED}
            for o in list(self._observations.values())[-MAX_LIST:]
        ]


def _observation_block(obs: Observation) -> dict[str, Any]:
    return {"snapshot_id": obs.observation_id, "captured_at": obs.captured_at, "source": "RESOLVE_BACKEND",
            "frame_sha256": obs.frame_sha256, "uia_snapshot_sha256": obs.uia_sha256}


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
