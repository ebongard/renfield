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


# --- continuous-value actions (brightness / temperature) -------------------

@pytest.mark.unit
@pytest.mark.asyncio
async def test_device_action_set_brightness_clamps_and_calls():
    svc = InternalToolService()
    ha = _ha_mock()
    ha.get_state = AsyncMock(return_value={"state": "on", "attributes": {}})
    with patch("ha_glue.integrations.homeassistant.HomeAssistantClient", return_value=ha):
        out = await svc._device_action({"entity_id": "light.wz", "action": "set_brightness", "value": 150})
    assert out["success"] is True
    # clamped to 100, sent as brightness_pct via light.turn_on
    ha.call_service.assert_awaited_once_with("light", "turn_on", "light.wz", {"brightness_pct": 100})
    assert out["data"]["brightness"] == 100
    assert out["data"]["state"] == "on"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_device_action_set_brightness_zero_turns_off():
    svc = InternalToolService()
    ha = _ha_mock()
    ha.get_state = AsyncMock(return_value={"state": "on", "attributes": {}})
    with patch("ha_glue.integrations.homeassistant.HomeAssistantClient", return_value=ha):
        out = await svc._device_action({"entity_id": "light.wz", "action": "set_brightness", "value": 0})
    assert out["success"] is True
    ha.call_service.assert_awaited_once_with("light", "turn_off", "light.wz")
    assert out["data"]["state"] == "off"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_device_action_set_brightness_non_numeric_rejected():
    svc = InternalToolService()
    ha = _ha_mock()
    ha.get_state = AsyncMock(return_value={"state": "on", "attributes": {}})
    with patch("ha_glue.integrations.homeassistant.HomeAssistantClient", return_value=ha):
        out = await svc._device_action({"entity_id": "light.wz", "action": "set_brightness", "value": "bright"})
    assert out["success"] is False
    ha.call_service.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_device_action_set_brightness_rejected_on_switch():
    # set_brightness is light-only; not in the switch allowlist.
    svc = InternalToolService()
    out = await svc._device_action({"entity_id": "switch.x", "action": "set_brightness", "value": 50})
    assert out["success"] is False
    assert "nicht erlaubt" in out["message"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_device_action_set_temperature_clamps_to_entity_bounds():
    svc = InternalToolService()
    ha = _ha_mock()
    ha.get_state = AsyncMock(return_value={"state": "heat", "attributes": {"min_temp": 16, "max_temp": 24}})
    with patch("ha_glue.integrations.homeassistant.HomeAssistantClient", return_value=ha):
        out = await svc._device_action({"entity_id": "climate.wz", "action": "set_temperature", "value": 30})
    assert out["success"] is True
    # clamped to max_temp=24
    ha.call_service.assert_awaited_once_with("climate", "set_temperature", "climate.wz", {"temperature": 24})
    assert out["data"]["targetTemp"] == 24


# --- presence_map producer -------------------------------------------------

class _FakePresence:
    def __init__(self, mapping, names):
        self._m = mapping
        self._names = names

    def get_all_presence(self):
        return self._m

    def get_display_name(self, uid):
        return self._names[uid]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_presence_map_groups_users_by_room(monkeypatch):
    from utils.config import settings
    monkeypatch.setattr(settings, "artifacts_typed_enabled", True)
    svc = InternalToolService()

    class _P:  # a presence record with room_name
        def __init__(self, room):
            self.room_name = room

    presence = _FakePresence(
        {1: _P("Wohnzimmer"), 2: _P("Wohnzimmer"), 3: _P("Küche")},
        {1: "Eduard", 2: "Anna", 3: "Ben"},
    )
    with patch("ha_glue.services.presence_service.get_presence_service", return_value=presence):
        out = await svc._presence_map({})
    rooms = out["data"]["artifacts"][0]["data"]["rooms"]
    by_room = {r["room"]: r["users"] for r in rooms}
    assert by_room["Wohnzimmer"] == ["Anna", "Eduard"]  # sorted
    assert by_room["Küche"] == ["Ben"]
    assert out["data"]["artifacts"][0]["kind"] == "presence_map"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_presence_map_nobody_home(monkeypatch):
    from utils.config import settings
    monkeypatch.setattr(settings, "artifacts_typed_enabled", True)
    svc = InternalToolService()
    with patch("ha_glue.services.presence_service.get_presence_service",
               return_value=_FakePresence({}, {})):
        out = await svc._presence_map({})
    assert out["success"] is True
    assert "data" not in out  # no artifact when nobody is present


# --- _device_controls: the producer (reads FRESH get_states() w/ attributes) -

from unittest.mock import MagicMock

# Raw HA states (the shape get_states() returns). The producer derives domain
# from the entity_id, name from attributes.friendly_name, room via _extract_room.
_STATES = [
    {"entity_id": "light.wz", "state": "on",
     "attributes": {"friendly_name": "Licht WZ", "brightness": 204}},  # 204/255 ≈ 80%
    {"entity_id": "switch.kaffee", "state": "off", "attributes": {"friendly_name": "Kaffee"}},
    {"entity_id": "scene.abend", "state": "x", "attributes": {"friendly_name": "Abend"}},
    {"entity_id": "climate.wz", "state": "heat",
     "attributes": {"friendly_name": "Heizung", "current_temperature": 19.5, "temperature": 21,
                    "min_temp": 5, "max_temp": 30, "target_temp_step": 0.5}},
    {"entity_id": "sensor.temp", "state": "21", "attributes": {"friendly_name": "Temp"}},
    {"entity_id": "media_player.tv", "state": "idle", "attributes": {"friendly_name": "TV"}},
]
_ROOMS = {"light.wz": "Wohnzimmer", "switch.kaffee": "Küche", "scene.abend": None,
          "climate.wz": "Wohnzimmer", "sensor.temp": "Wohnzimmer", "media_player.tv": "Wohnzimmer"}


def _ha_states_mock(states):
    ha = AsyncMock()
    ha.get_states = AsyncMock(return_value=states)
    ha._extract_room = MagicMock(side_effect=lambda eid, name: _ROOMS.get(eid))
    return ha


@pytest.mark.unit
@pytest.mark.asyncio
async def test_device_controls_builds_artifact_with_controllable_only(monkeypatch):
    from utils.config import settings
    monkeypatch.setattr(settings, "artifacts_typed_enabled", True)
    svc = InternalToolService()
    with patch("ha_glue.integrations.homeassistant.HomeAssistantClient",
               return_value=_ha_states_mock(_STATES)):
        out = await svc._device_controls({})
    assert out["success"] is True
    devices = out["data"]["artifacts"][0]["data"]["devices"]
    # sensor + media_player dropped; light/switch/scene/climate kept.
    assert [d["entity_id"] for d in devices] == ["light.wz", "switch.kaffee", "scene.abend", "climate.wz"]
    by_id = {d["entity_id"]: d for d in devices}
    assert by_id["light.wz"]["brightness"] == 80          # 204/255 → 80
    assert by_id["climate.wz"]["targetTemp"] == 21         # climate attrs surfaced
    assert by_id["climate.wz"]["currentTemp"] == 19.5
    assert out["data"]["artifacts"][0]["kind"] == "device_control"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_device_controls_room_scope(monkeypatch):
    from utils.config import settings
    monkeypatch.setattr(settings, "artifacts_typed_enabled", True)
    svc = InternalToolService()
    with patch("ha_glue.integrations.homeassistant.HomeAssistantClient",
               return_value=_ha_states_mock(_STATES)):
        out = await svc._device_controls({"room": "Küche"})
    devices = out["data"]["artifacts"][0]["data"]["devices"]
    assert [d["entity_id"] for d in devices] == ["switch.kaffee"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_device_controls_no_controllable_returns_message(monkeypatch):
    from utils.config import settings
    monkeypatch.setattr(settings, "artifacts_typed_enabled", True)
    svc = InternalToolService()
    with patch("ha_glue.integrations.homeassistant.HomeAssistantClient",
               return_value=_ha_states_mock([_STATES[4]])):  # only a sensor
        out = await svc._device_controls({})
    assert out["success"] is True
    assert "data" not in out  # no artifact
    assert "keine schaltbaren" in out["message"].lower()
