"""
Tests for the three additional Lane A artifact producers (keyvalue / list / chart)
in ``ha_glue.services.smarthome_artifacts``.

Each producer mirrors the FIRST one (``smarthome_status`` → table). For every one
we cover, per the task brief:
  * builds a well-formed artifact of the right KIND for its trigger (mocked data),
    and the artifact passes ``validate_artifacts`` (within the backend caps);
  * returns NO artifact (graceful, prose-only) on empty / unavailable data;
  * the dispatch handler is INERT (declines, returns None) when the flag is off,
    WITHOUT touching HA (so a broken flag gate would crash — no HA patched);
  * declines for any role / sub_intent / handler that isn't its own.

Plus a shared ``_infer_sub_intent`` block that proves the new sub-intents fire on
their German triggers and do NOT fire on actuation commands or on each other's
triggers.

The HA fetch is mocked everywhere (the producers call ``_safe_entity_map`` which
lazily imports ``HomeAssistantClient`` — we patch that symbol), so no real asyncpg
or HA connect happens (test-isolation lesson).
"""
import pytest

from ha_glue.services.smarthome_artifacts import (
    MAX_ACTIVE_ITEMS,
    MAX_ROOM_BARS,
    MAX_SENSOR_PAIRS,
    build_active_list,
    build_devices_per_room_chart,
    build_sensor_keyvalue,
    ha_dispatch_active_devices,
    ha_dispatch_devices_per_room,
    ha_dispatch_sensors,
)
from services.artifact_service import (
    MAX_CHART_POINTS,
    MAX_KEYVALUE_PAIRS,
    MAX_LIST_ITEMS,
    validate_artifacts,
)


# --- shared fixtures --------------------------------------------------------

_ENTITY_MAP = [
    {"entity_id": "light.wohnzimmer", "friendly_name": "Wohnzimmer Decke",
     "domain": "light", "room": "wohnzimmer", "state": "on"},
    {"entity_id": "light.kueche", "friendly_name": "Küche Spots",
     "domain": "light", "room": "küche", "state": "off"},
    {"entity_id": "switch.kueche_kaffee", "friendly_name": "Kaffeemaschine",
     "domain": "switch", "room": "küche", "state": "on"},
    {"entity_id": "media_player.wohnzimmer_tv", "friendly_name": "Wohnzimmer TV",
     "domain": "media_player", "room": "wohnzimmer", "state": "playing"},
    {"entity_id": "media_player.bad_radio", "friendly_name": "Bad Radio",
     "domain": "media_player", "room": "bad", "state": "idle"},
    {"entity_id": "climate.bad", "friendly_name": "Bad Thermostat",
     "domain": "climate", "room": "bad", "state": "heat"},
    {"entity_id": "sensor.wohnzimmer_temp", "friendly_name": "Wohnzimmer Temperatur",
     "domain": "sensor", "room": "wohnzimmer", "state": "21.4"},
    {"entity_id": "sensor.wohnzimmer_hum", "friendly_name": "Wohnzimmer Luftfeuchte",
     "domain": "sensor", "room": "wohnzimmer", "state": "45"},
    {"entity_id": "sensor.bad_temp", "friendly_name": "Bad Temperatur",
     "domain": "sensor", "room": "bad", "state": "23.1"},
    {"entity_id": "sensor.flur_motion", "friendly_name": "Flur Bewegung",
     "domain": "sensor", "room": None, "state": "clear"},
]


class _FakeHAClient:
    def __init__(self, entity_map):
        self._entity_map = entity_map

    async def get_entity_map(self):
        return self._entity_map


def _patch_ha(monkeypatch, entity_map=None, *, raises=False):
    """Patch the HomeAssistantClient the producers import lazily."""
    import ha_glue.integrations.homeassistant as ha_mod

    def _factory():
        if raises:
            raise RuntimeError("HA unreachable")
        return _FakeHAClient(entity_map if entity_map is not None else [])

    monkeypatch.setattr(ha_mod, "HomeAssistantClient", _factory)


