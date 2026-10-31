"""Gateway configuration: base URL and credential come from the environment or a private file.

Nothing here hardcodes a token or a user/workspace identity. Tokens are never included in ``repr``,
logs, tool output or resources. A token file is re-read on reconnect, so an operator can rotate the
credential without restarting the MCP client.
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import urlsplit

from .errors import AuthConfigError

DEFAULT_BASE_URL = "http://127.0.0.1:8765"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
TOKEN_PREFIX = "rst_"  # RESOLVE tenant session tokens (src/tenant_auth.py). Demo fallbacks are never accepted.


def validate_base_url(raw: str) -> str:
    """Accept http only for loopback; anything else must be https. No credentials, query or path in the URL."""
    url = (raw or "").strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise AuthConfigError("RESOLVE_MCP_BASE_URL must be an absolute http(s) URL")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise AuthConfigError("RESOLVE_MCP_BASE_URL must not contain credentials, query or fragment")
    if parts.scheme == "http" and parts.hostname not in LOOPBACK_HOSTS:
        raise AuthConfigError("plain http is allowed only for loopback RESOLVE; use https for remote hosts")
    if parts.path not in {"", "/"}:
        raise AuthConfigError("RESOLVE_MCP_BASE_URL must be the service origin, without a path")
    return f"{parts.scheme}://{parts.netloc}"


def read_token_file(path: str) -> str:
    """Read a credential file that only its owner can read (mode 0600 or stricter)."""
    try:
        st = os.stat(path)
    except OSError as exc:
        raise AuthConfigError(f"token file is not readable: {exc.strerror}") from None
    if not stat.S_ISREG(st.st_mode):
        raise AuthConfigError("token file must be a regular file")
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise AuthConfigError("token file must not be group- or world-accessible (use chmod 600)")
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read().strip()


def validate_token(token: str) -> str:
    if not token:
        raise AuthConfigError("missing RESOLVE credential: set RESOLVE_MCP_TOKEN_FILE (preferred) or RESOLVE_MCP_TOKEN")
    if not token.startswith(TOKEN_PREFIX) or len(token) < 24 or any(c.isspace() for c in token):
        raise AuthConfigError("the RESOLVE credential must be a tenant session token (rst_...)")
    return token


@dataclass(frozen=True)
class GatewayConfig:
    base_url: str
    token_file: str | None = None
    static_token: str | None = field(default=None, repr=False)
    timeout_s: float = 10.0
    max_response_bytes: int = 4_000_000
    max_frame_bytes: int = 2_000_000
    observation_ttl_s: float = 60.0
    rate_per_minute: int = 120

    def __repr__(self) -> str:  # never print the credential or its location
        return f"GatewayConfig(base_url={self.base_url!r}, credential=<redacted>, timeout_s={self.timeout_s})"

    def read_token(self) -> str:
        """Current credential. Re-read from the token file on every call so rotation takes effect on reconnect."""
        if self.token_file:
            return validate_token(read_token_file(self.token_file))
        return validate_token(self.static_token or "")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "GatewayConfig":
        env = os.environ if env is None else env
        token_file = (env.get("RESOLVE_MCP_TOKEN_FILE") or "").strip() or None
        static = (env.get("RESOLVE_MCP_TOKEN") or "").strip() or None
        config = cls(
            base_url=validate_base_url(env.get("RESOLVE_MCP_BASE_URL", DEFAULT_BASE_URL)),
            token_file=token_file,
            static_token=None if token_file else static,
            timeout_s=_bounded_float(env, "RESOLVE_MCP_TIMEOUT_S", 10.0, 1.0, 60.0),
            observation_ttl_s=_bounded_float(env, "RESOLVE_MCP_OBSERVATION_TTL_S", 60.0, 5.0, 900.0),
            rate_per_minute=int(_bounded_float(env, "RESOLVE_MCP_RATE_PER_MIN", 120, 10, 6000)),
        )
        config.read_token()  # fail at startup, not on the first tool call
        return config


def _bounded_float(env: Mapping[str, str], key: str, default: float, low: float, high: float) -> float:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise AuthConfigError(f"{key} must be a number") from None
    if not low <= value <= high:
        raise AuthConfigError(f"{key} must be between {low} and {high}")
    return value
