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


# --- _smart_home_overview: the agent-callable read-only overview tool ---------
# These four views used to be router short-circuits (dispatch_sub_intent hooks)
# that answered BEFORE the LLM and misfired on router mis-classification (e.g.
# "Wie spät ist es?" → sensors). They are now ONE agent-callable tool: the AGENT
# decides when to call it, and the tool has NO notion of the user's query text —
# it only knows the requested `view`. The tests pin exactly that.

_OVERVIEW_ENTITY_MAP = [
    {"entity_id": "light.wohnzimmer", "friendly_name": "Wohnzimmer Decke",
     "domain": "light", "room": "wohnzimmer", "state": "on"},
    {"entity_id": "switch.kueche_kaffee", "friendly_name": "Kaffeemaschine",
     "domain": "switch", "room": "küche", "state": "on"},
    {"entity_id": "media_player.bad_radio", "friendly_name": "Bad Radio",
     "domain": "media_player", "room": "bad", "state": "idle"},
    {"entity_id": "climate.bad", "friendly_name": "Bad Thermostat",
     "domain": "climate", "room": "bad", "state": "heat"},
    {"entity_id": "sensor.wohnzimmer_temp", "friendly_name": "Wohnzimmer Temperatur",
     "domain": "sensor", "room": "wohnzimmer", "state": "21.4"},
    {"entity_id": "sensor.wohnzimmer_hum", "friendly_name": "Wohnzimmer Luftfeuchte",
     "domain": "sensor", "room": "wohnzimmer", "state": "45"},
]


def _ha_entity_map_mock(entity_map, *, raises=False):
    ha = AsyncMock()
    if raises:
        ha.get_entity_map = AsyncMock(side_effect=RuntimeError("HA unreachable"))
    else:
        ha.get_entity_map = AsyncMock(return_value=entity_map)
    return ha