def _enable_flag(monkeypatch, value=True):
    from utils.config import settings
    monkeypatch.setattr(settings, "artifacts_typed_enabled", value, raising=False)


# ===========================================================================
# Producer 1: sensors → keyvalue
# ===========================================================================

@pytest.mark.unit
def test_build_sensor_keyvalue_well_formed():
    art = build_sensor_keyvalue(_ENTITY_MAP, lang="de")
    assert art is not None
    assert art["kind"] == "keyvalue"
    assert art["id"].startswith("art_sensors_")
    assert art["title"] == "Sensorwerte"
    pairs = art["data"]["pairs"]
    by_key = {p["key"]: p["value"] for p in pairs}
    # Wohnzimmer has both temp + humidity folded into one value string.
    assert by_key["Wohnzimmer"] == "21.4 °C · 45 %"
    # Bad has only temperature.
    assert by_key["Bad"] == "23.1 °C"
    # The non-temp/humidity sensor (Flur Bewegung, non-numeric) is excluded.
    assert "Ohne Raum" not in by_key
    for p in pairs:
        assert isinstance(p["key"], str) and isinstance(p["value"], str)


@pytest.mark.unit
def test_build_sensor_keyvalue_english():
    art = build_sensor_keyvalue(_ENTITY_MAP, lang="en")
    assert art["title"] == "Sensor readings"
    # units are language-neutral
    by_key = {p["key"]: p["value"] for p in art["data"]["pairs"]}
    assert by_key["Wohnzimmer"] == "21.4 °C · 45 %"


@pytest.mark.unit
def test_build_sensor_keyvalue_empty_returns_none():
    assert build_sensor_keyvalue([], lang="de") is None


@pytest.mark.unit
def test_build_sensor_keyvalue_no_sensors_returns_none():
    # Only controllable devices, no temp/humidity sensors → nothing to show.
    only_lights = [
        {"entity_id": "light.x", "friendly_name": "Lampe", "domain": "light",
         "room": "wohnzimmer", "state": "on"},
    ]
    assert build_sensor_keyvalue(only_lights, lang="de") is None


@pytest.mark.unit
def test_build_sensor_keyvalue_passes_validate():
    art = build_sensor_keyvalue(_ENTITY_MAP, lang="de")
    art.pop("_truncated", None)
    art.pop("_count", None)
    cleaned = validate_artifacts([art])
    assert len(cleaned) == 1
    assert cleaned[0]["kind"] == "keyvalue"


@pytest.mark.unit
def test_build_sensor_keyvalue_truncates_over_cap():
    big = []
    for i in range(MAX_SENSOR_PAIRS + 10):
        big.append({"entity_id": f"sensor.room{i}_temp",
                    "friendly_name": f"Room{i} Temperatur",
                    "domain": "sensor", "room": f"room{i}", "state": "20.0"})
    art = build_sensor_keyvalue(big, lang="de")
    assert len(art["data"]["pairs"]) == MAX_SENSOR_PAIRS
    assert art["_truncated"] is True
    assert len(art["data"]["pairs"]) <= MAX_KEYVALUE_PAIRS


@pytest.mark.unit
async def test_dispatch_sensors_returns_artifact(monkeypatch):
    _enable_flag(monkeypatch, True)
    _patch_ha(monkeypatch, _ENTITY_MAP)
    res = await ha_dispatch_sensors(
        role="smart_home", sub_intent="sensors",
        handler_name="smarthome_sensors", lang="de",
    )
    assert res["handled"] is True
    assert res["card"] is None
    assert isinstance(res["answer"], str) and res["answer"]
    arts = res["artifacts"]
    assert len(arts) == 1 and arts[0]["kind"] == "keyvalue"
    assert "_truncated" not in arts[0] and "_count" not in arts[0]
    assert len(validate_artifacts(arts)) == 1


