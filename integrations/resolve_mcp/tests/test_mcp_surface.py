"""MCP-level tests: an in-process MCP client discovers and invokes the gateway; a real STDIO subprocess serves it."""
import json
import os
import sys
from pathlib import Path

import pytest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

from fake_resolve import GOOD_TOKEN
from resolve_mcp.server import build_server

EXPECTED_TOOLS = {
    "resolve_status", "resolve_get_capabilities", "resolve_list_windows", "resolve_observe_desktop", "resolve_inspect_window",
    "resolve_find_controls", "resolve_observe_cloud_browser", "resolve_capture_evidence", "resolve_propose_action",
    "resolve_get_action_status", "resolve_execute_approved_action", "resolve_emergency_stop",
}
ROOT = Path(__file__).resolve().parents[1]


def _text(result) -> dict:
    return json.loads(result.content[0].text)


@pytest.mark.anyio
async def test_in_process_client_discovers_all_tools_resources_and_prompts(gateway):  # 073
    server = build_server(gateway)
    async with create_connected_server_and_client_session(server._mcp_server) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools.tools}
        assert EXPECTED_TOOLS <= names
        for t in tools.tools:  # no identity or approval parameters exposed to any model
            props = set((t.inputSchema or {}).get("properties", {}))
            assert not props & {"user_id", "workspace_id", "approved", "approval", "token"}, t.name
        prompts = {p.name for p in (await client.list_prompts()).prompts}
        assert {"explain_current_screen", "inspect_application", "plan_computer_task", "review_execution_evidence"} <= prompts
        templates = {t.uriTemplate for t in (await client.list_resource_templates()).resourceTemplates}
        assert "resolve://proofpacks/{operation_id}" in templates


@pytest.mark.anyio
async def test_in_process_end_to_end_observe_propose_status_execute(make_gateway, fake):  # 074/076 against the fake backend
    server = build_server(make_gateway())
    async with create_connected_server_and_client_session(server._mcp_server) as client:
        status = _text(await client.call_tool("resolve_status", {}))
        assert status["ok"] is True and status["mode"] == "demo"
        obs = _text(await client.call_tool("resolve_observe_cloud_browser", {"sandbox_id": "sbx_0123456789ab"}))
        assert obs["ok"] is True and obs["frame_sha256"].startswith("sha256:")
        prop = _text(await client.call_tool("resolve_propose_action", {
            "task": "Click save", "observation_id": obs["observation_id"], "target_id": obs["targets"][0]["target_id"],
            "action_kind": "CLICK", "x": 5, "y": 5}))
        assert prop["status"] == "AWAITING_APPROVAL"
        res = _text(await client.call_tool("resolve_execute_approved_action", {
            "operation_id": prop["operation_id"], "intent_digest": prop["intent_digest"]}))
        assert res["status"] == "AWAITING_APPROVAL" and res["ok"] is False
        resource = await client.read_resource(f"resolve://proofpacks/{prop['operation_id']}")
        assert json.loads(resource.contents[0].text)["proofpack"]["status"] == "AWAITING_APPROVAL"
        prompt = await client.get_prompt("review_execution_evidence", {"operation_id": prop["operation_id"]})
        assert "untrusted" in prompt.messages[0].content.text.lower() or "requested" in prompt.messages[0].content.text


@pytest.mark.anyio
async def test_tool_arguments_cannot_inject_identity(make_gateway, fake):  # 067 through the MCP surface
    server = build_server(make_gateway())
    async with create_connected_server_and_client_session(server._mcp_server) as client:
        res = _text(await client.call_tool("resolve_status", {"user_id": "usr_victim", "workspace_id": "ws_victim"}))
        assert res["ok"] is True
        assert not any("usr_victim" in json.dumps(b) or "ws_victim" in json.dumps(b) for _m, _p, b, _h in fake.requests)
        sess = _text(await client.call_tool("resolve_get_capabilities", {"workspace_id": "ws_victim"}))
        assert sess["identity"]["workspace_ref"].startswith("ws_ref_")
        assert "ws_victim" not in json.dumps(sess)


@pytest.mark.anyio
async def test_invalid_arguments_are_rejected_by_schema(make_gateway):  # 072 bounds
    server = build_server(make_gateway())
    async with create_connected_server_and_client_session(server._mcp_server) as client:
        bad = await client.call_tool("resolve_observe_cloud_browser", {"sandbox_id": "../../etc/passwd"})
        assert bad.isError is True


def test_resources_are_read_only_and_redact_session(make_gateway):  # 050/056
    import asyncio
    gw = make_gateway()
    server = build_server(gw)

    async def read():
        async with create_connected_server_and_client_session(server._mcp_server) as client:
            res = await client.read_resource("resolve://session")
            return res.contents[0].text

    text = asyncio.run(read())
    assert GOOD_TOKEN not in text and "x@example.invalid" not in text and "usr_test000000000001" not in text
    assert "user_ref" in text


@pytest.mark.anyio
async def test_stdio_server_speaks_clean_json_rpc_on_stdout(fake):  # 013-015
    env = {**os.environ, "RESOLVE_MCP_BASE_URL": fake.base_url, "RESOLVE_MCP_TOKEN": GOOD_TOKEN, "PYTHONPATH": str(ROOT / "src")}
    params = StdioServerParameters(command=sys.executable, args=["-m", "resolve_mcp", "serve"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
            assert EXPECTED_TOOLS <= tools
            status = _text(await session.call_tool("resolve_status", {}))
            assert status["reachable"] is True


def test_stdio_server_refuses_to_start_without_credential(tmp_path):  # 065 at process level
    import subprocess
    env = {k: v for k, v in os.environ.items() if not k.startswith("RESOLVE_MCP_")}
    env["PYTHONPATH"] = str(ROOT / "src")
    proc = subprocess.run([sys.executable, "-m", "resolve_mcp", "serve"], env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 2
    assert proc.stdout == ""  # nothing on the protocol channel
    assert "missing RESOLVE credential" in proc.stderr
