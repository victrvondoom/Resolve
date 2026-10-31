"""Typed isolation-provider contract for sandbox command execution.

Executing a user-supplied command is only permitted through a provider whose report says it is production-ready AND
enforces every isolation property below. RESOLVE ships NO such provider. The default is UnavailableProvider, which
refuses every execution before any process is created.

There is deliberately NO provider that runs commands directly on the application host. A working directory and an
environment dictionary are not isolation: the command still runs as the server user, with the server's file access.
"""
from __future__ import annotations

from dataclasses import dataclass


class ExecutionBlocked(Exception):
    """Execution was refused by policy or because no isolated provider is available. No process was started."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ProviderReport:
    name: str
    isolation_type: str                 # "none" | "container" | "microvm" | ...
    production_ready: bool
    filesystem_isolated: bool
    network_policy_enforced: bool
    resource_limits_enforced: bool      # CPU, memory, process count, disk, output size, wall time
    process_cleanup_enforced: bool
    available: bool
    reason: str

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class IsolationProvider:
    """Subclasses implement report() truthfully and run(). run() must not be called unless report().production_ready."""

    def report(self) -> ProviderReport:
        raise NotImplementedError

    def run(self, sandbox_root: str, command: list[str], timeout_s: int, output_limit_bytes: int) -> dict:
        raise NotImplementedError


class UnavailableProvider(IsolationProvider):
    """The only provider in production today. It refuses execution and says exactly why."""

    def report(self) -> ProviderReport:
        return ProviderReport(
            name="unavailable", isolation_type="none", production_ready=False, filesystem_isolated=False,
            network_policy_enforced=False, resource_limits_enforced=False, process_cleanup_enforced=False, available=False,
            reason="No isolated command-execution provider is configured. Commands are not run on the application host.")

    def run(self, sandbox_root: str, command: list[str], timeout_s: int, output_limit_bytes: int) -> dict:
        raise ExecutionBlocked(self.report().reason)


PROVIDERS = {"unavailable": UnavailableProvider}


def select_provider(name: str | None) -> IsolationProvider:
    """RESOLVE_SANDBOX_PROVIDER. Unset means unavailable. An unknown name is a startup error, never a silent fallback."""
    key = (name or "unavailable").strip().lower()
    if key not in PROVIDERS:
        raise ValueError(f"RESOLVE_SANDBOX_PROVIDER={name!r} is not a known provider; known: {sorted(PROVIDERS)}")
    return PROVIDERS[key]()