@pytest.mark.unit
async def test_dispatch_sensors_ha_unavailable_graceful(monkeypatch):
    _enable_flag(monkeypatch, True)
    _patch_ha(monkeypatch, raises=True)
    res = await ha_dispatch_sensors(
        role="smart_home", sub_intent="sensors",
        handler_name="smarthome_sensors", lang="de",
    )
    assert res["handled"] is True
    assert "artifacts" not in res
    assert res["answer"]


@pytest.mark.unit
async def test_dispatch_sensors_empty_graceful(monkeypatch):
    _enable_flag(monkeypatch, True)
    _patch_ha(monkeypatch, [])
    res = await ha_dispatch_sensors(
        role="smart_home", sub_intent="sensors",
        handler_name="smarthome_sensors", lang="de",
    )
    assert res["handled"] is True
    assert "artifacts" not in res


@pytest.mark.unit
async def test_dispatch_sensors_inert_when_flag_off(monkeypatch):
    _enable_flag(monkeypatch, False)
    # No HA patched — if the flag gate were broken, this would raise.
    res = await ha_dispatch_sensors(
        role="smart_home", sub_intent="sensors",
        handler_name="smarthome_sensors", lang="de",
    )
    assert res is None


@pytest.mark.unit
async def test_dispatch_sensors_declines_other_sub_intent(monkeypatch):
    _enable_flag(monkeypatch, True)
    assert await ha_dispatch_sensors(
        role="smart_home", sub_intent="status",
        handler_name="smarthome_sensors", lang="de") is None
    assert await ha_dispatch_sensors(
        role="media", sub_intent="sensors",
        handler_name="smarthome_sensors", lang="de") is None
    assert await ha_dispatch_sensors(
        role="smart_home", sub_intent="sensors",
        handler_name="something_else", lang="de") is None


# ===========================================================================
# Producer 2: active_devices → list
# ===========================================================================

@pytest.mark.unit
def test_build_active_list_well_formed():
    art = build_active_list(_ENTITY_MAP, lang="de")
    assert art is not None
    assert art["kind"] == "list"
    assert art["id"].startswith("art_active_")
    assert art["title"] == "Aktuell eingeschaltet"
    items = art["data"]["items"]
    assert art["data"]["ordered"] is False
    flat = " || ".join(items)
    # on light, on switch, playing media_player are included.
    assert "Wohnzimmer: Licht – Wohnzimmer Decke" in flat
    assert "Küche: Schalter – Kaffeemaschine" in flat
    assert "Wohnzimmer: Medien – Wohnzimmer TV" in flat
    # OFF light and IDLE media_player are excluded.
    assert "Küche Spots" not in flat
    assert "Bad Radio" not in flat
    # climate / sensor are not "active devices".
    assert "Thermostat" not in flat
    for it in items:
        assert isinstance(it, str)


@pytest.mark.unit
def test_build_active_list_english():
    art = build_active_list(_ENTITY_MAP, lang="en")
    assert art["title"] == "Currently on"
    flat = " || ".join(art["data"]["items"])
    assert "Light – Wohnzimmer Decke" in flat
    assert "Media – Wohnzimmer TV" in flat


@pytest.mark.unit
def test_build_active_list_empty_returns_none():
    assert build_active_list([], lang="de") is None


@pytest.mark.unit
def test_build_active_list_nothing_on_returns_none():
    all_off = [
        {"entity_id": "light.x", "friendly_name": "Lampe", "domain": "light",
         "room": "wohnzimmer", "state": "off"},
        {"entity_id": "media_player.y", "friendly_name": "TV",
         "domain": "media_player", "room": "wohnzimmer", "state": "idle"},
    ]
    assert build_active_list(all_off, lang="de") is None


