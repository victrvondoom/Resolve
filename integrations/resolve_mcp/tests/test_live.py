"""Live tests against a RESOLVE server you started yourself.

They skip (with a stated environment class) unless these are set:
    RESOLVE_LIVE_BASE_URL   e.g. http://127.0.0.1:8800
    RESOLVE_LIVE_TOKEN      an rst_ tenant session token for a test account on that server

They never create demo data for you and never call approve/execute routes.
"""
import base64
import hashlib

import pytest

from resolve_mcp.client import ResolveClient
from resolve_mcp.gateway import ResolveGateway


@pytest.fixture
def live(live_config):
    return ResolveGateway(live_config)


def test_live_status_and_readiness(live):  # 074
    res = live.safe(live.status)
    assert res["ok"] is True, res
    assert res["mode"] in {"demo", "production"}


def test_live_identity_and_capabilities_are_probed(live):  # 040/074
    res = live.safe(live.capabilities)
    assert res["ok"] is True, res
    caps = {c["capability"]: c for c in res["capabilities"]}
    assert caps["tenant_identity"]["status"] == "AVAILABLE"
    assert caps["action_execution"]["status"] == "UNAVAILABLE"
    assert caps["emergency_stop"]["measured"] is False


def test_live_expired_or_revoked_credential_is_refused(live_config):  # 066 against the real backend
    bad = ResolveGateway(type(live_config)(base_url=live_config.base_url, static_token="rst_" + "0" * 40))
    res = bad.safe(bad.session)
    assert res["ok"] is False and res["error"]["code"] == "AUTH_INVALID"


def test_live_desktop_is_honestly_unavailable_on_this_host(live):  # 077/034
    res = live.safe(live.list_windows)
    if res["ok"]:
        pytest.skip("this host exposes a desktop adapter; desktop behaviour is measured elsewhere")
    assert res["error"]["code"] == "UNAVAILABLE"


def test_live_cloud_browser_real_frame_and_evidence(live_config):  # 075
    """Creates a sandbox for the test account, observes its real Chromium frame, verifies JPEG bytes and digest."""
    import json
    client = ResolveClient(live_config)
    sandbox = client.post("/api/sandboxes", {}).data
    sbx = sandbox["id"]
    gw = ResolveGateway(live_config)
    obs = gw.safe(lambda: gw.observe_cloud_browser(sbx))
    assert obs["ok"] is True, obs
    assert obs["media_type"] == "image/jpeg"
    ev = gw.safe(lambda: gw.capture_evidence(obs["observation_id"], include_image=True))
    assert ev["ok"] is True
    raw = base64.b64decode(ev["image_base64"])
    assert raw[:2] == b"\xff\xd8", "frame is not a JPEG"
    assert ev["digest"] == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert obs["url"] is not None
    json.dumps(obs)  # serialisable, bounded
    assert "image_base64" not in obs  # observations never carry image bytes


def test_live_propose_then_execution_stays_awaiting_approval(live_config):  # 071/076
    """Real backend: a proposal cannot be executed by the agent, so the operation stays AWAITING_APPROVAL."""
    client = ResolveClient(live_config)
    sbx = client.post("/api/sandboxes", {}).data["id"]
    gw = ResolveGateway(live_config)
    obs = gw.safe(lambda: gw.observe_cloud_browser(sbx))
    prop = gw.safe(lambda: gw.propose_action("Click the page center", obs["observation_id"], obs["targets"][0]["target_id"], "CLICK", x=10, y=10))
    assert prop["ok"] is True and prop["status"] == "AWAITING_APPROVAL"
    res = gw.safe(lambda: gw.execute_approved_action(prop["operation_id"], prop["intent_digest"]))
    assert res["status"] == "AWAITING_APPROVAL" and res["ok"] is False
    assert res["proofpack"]["verification"]["status"] == "NOT_RUN"
