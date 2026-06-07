"""Phase 4 (P4.1): aggregated available-outputs union.

OutputRoutingService.get_aggregated_outputs merges built-ins (renfield/HA/dlna,
via the existing per-source methods) with MCP-declared providers discovered in
parallel under a per-provider timeout. A provider that errors or times out is a
DEGRADED entry (reachable=False), never dropped. Built-in methods + the registry
builder are mocked, so no DB/network is needed.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from ha_glue.services.output_providers import OutputProviderError, OutputTarget

pytestmark = pytest.mark.asyncio


def _service():
    from ha_glue.services.output_routing_service import OutputRoutingService

    svc = OutputRoutingService(db_session=AsyncMock())
    # Patch the three built-in discovery methods to controlled data.
    svc.get_available_renfield_devices = AsyncMock(return_value=[
        type("D", (), {"device_id": "sat-1", "device_name": "Kitchen Sat"})(),
    ])
    svc.get_available_ha_media_players = AsyncMock(return_value=[
        {"entity_id": "media_player.kitchen", "friendly_name": "Kitchen Speaker"},
    ])
    svc.get_available_dlna_renderers = AsyncMock(return_value=[
        {"name": "rid", "friendly_name": "Study Renderer"},
    ])
    return svc


class _FakeProvider:
    def __init__(self, key, *, targets=None, error=None, hang=False):
        self.key = key
        self.capabilities = frozenset({"video", "audio", "power"})
        self._targets = targets or []
        self._error = error
        self._hang = hang

    async def discover(self, room_id=None):
        if self._hang:
            await asyncio.sleep(5)
        if self._error:
            raise self._error
        return self._targets


def _registry(*providers):
    return {p.key: p for p in providers}


async def test_union_includes_builtins_and_mcp_targets():
    svc = _service()
    samsung = _FakeProvider("samsung", targets=[
        OutputTarget(provider="samsung", target_id="192.168.1.47", name="Living Room TV",
                     capabilities=frozenset({"video", "power"})),
    ])
    with patch("ha_glue.services.output_providers.build_mcp_output_providers",
               return_value=_registry(samsung)):
        out = await svc.get_aggregated_outputs(room_id=1, mcp_manager=object())

    by_provider = {t["provider"]: t for t in out}
    assert set(by_provider) == {"renfield", "homeassistant", "dlna", "samsung"}
    assert by_provider["renfield"]["target_id"] == "sat-1"
    assert by_provider["homeassistant"]["name"] == "Kitchen Speaker"
    assert by_provider["dlna"]["target_id"] == "rid"
    assert by_provider["samsung"]["target_id"] == "192.168.1.47"
    assert all("reachable" in t and "capabilities" in t for t in out)


async def test_failed_provider_is_degraded_not_dropped():
    svc = _service()
    samsung = _FakeProvider("samsung", error=OutputProviderError("TV asleep"))
    with patch("ha_glue.services.output_providers.build_mcp_output_providers",
               return_value=_registry(samsung)):
        out = await svc.get_aggregated_outputs(room_id=1, mcp_manager=object())

    deg = [t for t in out if t["provider"] == "samsung"]
    assert len(deg) == 1
    assert deg[0]["reachable"] is False
    assert "unreachable" in deg[0]["name"]
    assert deg[0]["capabilities"] == ["audio", "power", "video"]  # still advertised


async def test_timeout_is_degraded(monkeypatch):
    from utils.config import settings
    monkeypatch.setattr(settings, "output_provider_discover_timeout", 0.05)
    svc = _service()
    slow = _FakeProvider("samsung", hang=True)
    with patch("ha_glue.services.output_providers.build_mcp_output_providers",
               return_value=_registry(slow)):
        out = await svc.get_aggregated_outputs(room_id=1, mcp_manager=object())
    samsung = [t for t in out if t["provider"] == "samsung"]
    assert samsung and samsung[0]["reachable"] is False


async def test_parallel_one_fails_one_ok():
    svc = _service()
    ok = _FakeProvider("sonos", targets=[
        OutputTarget(provider="sonos", target_id="sonos1", name="Sonos One",
                     capabilities=frozenset({"audio"})),
    ])
    bad = _FakeProvider("samsung", error=OutputProviderError("asleep"))
    with patch("ha_glue.services.output_providers.build_mcp_output_providers",
               return_value=_registry(ok, bad)):
        out = await svc.get_aggregated_outputs(room_id=1, mcp_manager=object())
    by = {t["provider"]: t for t in out}
    assert by["sonos"]["reachable"] is True and by["sonos"]["target_id"] == "sonos1"
    assert by["samsung"]["reachable"] is False


async def test_no_mcp_manager_returns_builtins_only():
    svc = _service()
    out = await svc.get_aggregated_outputs(room_id=1, mcp_manager=None)
    assert {t["provider"] for t in out} == {"renfield", "homeassistant", "dlna"}