@pytest.mark.unit
def test_build_active_list_passes_validate():
    art = build_active_list(_ENTITY_MAP, lang="de")
    art.pop("_truncated", None)
    art.pop("_count", None)
    cleaned = validate_artifacts([art])
    assert len(cleaned) == 1 and cleaned[0]["kind"] == "list"


@pytest.mark.unit
def test_build_active_list_truncates_over_cap():
    big = [
        {"entity_id": f"light.x{i}", "friendly_name": f"Lampe {i}",
         "domain": "light", "room": "wohnzimmer", "state": "on"}
        for i in range(MAX_ACTIVE_ITEMS + 15)
    ]
    art = build_active_list(big, lang="de")
    assert len(art["data"]["items"]) == MAX_ACTIVE_ITEMS
    assert art["_truncated"] is True
    assert len(art["data"]["items"]) <= MAX_LIST_ITEMS


@pytest.mark.unit
async def test_dispatch_active_returns_artifact(monkeypatch):
    _enable_flag(monkeypatch, True)
    _patch_ha(monkeypatch, _ENTITY_MAP)
    res = await ha_dispatch_active_devices(
        role="smart_home", sub_intent="active_devices",
        handler_name="smarthome_active_devices", lang="de",
    )
    assert res["handled"] is True and res["card"] is None
    arts = res["artifacts"]
    assert len(arts) == 1 and arts[0]["kind"] == "list"
    assert "_truncated" not in arts[0] and "_count" not in arts[0]
    assert len(validate_artifacts(arts)) == 1


@pytest.mark.unit
async def test_dispatch_active_nothing_on_prose_only(monkeypatch):
    _enable_flag(monkeypatch, True)
    all_off = [
        {"entity_id": "light.x", "friendly_name": "Lampe", "domain": "light",
         "room": "wohnzimmer", "state": "off"},
    ]
    _patch_ha(monkeypatch, all_off)
    res = await ha_dispatch_active_devices(
        role="smart_home", sub_intent="active_devices",
        handler_name="smarthome_active_devices", lang="de",
    )
    assert res["handled"] is True
    assert "artifacts" not in res
    assert "nichts" in res["answer"].lower()


@pytest.mark.unit
async def test_dispatch_active_ha_unavailable_graceful(monkeypatch):
    _enable_flag(monkeypatch, True)
    _patch_ha(monkeypatch, raises=True)
    res = await ha_dispatch_active_devices(
        role="smart_home", sub_intent="active_devices",
        handler_name="smarthome_active_devices", lang="de",
    )
    assert res["handled"] is True and "artifacts" not in res


@pytest.mark.unit
async def test_dispatch_active_inert_when_flag_off(monkeypatch):
    _enable_flag(monkeypatch, False)
    res = await ha_dispatch_active_devices(
        role="smart_home", sub_intent="active_devices",
        handler_name="smarthome_active_devices", lang="de",
    )
    assert res is None


@pytest.mark.unit
async def test_dispatch_active_declines_other_sub_intent(monkeypatch):
    _enable_flag(monkeypatch, True)
    assert await ha_dispatch_active_devices(
        role="smart_home", sub_intent="status",
        handler_name="smarthome_active_devices", lang="de") is None
    assert await ha_dispatch_active_devices(
        role="media", sub_intent="active_devices",
        handler_name="smarthome_active_devices", lang="de") is None


# ===========================================================================
# Producer 3: devices_per_room → chart
# ===========================================================================

