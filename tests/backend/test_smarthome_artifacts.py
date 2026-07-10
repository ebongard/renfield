"""
Tests for the three additional Lane A artifact BUILDERS (keyvalue / list / chart)
in ``ha_glue.services.smarthome_artifacts``.

These builders back the agent-callable ``internal.smart_home_overview`` tool (views
``sensors`` / ``active_devices`` / ``devices_per_room``). The tool-level tests
(dispatch, HA-unavailable, flag-off, view routing) live in
``test_device_widget_tools.py``; here we cover each builder in isolation:

  * builds a well-formed artifact of the right KIND for its data (mocked entity
    map), and the artifact passes ``validate_artifacts`` (within the backend caps);
  * returns ``None`` (graceful — the tool answers prose-only) on empty / no-match
    data;
  * truncation over the per-kind cap is honest (flagged, never silent).
"""
import pytest

from ha_glue.services.smarthome_artifacts import (
    MAX_ACTIVE_ITEMS,
    MAX_ROOM_BARS,
    MAX_SENSOR_PAIRS,
    build_active_list,
    build_devices_per_room_chart,
    build_sensor_keyvalue,
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


# ===========================================================================
# Builder 1: sensors → keyvalue
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


# ===========================================================================
# Builder 2: active_devices → list
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


# ===========================================================================
# Builder 3: devices_per_room → chart
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
