"""
Tests for the interactive device-control widget tools (Gen-UI item 10):
`internal.device_controls` (producer → device_control artifact) and
`internal.device_action` (the actuation the widget-click frame routes to).

The HA_CONTROL permission gate lives in the chat handler; these pin the SECOND
layer that `_device_action` enforces regardless — the domain/action allowlist
and the entity-existence probe — so a crafted frame can't drive an arbitrary
service. HomeAssistantClient is mocked.
"""
from unittest.mock import AsyncMock, patch

import pytest

from ha_glue.services.internal_tools import InternalToolService


def _ha_mock(*, state_exists=True, call_ok=True):
    ha = AsyncMock()
    ha.get_state = AsyncMock(return_value={"state": "on"} if state_exists else None)
    ha.call_service = AsyncMock(return_value=call_ok)
    return ha


# --- _device_action: the actuation allowlist (security 2nd layer) -----------

@pytest.mark.unit
@pytest.mark.asyncio
async def test_device_action_rejects_uncontrollable_domain():
    svc = InternalToolService()
    out = await svc._device_action({"entity_id": "sensor.temp", "action": "toggle"})
    assert out["success"] is False
    assert "schaltbar" in out["message"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_device_action_rejects_disallowed_action():
    svc = InternalToolService()
    # 'activate' is only for scenes; not allowed on a light.
    out = await svc._device_action({"entity_id": "light.wz", "action": "activate"})
    assert out["success"] is False
    assert "nicht erlaubt" in out["message"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_device_action_rejects_nonexistent_entity():
    svc = InternalToolService()
    with patch("ha_glue.integrations.homeassistant.HomeAssistantClient",
               return_value=_ha_mock(state_exists=False)):
        out = await svc._device_action({"entity_id": "light.ghost", "action": "toggle"})
    assert out["success"] is False
    assert "nicht gefunden" in out["message"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_device_action_toggle_inverts_prior_state_deterministically():
    svc = InternalToolService()
    ha = _ha_mock()
    # Single probe (prior=off); the new state is computed from the action (toggle
    # → on), NOT a re-read (HA's state store lags the service call).
    ha.get_state = AsyncMock(return_value={"state": "off"})
    with patch("ha_glue.integrations.homeassistant.HomeAssistantClient", return_value=ha):
        out = await svc._device_action({"entity_id": "light.wz", "action": "toggle"})
    assert out["success"] is True
    ha.call_service.assert_awaited_once_with("light", "toggle", "light.wz")
    assert ha.get_state.await_count == 1  # no second re-read
    assert out["data"]["state"] == "on"
    assert out["data"]["entity_id"] == "light.wz"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_device_action_turn_off_resolves_off():
    svc = InternalToolService()
    ha = _ha_mock()
    ha.get_state = AsyncMock(return_value={"state": "on"})
    with patch("ha_glue.integrations.homeassistant.HomeAssistantClient", return_value=ha):
        out = await svc._device_action({"entity_id": "switch.x", "action": "turn_off"})
    assert out["data"]["state"] == "off"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_device_action_scene_activates_via_scene_turn_on():
    svc = InternalToolService()
    ha = _ha_mock()
    with patch("ha_glue.integrations.homeassistant.HomeAssistantClient", return_value=ha):
        out = await svc._device_action({"entity_id": "scene.abend", "action": "activate"})
    assert out["success"] is True
    ha.call_service.assert_awaited_once_with("scene", "turn_on", "scene.abend")
    assert out["data"]["state"] == "on"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_device_action_ha_call_failure():
    svc = InternalToolService()
    with patch("ha_glue.integrations.homeassistant.HomeAssistantClient",
               return_value=_ha_mock(call_ok=False)):
        out = await svc._device_action({"entity_id": "switch.x", "action": "turn_on"})
    assert out["success"] is False


# --- _device_controls: the producer ----------------------------------------

_ENTITY_MAP = [
    {"entity_id": "light.wz", "domain": "light", "friendly_name": "Licht WZ", "state": "on", "room": "Wohnzimmer"},
    {"entity_id": "switch.kaffee", "domain": "switch", "friendly_name": "Kaffee", "state": "off", "room": "Küche"},
    {"entity_id": "scene.abend", "domain": "scene", "friendly_name": "Abend", "state": "x", "room": None},
    {"entity_id": "sensor.temp", "domain": "sensor", "friendly_name": "Temp", "state": "21", "room": "Wohnzimmer"},
    {"entity_id": "media_player.tv", "domain": "media_player", "friendly_name": "TV", "state": "idle", "room": "Wohnzimmer"},
]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_device_controls_builds_artifact_with_controllable_only(monkeypatch):
    from utils.config import settings
    monkeypatch.setattr(settings, "artifacts_typed_enabled", True)
    svc = InternalToolService()
    ha = AsyncMock()
    ha.get_entity_map = AsyncMock(return_value=_ENTITY_MAP)
    with patch("ha_glue.integrations.homeassistant.HomeAssistantClient", return_value=ha):
        out = await svc._device_controls({})
    assert out["success"] is True
    devices = out["data"]["artifacts"][0]["data"]["devices"]
    # sensor + media_player dropped; light/switch/scene kept.
    assert [d["entity_id"] for d in devices] == ["light.wz", "switch.kaffee", "scene.abend"]
    assert out["data"]["artifacts"][0]["kind"] == "device_control"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_device_controls_room_scope(monkeypatch):
    from utils.config import settings
    monkeypatch.setattr(settings, "artifacts_typed_enabled", True)
    svc = InternalToolService()
    ha = AsyncMock()
    ha.get_entity_map = AsyncMock(return_value=_ENTITY_MAP)
    with patch("ha_glue.integrations.homeassistant.HomeAssistantClient", return_value=ha):
        out = await svc._device_controls({"room": "Küche"})
    devices = out["data"]["artifacts"][0]["data"]["devices"]
    assert [d["entity_id"] for d in devices] == ["switch.kaffee"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_device_controls_no_controllable_returns_message(monkeypatch):
    from utils.config import settings
    monkeypatch.setattr(settings, "artifacts_typed_enabled", True)
    svc = InternalToolService()
    ha = AsyncMock()
    ha.get_entity_map = AsyncMock(return_value=[_ENTITY_MAP[3]])  # only a sensor
    with patch("ha_glue.integrations.homeassistant.HomeAssistantClient", return_value=ha):
        out = await svc._device_controls({})
    assert out["success"] is True
    assert "data" not in out  # no artifact
    assert "keine schaltbaren" in out["message"].lower()