@pytest.mark.unit
def test_build_chart_well_formed():
    art = build_devices_per_room_chart(_ENTITY_MAP, lang="de")
    assert art is not None
    assert art["kind"] == "chart"
    assert art["id"].startswith("art_devperroom_")
    assert art["title"] == "Geräte pro Raum"
    data = art["data"]
    assert data["chartType"] == "bar"
    assert len(data["series"]) == 1
    series = data["series"][0]
    assert series["label"] == "Geräte"
    points = series["points"]
    # every x/y is a finite float
    for p in points:
        assert isinstance(p["x"], float) and isinstance(p["y"], float)
    # counts: wohnzimmer=3 (light, media_player, 2 sensors=4?) — recompute honestly.
    # wohnzimmer: light.wohnzimmer, media_player.wohnzimmer_tv,
    #             sensor.wohnzimmer_temp, sensor.wohnzimmer_hum = 4
    # küche: light.kueche, switch.kueche_kaffee = 2
    # bad: media_player.bad_radio, climate.bad, sensor.bad_temp = 3
    # Ohne Raum: sensor.flur_motion = 1
    labels = art["_room_labels"]
    ys = [int(p["y"]) for p in points]
    by_room = dict(zip(labels, ys))
    assert by_room["Wohnzimmer"] == 4
    assert by_room["Küche"] == 2
    assert by_room["Bad"] == 3
    assert by_room["Ohne Raum"] == 1
    # sorted descending by count
    assert ys == sorted(ys, reverse=True)


@pytest.mark.unit
def test_build_chart_english():
    art = build_devices_per_room_chart(_ENTITY_MAP, lang="en")
    assert art["title"] == "Devices per room"
    assert art["data"]["series"][0]["label"] == "Devices"
    assert "No room" in art["_room_labels"]


@pytest.mark.unit
def test_build_chart_empty_returns_none():
    assert build_devices_per_room_chart([], lang="de") is None


@pytest.mark.unit
def test_build_chart_passes_validate():
    art = build_devices_per_room_chart(_ENTITY_MAP, lang="de")
    art.pop("_room_labels", None)
    art.pop("_truncated", None)
    art.pop("_count", None)
    cleaned = validate_artifacts([art])
    assert len(cleaned) == 1 and cleaned[0]["kind"] == "chart"


@pytest.mark.unit
def test_build_chart_truncates_over_cap():
    big = [
        {"entity_id": f"light.x{i}", "friendly_name": f"L{i}", "domain": "light",
         "room": f"room{i}", "state": "on"}
        for i in range(MAX_ROOM_BARS + 12)
    ]
    art = build_devices_per_room_chart(big, lang="de")
    pts = art["data"]["series"][0]["points"]
    assert len(pts) == MAX_ROOM_BARS
    assert art["_truncated"] is True
    assert len(pts) <= MAX_CHART_POINTS


@pytest.mark.unit
async def test_dispatch_chart_returns_artifact(monkeypatch):
    _enable_flag(monkeypatch, True)
    _patch_ha(monkeypatch, _ENTITY_MAP)
    res = await ha_dispatch_devices_per_room(
        role="smart_home", sub_intent="devices_per_room",
        handler_name="smarthome_devices_per_room", lang="de",
    )
    assert res["handled"] is True and res["card"] is None
    arts = res["artifacts"]
    assert len(arts) == 1 and arts[0]["kind"] == "chart"
    # internal hint keys stripped
    assert "_room_labels" not in arts[0]
    assert "_truncated" not in arts[0] and "_count" not in arts[0]
    # legend prose names the rooms left-to-right
    assert "Wohnzimmer" in res["answer"]
    assert len(validate_artifacts(arts)) == 1


@pytest.mark.unit
async def test_dispatch_chart_ha_unavailable_graceful(monkeypatch):
    _enable_flag(monkeypatch, True)
    _patch_ha(monkeypatch, raises=True)
    res = await ha_dispatch_devices_per_room(
        role="smart_home", sub_intent="devices_per_room",
        handler_name="smarthome_devices_per_room", lang="de",
    )
    assert res["handled"] is True and "artifacts" not in res


@pytest.mark.unit
async def test_dispatch_chart_empty_graceful(monkeypatch):
    _enable_flag(monkeypatch, True)
    _patch_ha(monkeypatch, [])
    res = await ha_dispatch_devices_per_room(
        role="smart_home", sub_intent="devices_per_room",
        handler_name="smarthome_devices_per_room", lang="de",
    )
    assert res["handled"] is True and "artifacts" not in res


