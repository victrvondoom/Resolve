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


@dataclass(frozen=True)
class NetPolicy:
    allow_loopback: bool = False  # tests / explicit local demo only

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
        host = (parsed.hostname or "").lower().rstrip(".")
        if not host:
            raise PolicyViolation("URL has no host", 400)
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
