"""Deployment-mode policy, startup validation, rate limiting and response headers.

DEMO mode  : loopback-only, shared local demo token via /api/session (development only).
PRODUCTION : no shared token, no unauthenticated fallback to the demo tenant, legacy
             single-ledger routes disabled, Host header must be in RESOLVE_ALLOWED_HOSTS.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field

LOOPBACK = {"127.0.0.1", "::1", "localhost"}

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "microphone=(self), camera=(), geolocation=()",
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
                                "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-src 'self' blob:; "
                                "object-src 'self' blob:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"),
}


class ConfigError(Exception):
    pass


def client_ip(peer: str, forwarded_for: str, trusted_proxy: bool) -> str:
    """Identify the client for rate limiting. X-Forwarded-For is attacker-controlled unless our own proxy
    appended it, so only the right-most entry is used, and only when a trusted proxy is declared."""
    if trusted_proxy and forwarded_for.strip():
        return forwarded_for.split(",")[-1].strip() or peer
    return peer


class RateLimiter:
    """Sliding-window limiter keyed by (bucket, client); memory bounded by max_keys."""

    def __init__(self, max_keys: int = 10000):
        self._hits: dict[tuple[str, str], list[float]] = {}
        self._lock = threading.Lock()
        self._max_keys = max_keys

    def allow(self, bucket: str, client: str, limit: int, window_s: float = 60.0) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._hits.get((bucket, client), []) if now - t < window_s]
            if len(hits) >= limit:
                self._hits[(bucket, client)] = hits
                return False
            hits.append(now)
            self._hits[(bucket, client)] = hits
            if len(self._hits) > self._max_keys:   # evict least-recently-used keys so spoofed/rotating clients cannot grow memory
                for k in sorted(self._hits, key=lambda key: self._hits[key][-1] if self._hits[key] else 0)[: len(self._hits) - self._max_keys]:
                    del self._hits[k]
            return True


@dataclass(frozen=True)
class ServerSecurity:
    mode: str = "demo"
    bind_host: str = "127.0.0.1"
    allowed_hosts: frozenset = field(default_factory=frozenset)
    auth_rate_per_min: int = 10
    trusted_proxy: bool = False
    cloud_browser_isolation: str = ""   # e.g. 'microvm' | 'container-gvisor'; empty = none declared

    @property
    def production(self) -> bool:
        return self.mode == "production"

    @classmethod
    def from_env(cls, env=os.environ) -> "ServerSecurity":
        hosts = frozenset(h.strip().lower() for h in env.get("RESOLVE_ALLOWED_HOSTS", "").split(",") if h.strip())
        return cls(mode=env.get("RESOLVE_MODE", "demo").lower(), bind_host=env.get("RESOLVE_BIND", "127.0.0.1"),
                   allowed_hosts=hosts, auth_rate_per_min=int(env.get("RESOLVE_AUTH_RATE_PER_MIN", "10")),
                   cloud_browser_isolation=env.get("RESOLVE_CLOUD_BROWSER_ISOLATION", "").strip().lower(),
                   trusted_proxy=env.get("RESOLVE_TRUSTED_PROXY") == "1")

    def validate_startup(self) -> None:
        if self.mode not in {"demo", "production"}:
            raise ConfigError(f"RESOLVE_MODE must be 'demo' or 'production', got {self.mode!r}")
        if self.production:
            if not self.allowed_hosts:
                raise ConfigError("production requires RESOLVE_ALLOWED_HOSTS (comma-separated Host values)")
            if os.environ.get("RESOLVE_ALLOW_BROWSER_LOOPBACK") == "1":
                raise ConfigError("RESOLVE_ALLOW_BROWSER_LOOPBACK must not be set in production")
        elif self.bind_host not in LOOPBACK:
            raise ConfigError("demo mode may only bind to loopback; set RESOLVE_MODE=production with RESOLVE_ALLOWED_HOSTS")

    @property
    def cloud_browser_enabled(self) -> bool:
        return (not self.production) or self.cloud_browser_isolation in {"microvm", "container-gvisor"}

    def host_allowed(self, host_header: str | None, port: int) -> bool:
        host = (host_header or "").lower()
        if self.production:
            return host in self.allowed_hosts
        return host in {f"127.0.0.1:{port}", f"localhost:{port}"}
