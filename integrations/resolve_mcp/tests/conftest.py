from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))  # fake_resolve helper

from fake_resolve import GOOD_TOKEN, FakeResolve  # noqa: E402

from resolve_mcp.config import GatewayConfig  # noqa: E402
from resolve_mcp.gateway import ResolveGateway  # noqa: E402


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def fake():
    server = FakeResolve().start()
    yield server
    server.stop()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def make_gateway(fake, clock):
    def _make(token: str = GOOD_TOKEN, **overrides) -> ResolveGateway:
        config = GatewayConfig(base_url=fake.base_url, static_token=token, **overrides)
        return ResolveGateway(config, clock=clock)

    return _make


@pytest.fixture
def gateway(make_gateway) -> ResolveGateway:
    return make_gateway()


# ---- live tests: only run against a real RESOLVE you started yourself (see README, "Live tests")
@pytest.fixture(scope="session")
def live_config() -> GatewayConfig:
    base = os.environ.get("RESOLVE_LIVE_BASE_URL", "").strip()
    token = os.environ.get("RESOLVE_LIVE_TOKEN", "").strip()
    if not base or not token:
        pytest.skip("live RESOLVE tests need RESOLVE_LIVE_BASE_URL and RESOLVE_LIVE_TOKEN (environment class: live-resolve)")
    return GatewayConfig(base_url=base, static_token=token, timeout_s=60.0)
