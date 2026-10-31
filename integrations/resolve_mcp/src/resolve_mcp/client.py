"""Thin HTTP client for RESOLVE's existing, documented, authenticated JSON API.

The client never creates a second executor: it only calls routes RESOLVE already serves, with the
tenant session token it was configured with. Response bodies are size-bounded, time-bounded, and are
never written to logs. Status codes are mapped to typed gateway errors.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import GatewayConfig
from .errors import AuthError, BackendError, BlockedError, GatewayError, InvalidInputError, TimeoutError_, UnavailableError

log = logging.getLogger("resolve_mcp.client")


@dataclass(frozen=True)
class Response:
    status: int
    data: Any


class ResolveClient:
    def __init__(self, config: GatewayConfig):
        self._config = config
        self._opener = urllib.request.build_opener(_NoRedirect())

    @property
    def base_url(self) -> str:
        return self._config.base_url

    def _token(self) -> str:
        return self._config.read_token()

    def get(self, path: str, *, max_bytes: int | None = None) -> Response:
        return self._request("GET", path, None, max_bytes)

    def post(self, path: str, body: dict[str, Any] | None = None, *, max_bytes: int | None = None) -> Response:
        return self._request("POST", path, body or {}, max_bytes)

    def _request(self, method: str, path: str, body: dict[str, Any] | None, max_bytes: int | None) -> Response:
        if not path.startswith("/api/"):
            raise InvalidInputError("internal: only /api/ routes may be called")
        limit = max_bytes or self._config.max_response_bytes
        token = self._token()
        headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
        raw = None
        if body is not None:
            raw = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(raw))
        req = urllib.request.Request(self._config.base_url + path, data=raw, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=self._config.timeout_s) as resp:
                status = resp.status
                payload = resp.read(limit + 1)
        except urllib.error.HTTPError as exc:
            status = exc.code
            payload = exc.read(limit + 1) if exc.fp else b""
        except TimeoutError:
            log.info("%s %s timed out", method, path)
            raise TimeoutError_("RESOLVE did not respond in time") from None
        except urllib.error.URLError as exc:
            log.info("%s %s unreachable (%s)", method, path, type(exc.reason).__name__)
            raise UnavailableError("RESOLVE is not reachable at the configured base URL") from None
        log.info("%s %s -> %s", method, path, status)
        if len(payload) > limit:
            raise BlockedError("RESOLVE response exceeded the gateway size bound", code="RESPONSE_TOO_LARGE")
        data = _decode(payload)
        if status >= 400:
            raise self._map_error(status, data, path, token)
        return Response(status=status, data=data)

    def _map_error(self, status: int, data: Any, path: str, token: str) -> GatewayError:
        message = _safe_message(data, token)
        if status == 401:
            return AuthError("RESOLVE rejected the session: " + (message or "authentication required"))
        if status == 403:
            return BlockedError("RESOLVE refused this operation for the authenticated user: " + message, code="FORBIDDEN")
        if status == 404 and "disabled in production" in message:
            return UnavailableError("route is disabled on this RESOLVE deployment: " + message)
        if "unavailable" in message.lower() or "is required" in message.lower():
            # RESOLVE reports host-level absence (for example "Windows control is unavailable") with 409 or 503.
            return UnavailableError("RESOLVE reports the capability is unavailable: " + message)
        if status == 404:
            return UnavailableError("RESOLVE route not found: " + path)
        if status == 409:
            return BlockedError("RESOLVE state conflict: " + message, code="CONFLICT")
        if status == 413:
            return InvalidInputError("request too large for RESOLVE")
        if status in (400, 415, 422):
            return InvalidInputError("RESOLVE rejected the request: " + message)
        if status == 503:
            return UnavailableError("RESOLVE reports the capability is unavailable: " + message)
        return BackendError(f"RESOLVE returned HTTP {status}: " + message)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: a redirect could carry the bearer token to another origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise BlockedError("RESOLVE attempted a redirect; refused to forward the credential", code="REDIRECT_REFUSED")


def _decode(payload: bytes) -> Any:
    if not payload:
        return {}
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"raw_non_json": True}


def _safe_message(data: Any, token: str) -> str:
    text = ""
    if isinstance(data, dict):
        text = str(data.get("error") or data.get("message") or "")
    text = text.replace(token, "<redacted>")
    return text[:300]
