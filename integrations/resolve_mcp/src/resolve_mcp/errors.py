"""Typed gateway errors. Every error maps to a structured, token-free result for the MCP client."""
from __future__ import annotations

from typing import Any


class GatewayError(Exception):
    """A refusal or failure the MCP client can act on. ``code`` is stable; ``message`` is safe to show."""

    code = "GATEWAY_ERROR"

    def __init__(self, message: str, *, code: str | None = None, details: dict[str, Any] | None = None):
        super().__init__(message)
        if code:
            self.code = code
        self.details = details or {}

    def to_result(self) -> dict[str, Any]:
        return {"ok": False, "status": self.code, "error": {"code": self.code, "message": str(self)}, **({"details": self.details} if self.details else {})}


class AuthConfigError(GatewayError):
    """Credential or base URL is missing or malformed. Raised before any network call."""

    code = "AUTH_CONFIG_INVALID"


class AuthError(GatewayError):
    """RESOLVE rejected the session (missing, invalid, revoked or expired)."""

    code = "AUTH_INVALID"


class UnavailableError(GatewayError):
    """The capability is not available on this RESOLVE deployment or host. This is not a failure of the request."""

    code = "UNAVAILABLE"


class StaleError(GatewayError):
    code = "STALE"


class BlockedError(GatewayError):
    code = "BLOCKED"


class InvalidInputError(GatewayError):
    code = "INVALID_INPUT"


class RateLimitedError(GatewayError):
    code = "RATE_LIMITED"


class BackendError(GatewayError):
    code = "BACKEND_ERROR"


class TimeoutError_(GatewayError):
    code = "TIMEOUT"
