import os

import pytest

from resolve_mcp.config import GatewayConfig, validate_base_url
from resolve_mcp.errors import AuthConfigError
from fake_resolve import GOOD_TOKEN


def test_missing_credential_fails_before_any_network_call():  # 065
    with pytest.raises(AuthConfigError, match="missing RESOLVE credential"):
        GatewayConfig.from_env({"RESOLVE_MCP_BASE_URL": "http://127.0.0.1:8765"})


def test_credential_must_be_tenant_token_not_demo_or_placeholder():  # 065/020
    for bad in ["SESSION-DEMO-TOKEN-123456789", "rst_short", "rst_has space in the middle here"]:
        with pytest.raises(AuthConfigError):
            GatewayConfig.from_env({"RESOLVE_MCP_TOKEN": bad})


def test_repr_never_shows_the_token():  # 020/072
    cfg = GatewayConfig(base_url="http://127.0.0.1:8765", static_token=GOOD_TOKEN)
    assert GOOD_TOKEN not in repr(cfg)
    assert "redacted" in repr(cfg)


def test_plain_http_only_for_loopback():
    assert validate_base_url("http://127.0.0.1:8765/") == "http://127.0.0.1:8765"
    assert validate_base_url("https://resolve.example.com") == "https://resolve.example.com"
    for bad in ["http://resolve.example.com", "ftp://127.0.0.1", "https://user:pw@host", "https://host/path", "https://host/?x=1"]:
        with pytest.raises(AuthConfigError):
            validate_base_url(bad)


def test_token_file_must_be_private(tmp_path):
    token = tmp_path / "cred"
    token.write_text(GOOD_TOKEN + "\n")
    os.chmod(token, 0o644)
    with pytest.raises(AuthConfigError, match="group- or world-accessible"):
        GatewayConfig.from_env({"RESOLVE_MCP_TOKEN_FILE": str(token)})
    os.chmod(token, 0o600)
    cfg = GatewayConfig.from_env({"RESOLVE_MCP_TOKEN_FILE": str(token)})
    assert cfg.read_token() == GOOD_TOKEN


def test_token_file_rotation_takes_effect_without_restart(tmp_path):  # 078
    token = tmp_path / "cred"
    token.write_text(GOOD_TOKEN)
    os.chmod(token, 0o600)
    cfg = GatewayConfig.from_env({"RESOLVE_MCP_TOKEN_FILE": str(token)})
    token.write_text("rst_" + "b" * 40)
    assert cfg.read_token() == "rst_" + "b" * 40


def test_bounded_numeric_settings_are_validated():
    with pytest.raises(AuthConfigError):
        GatewayConfig.from_env({"RESOLVE_MCP_TOKEN": GOOD_TOKEN, "RESOLVE_MCP_TIMEOUT_S": "9999"})
    with pytest.raises(AuthConfigError):
        GatewayConfig.from_env({"RESOLVE_MCP_TOKEN": GOOD_TOKEN, "RESOLVE_MCP_TIMEOUT_S": "soon"})
    cfg = GatewayConfig.from_env({"RESOLVE_MCP_TOKEN": GOOD_TOKEN, "RESOLVE_MCP_TIMEOUT_S": "5"})
    assert cfg.timeout_s == 5.0
