"""Outbound network policy for the cloud browser (SSRF defense).

Pure-Python checks: scheme allow-list, literal-IP and DNS-resolved address
classification. Every resolved address must be globally routable unless the
policy explicitly allows loopback (tests / local demo only).

Residual risk: the browser resolves DNS itself after this check (DNS rebinding
TOCTOU). Production must also enforce egress rules at the sandbox/network
boundary; see docs/SECURITY.md.
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

ALLOWED_SCHEMES = frozenset({"http", "https"})
BLOCKED_HOSTNAMES = frozenset({"metadata.google.internal", "metadata.azure.com", "metadata", "localhost"})


class PolicyViolation(Exception):
    def __init__(self, message: str, status_code: int = 403):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def origin_of(url: str) -> str | None:
    """Normalized "scheme://host[:port]" (default ports dropped, lowercase), or None when the URL has no host."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if not host or not scheme:
        return None
    host = f"[{host}]" if ":" in host else host
    default = {"http": 80, "https": 443}.get(scheme)
    return f"{scheme}://{host}" + (f":{port}" if port and port != default else "")


@dataclass(frozen=True)
class NetPolicy:
    allow_loopback: bool = False  # tests / explicit local demo only
    # Exact origins (scheme+host+port) that bypass the address check, e.g. the bundled demo web app on the server's own
    # loopback port. A different port, scheme or host is NOT covered; everything else still goes through the full check.
    allowed_origins: frozenset = frozenset()

    def _check_ip(self, ip: ipaddress._BaseAddress, host: str) -> None:
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if ip.is_loopback and self.allow_loopback:
            return
        if not ip.is_global:
            raise PolicyViolation(f"Destination {host} resolves to non-public address {ip}")

    def check_url(self, url: str, resolver=socket.getaddrinfo) -> str:
        parsed = urlparse(url)
        if parsed.scheme.lower() not in ALLOWED_SCHEMES:
            raise PolicyViolation(f"Unsupported URL scheme: {parsed.scheme or '(none)'}", 400)
        if parsed.username is not None or parsed.password is not None:
            raise PolicyViolation("URLs with embedded credentials are not allowed", 400)   # blocks http://allowed@evil/ tricks
        host = (parsed.hostname or "").lower().rstrip(".")
        if not host:
            raise PolicyViolation("URL has no host", 400)
        if self.allowed_origins and origin_of(url) in {o.lower().rstrip("/") for o in self.allowed_origins}:
            return url
        if host in BLOCKED_HOSTNAMES and not (self.allow_loopback and host == "localhost"):
            raise PolicyViolation(f"Host {host} is forbidden by policy")
        try:
            self._check_ip(ipaddress.ip_address(host), host)
            return url
        except ValueError:
            pass
        try:
            infos = resolver(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except OSError as exc:
            raise PolicyViolation(f"Cannot resolve {host}: {exc}", 502)
        if not infos:
            raise PolicyViolation(f"Cannot resolve {host}", 502)
        for info in infos:
            self._check_ip(ipaddress.ip_address(info[4][0]), host)
        return url
