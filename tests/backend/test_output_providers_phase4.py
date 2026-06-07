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


# --- dedupe (same physical device exposed by multiple providers) -------------

from ha_glue.services.output_routing_service import (  # noqa: E402
    _clean_display_name,
    _device_match_key,
    _dedupe_output_targets,
)


def test_match_key_collapses_ha_and_dlna_names():
    # HA friendly name (doubled room) vs DLNA renderer name → same key
    assert _device_match_key("Linn Wohnzimmer Wohnzimmer") == _device_match_key("Linn Wohnzimmer:UPnP AV")
    assert _device_match_key("HiFiBerry Arbeitszimmer") == _device_match_key("HiFiBerry Arbeitszimmer")
    # distinct devices → distinct keys
    assert _device_match_key("Linn Küche:UpnpAv") != _device_match_key("Linn Garten:UPnP AV")


def test_clean_display_name():
    assert _clean_display_name("Linn Wohnzimmer:UPnP AV") == "Linn Wohnzimmer"
    assert _clean_display_name("Linn Wohnzimmer Wohnzimmer") == "Linn Wohnzimmer"


def _t(provider, target_id, name, caps, reachable=True):
    return {"provider": provider, "target_id": target_id, "name": name,
            "capabilities": caps, "room_hint": None, "reachable": reachable}


def test_dedupe_collapses_cross_provider_prefers_dlna_merges_caps():
    targets = [
        _t("homeassistant", "media_player.linn_wz", "Linn Wohnzimmer Wohnzimmer", ["audio", "transport"]),
        _t("dlna", "Linn Wohnzimmer:UPnP AV", "Linn Wohnzimmer:UPnP AV", ["audio", "video"]),
    ]
    out = _dedupe_output_targets(targets)
    assert len(out) == 1
    assert out[0]["provider"] == "dlna"                       # dlna preferred
    assert out[0]["target_id"] == "Linn Wohnzimmer:UPnP AV"
    assert out[0]["name"] == "Linn Wohnzimmer"               # tidied
    assert out[0]["capabilities"] == ["audio", "transport", "video"]  # unioned


def test_dedupe_keeps_same_provider_same_name_as_distinct():
    # Two different HA entities both named "Soundbar" → must NOT merge
    targets = [
        _t("homeassistant", "media_player.buro", "Soundbar", ["audio"]),
        _t("homeassistant", "media_player.118", "Soundbar", ["audio"]),
    ]
    out = _dedupe_output_targets(targets)
    assert len(out) == 2
    assert {t["target_id"] for t in out} == {"media_player.buro", "media_player.118"}


def test_dedupe_preserves_singletons_and_order():
    targets = [
        _t("renfield", "sat-1", "Kitchen Sat", ["audio"]),
        _t("samsung", "192.168.1.47", "Living Room TV", ["video", "power"]),
    ]
    out = _dedupe_output_targets(targets)
    assert [t["target_id"] for t in out] == ["sat-1", "192.168.1.47"]


async def test_aggregation_dedupes_ha_dlna_overlap():
    # get_aggregated_outputs end-to-end: HA + dlna both expose "Wohnzimmer Speaker"
    svc = _service()
    svc.get_available_renfield_devices = AsyncMock(return_value=[])
    svc.get_available_ha_media_players = AsyncMock(return_value=[
        {"entity_id": "media_player.wz", "friendly_name": "Wohnzimmer Speaker"},
    ])
    svc.get_available_dlna_renderers = AsyncMock(return_value=[
        {"name": "Wohnzimmer Speaker:UPnP AV", "friendly_name": "Wohnzimmer Speaker:UPnP AV"},
    ])
    out = await svc.get_aggregated_outputs(room_id=1, mcp_manager=None)
    assert len(out) == 1
    assert out[0]["provider"] == "dlna" and out[0]["name"] == "Wohnzimmer Speaker"
