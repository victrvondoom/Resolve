"""``resolve-mcp selftest``: verifies the environment, the RESOLVE connection, the session and tool discovery.

Prints one line per check. It never prints the credential, the token file path, or any tenant data
beyond opaque references. Exit status is 0 only when every required check passes.
"""
from __future__ import annotations

import asyncio
import platform
import sys
from dataclasses import dataclass
from typing import TextIO

from .config import GatewayConfig
from .errors import GatewayError
from .gateway import ResolveGateway
from .server import build_server

REQUIRED_TOOLS = {
    "resolve_status", "resolve_get_capabilities", "resolve_list_windows", "resolve_observe_desktop", "resolve_inspect_window",
    "resolve_find_controls", "resolve_observe_cloud_browser", "resolve_capture_evidence", "resolve_propose_action",
    "resolve_get_action_status", "resolve_execute_approved_action", "resolve_emergency_stop",
}


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def run_selftest(config: GatewayConfig | None, out: TextIO) -> int:
    checks: list[Check] = []

    def record(name: str, ok: bool, detail: str) -> None:
        checks.append(Check(name, ok, detail))
        out.write(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}\n")

    record("python_version", sys.version_info >= (3, 10), f"{platform.python_version()} (requires >= 3.10)")
    try:
        from importlib.metadata import version as pkg_version
        sdk = pkg_version("mcp")
        record("mcp_sdk", sdk.startswith("1.") and sdk >= "1.30.0", f"mcp {sdk} (pinned line >=1.30,<2)")
    except Exception:  # noqa: BLE001
        record("mcp_sdk", False, "mcp SDK is not installed")
    if config is None:
        record("credential_configured", False, "no valid credential: set RESOLVE_MCP_TOKEN_FILE (preferred) or RESOLVE_MCP_TOKEN")
        return _finish(checks, out)
    record("credential_configured", True, "credential source present (value not shown)")

    gateway = ResolveGateway(config)
    try:
        status = gateway.status()
        record("resolve_reachable", True, f"{status.get('service')} mode={status.get('mode')} version={status.get('version')}")
    except GatewayError as exc:
        record("resolve_reachable", False, str(exc))
        return _finish(checks, out)

    try:
        ident = gateway.identity()
        record("session_authorized", True, "RESOLVE accepted the bound credential")
        record("tenant_identity_resolved", True, f"workspace_ref={ident.ref()['workspace_ref']}")
    except GatewayError as exc:
        record("session_authorized", False, str(exc))
        record("tenant_identity_resolved", False, "skipped: no authorized session")
        return _finish(checks, out)

    try:
        caps = gateway.capabilities()["capabilities"]
        summary = ", ".join(f"{c['capability']}={c['status']}" for c in caps)
        record("capabilities_probed", True, summary)
    except GatewayError as exc:
        record("capabilities_probed", False, str(exc))

    try:
        tools = asyncio.run(_list_tool_names(gateway))
        missing = sorted(REQUIRED_TOOLS - tools)
        record("mcp_tool_discovery", not missing, f"{len(tools)} tools" + (f"; missing {missing}" if missing else ""))
    except Exception as exc:  # noqa: BLE001
        record("mcp_tool_discovery", False, type(exc).__name__)
    return _finish(checks, out)


async def _list_tool_names(gateway: ResolveGateway) -> set[str]:
    server = build_server(gateway)
    return {tool.name for tool in await server.list_tools()}


def _finish(checks: list[Check], out: TextIO) -> int:
    failed = [c.name for c in checks if not c.ok]
    out.write("SELFTEST " + ("PASS" if not failed else "FAIL: " + ", ".join(failed)) + "\n")
    return 0 if not failed else 1