def _enable_typed(monkeypatch, value=True):
    from utils.config import settings
    monkeypatch.setattr(settings, "artifacts_typed_enabled", value, raising=False)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("view,kind", [
    ("status", "table"),
    ("sensors", "keyvalue"),
    ("active_devices", "list"),
    ("devices_per_room", "chart"),
])
async def test_smart_home_overview_view_returns_expected_kind(monkeypatch, view, kind):
    _enable_typed(monkeypatch, True)
    svc = InternalToolService()
    with patch("ha_glue.integrations.homeassistant.HomeAssistantClient",
               return_value=_ha_entity_map_mock(_OVERVIEW_ENTITY_MAP)):
        out = await svc._smart_home_overview({"view": view})
    assert out["success"] is True
    assert out["action_taken"] is False
    arts = out["data"]["artifacts"]
    assert len(arts) == 1 and arts[0]["kind"] == kind
    # Internal builder-hint keys must be stripped before returning.
    for hint in ("_truncated", "_total", "_count", "_room_labels"):
        assert hint not in arts[0]
    assert isinstance(out["message"], str) and out["message"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_smart_home_overview_defaults_to_status(monkeypatch):
    _enable_typed(monkeypatch, True)
    svc = InternalToolService()
    with patch("ha_glue.integrations.homeassistant.HomeAssistantClient",
               return_value=_ha_entity_map_mock(_OVERVIEW_ENTITY_MAP)):
        # no view given → status
        out_default = await svc._smart_home_overview({})
        # unknown view → falls back to status (never an error)
        out_unknown = await svc._smart_home_overview({"view": "does_not_exist"})
    assert out_default["data"]["artifacts"][0]["kind"] == "table"
    assert out_unknown["data"]["artifacts"][0]["kind"] == "table"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_smart_home_overview_english_lede(monkeypatch):
    _enable_typed(monkeypatch, True)
    svc = InternalToolService()
    with patch("ha_glue.integrations.homeassistant.HomeAssistantClient",
               return_value=_ha_entity_map_mock(_OVERVIEW_ENTITY_MAP)):
        out = await svc._smart_home_overview({"view": "status", "lang": "en"})
    # English lede + English title from the builder.
    assert out["data"]["artifacts"][0]["title"] == "Smart home status"
    assert "devices in the house" in out["message"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_smart_home_overview_ha_unavailable_is_prose_only(monkeypatch):
    _enable_typed(monkeypatch, True)
    svc = InternalToolService()
    with patch("ha_glue.integrations.homeassistant.HomeAssistantClient",
               return_value=_ha_entity_map_mock([], raises=True)):
        out = await svc._smart_home_overview({"view": "sensors"})
    assert out["success"] is True
    assert "data" not in out          # graceful: no artifact, never a crash
    assert isinstance(out["message"], str) and out["message"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_smart_home_overview_empty_map_is_prose_only(monkeypatch):
    _enable_typed(monkeypatch, True)
    svc = InternalToolService()
    with patch("ha_glue.integrations.homeassistant.HomeAssistantClient",
               return_value=_ha_entity_map_mock([])):
        out = await svc._smart_home_overview({"view": "devices_per_room"})
    assert out["success"] is True
    assert "data" not in out


@pytest.mark.unit
@pytest.mark.asyncio
async def test_smart_home_overview_prose_only_when_flag_off(monkeypatch):
    """Flag off: still answer in PROSE (the lede), just without the typed artifact
    — better than the empty result the first cut returned, which wasted the
    agent's tool step (review finding 3)."""
    _enable_typed(monkeypatch, False)
    svc = InternalToolService()
    with patch("ha_glue.integrations.homeassistant.HomeAssistantClient",
               return_value=_ha_entity_map_mock(_OVERVIEW_ENTITY_MAP)):
        out = await svc._smart_home_overview({"view": "status"})
    assert out["success"] is True
    assert isinstance(out["message"], str) and out["message"]  # real prose answer
    assert "data" not in out  # but NO typed artifact when the renderer is off


@pytest.mark.unit
@pytest.mark.asyncio
async def test_smart_home_overview_lang_follows_settings(monkeypatch):
    """Language follows settings.default_language, not a hardcoded 'de' (review
    finding 1) — the LLM never passes `lang`, so an en deployment must render en."""
    from utils.config import settings
    _enable_typed(monkeypatch, True)
    monkeypatch.setattr(settings, "default_language", "en", raising=False)
    svc = InternalToolService()
    with patch("ha_glue.integrations.homeassistant.HomeAssistantClient",
               return_value=_ha_entity_map_mock(_OVERVIEW_ENTITY_MAP)):
        out = await svc._smart_home_overview({"view": "status"})  # no lang param
    assert out["data"]["artifacts"][0]["title"] == "Smart home status"
    assert "devices in the house" in out["message"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_smart_home_overview_ha_outage_not_reported_as_nothing_on(monkeypatch):
    """On an HA OUTAGE, active_devices must say 'couldn't read the house', NOT
    'nothing is on' (review finding 2). Proof: the outage lede is identical across
    views (the uniform couldn't-read message), not each view's genuine-empty prose."""
    _enable_typed(monkeypatch, True)
    svc = InternalToolService()
    with patch("ha_glue.integrations.homeassistant.HomeAssistantClient",
               return_value=_ha_entity_map_mock([], raises=True)):
        active = await svc._smart_home_overview({"view": "active_devices"})
        sensors = await svc._smart_home_overview({"view": "sensors"})
    assert "data" not in active and "data" not in sensors
    # Same couldn't-read lede regardless of view → no false "nothing is on".
    assert active["message"] == sensors["message"]


@pytest.mark.unit
def test_smart_home_overview_tool_has_no_time_special_casing():
    """The key behavioral proof of the refactor: the tool is a pure view builder
    with NO time/clock notion. Its schema must not mention time/clock (so the LLM
    never treats it as a time answer), and it exposes exactly the four views."""
    defn = InternalToolService.TOOLS["internal.smart_home_overview"]
    blob = (defn["description"] + " " + " ".join(defn["parameters"].values())).lower()
    for term in ("uhrzeit", "clock", "wie spät", "time of day"):
        assert term not in blob
    # Exactly the four read-only overview views, nothing time-ish.
    assert InternalToolService._OVERVIEW_VIEWS == frozenset(
        {"status", "sensors", "active_devices", "devices_per_room"}
    )