@pytest.mark.unit
async def test_dispatch_chart_inert_when_flag_off(monkeypatch):
    _enable_flag(monkeypatch, False)
    res = await ha_dispatch_devices_per_room(
        role="smart_home", sub_intent="devices_per_room",
        handler_name="smarthome_devices_per_room", lang="de",
    )
    assert res is None


@pytest.mark.unit
async def test_dispatch_chart_declines_other_sub_intent(monkeypatch):
    _enable_flag(monkeypatch, True)
    assert await ha_dispatch_devices_per_room(
        role="smart_home", sub_intent="status",
        handler_name="smarthome_devices_per_room", lang="de") is None


# ===========================================================================
# Sub-intent routing: _infer_sub_intent over the configured keyword sets
# ===========================================================================

def _all_sub_intent_definitions():
    """The four smart_home sub_intent definitions exactly as agent_router parses
    them from agent_roles.yaml (de/en keyword strings)."""
    return {
        "status": {
            "de": ("Status, Übersicht, Überblick, Zustand, wie ist der status im haus, "
                   "smart-home-status, geräteübersicht, alle geräte anzeigen, gesamtüberblick"),
            "en": ("status, overview, summary, state of the house, smart home status, "
                   "device overview, show all devices, whole house status"),
        },
        "sensors": {
            "de": ("Sensorwerte, Temperaturen, Temperatur in den räumen, wie warm ist es, "
                   "wie warm ist es in den räumen, luftfeuchte, luftfeuchtigkeit, "
                   "raumtemperatur, messwerte"),
            "en": ("sensor readings, temperatures, temperature in the rooms, how warm is it, "
                   "how warm is it in the rooms, humidity, air humidity, room temperature, "
                   "measured values"),
        },
        "active_devices": {
            "de": ("Was ist gerade an, was ist an, was läuft gerade, welche geräte sind "
                   "eingeschaltet, was ist eingeschaltet, welche geräte laufen, was ist aktiv"),
            "en": ("what is on, what is currently on, what is running, which devices are "
                   "switched on, which devices are on, which devices are running, "
                   "what is active"),
        },
        "devices_per_room": {
            "de": ("Geräte pro Raum, wie viele geräte pro raum, anzahl geräte je raum, "
                   "geräteverteilung, wie viele geräte gibt es pro raum, geräte je raum"),
            "en": ("devices per room, how many devices per room, device count per room, "
                   "device distribution, how many devices are there per room, devices by room"),
        },
    }


@pytest.mark.unit
@pytest.mark.parametrize("msg,expected", [
    ("Wie sind die Temperaturen?", "sensors"),
    ("Zeig mir die Sensorwerte", "sensors"),
    ("Wie warm ist es in den Räumen?", "sensors"),
    ("Was ist gerade an?", "active_devices"),
    ("Welche Geräte sind eingeschaltet?", "active_devices"),
    ("Was läuft gerade?", "active_devices"),
    ("Geräte pro Raum bitte", "devices_per_room"),
    ("Wie viele Geräte pro Raum gibt es?", "devices_per_room"),
    ("Gib mir einen Überblick", "status"),
    ("Smart-Home-Status", "status"),
])
def test_infer_sub_intent_routes_to_correct_producer(msg, expected):
    from services.agent_router import AgentRouter
    assert AgentRouter._infer_sub_intent(msg, _all_sub_intent_definitions(), "de") == expected


@pytest.mark.unit
@pytest.mark.parametrize("msg", [
    "mach das Licht an",
    "schalte die Heizung aus",
    "dimme das Wohnzimmer auf 50%",
    "spiel Musik im Bad",
])
def test_infer_sub_intent_does_not_fire_on_actuation(msg):
    from services.agent_router import AgentRouter
    assert AgentRouter._infer_sub_intent(msg, _all_sub_intent_definitions(), "de") is None
