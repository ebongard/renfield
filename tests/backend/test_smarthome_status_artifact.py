"""
Tests for the smart-home status → typed `table` artifact BUILDER.

``build_status_table`` is the pure builder behind the agent-callable
``internal.smart_home_overview`` tool (view=``status``). The tool-level tests
(dispatch, HA-unavailable, flag-off, view routing) live in
``test_device_widget_tools.py``; here we cover the builder in isolation:

  * it returns a well-formed `table` artifact for a status request given a mocked
    entity map, and the artifact passes ``validate_artifacts`` (within caps);
  * it returns ``None`` (graceful, prose-only for the caller) on empty input;
  * truncation over the row cap is honest (flagged, never silent).
"""
import pytest

from ha_glue.services.smarthome_status import (
    MAX_STATUS_ROWS,
    build_status_table,
)
from services.artifact_service import validate_artifacts


# --- shared fixtures --------------------------------------------------------

_ENTITY_MAP = [
    {"entity_id": "light.wohnzimmer", "friendly_name": "Wohnzimmer Decke",
     "domain": "light", "room": "wohnzimmer", "state": "on"},
    {"entity_id": "switch.kueche_kaffee", "friendly_name": "Kaffeemaschine",
     "domain": "switch", "room": "küche", "state": "off"},
    {"entity_id": "climate.bad", "friendly_name": "Bad Thermostat",
     "domain": "climate", "room": "bad", "state": "heat"},
    {"entity_id": "sensor.flur_temp", "friendly_name": "Flur Temperatur",
     "domain": "sensor", "room": None, "state": "21.4"},
]


# --- build_status_table -----------------------------------------------------

@pytest.mark.unit
def test_build_status_table_well_formed():
    art = build_status_table(_ENTITY_MAP, lang="de")
    assert art is not None
    assert art["kind"] == "table"
    assert art["id"].startswith("art_smarthome_status_")
    assert art["title"] == "Smart-Home-Status"
    assert art["data"]["columns"] == ["Raum", "Gerät", "Status"]
    rows = art["data"]["rows"]
    assert len(rows) == len(_ENTITY_MAP)
    # Every row is [room, device, state] — all strings.
    for r in rows:
        assert len(r) == 3
        assert all(isinstance(c, str) for c in r)
    # The device cell is "<domain-label>: <friendly_name>".
    flat = " | ".join(c for r in rows for c in r)
    assert "Licht: Wohnzimmer Decke" in flat
    assert "Schalter: Kaffeemaschine" in flat
    # State carried through verbatim.
    assert "heat" in flat
    # Roomless entity gets the "Ohne Raum" bucket.
    assert any(r[0] == "Ohne Raum" for r in rows)


@pytest.mark.unit
def test_build_status_table_english():
    art = build_status_table(_ENTITY_MAP, lang="en")
    assert art["data"]["columns"] == ["Room", "Device", "State"]
    assert art["title"] == "Smart home status"
    flat = " | ".join(c for r in art["data"]["rows"] for c in r)
    assert "Light: Wohnzimmer Decke" in flat
    assert any(r[0] == "No room" for r in art["data"]["rows"])


@pytest.mark.unit
def test_build_status_table_empty_returns_none():
    assert build_status_table([], lang="de") is None


@pytest.mark.unit
def test_build_status_table_truncates_over_cap():
    big = [
        {"entity_id": f"light.x{i}", "friendly_name": f"Lampe {i}",
         "domain": "light", "room": "wohnzimmer", "state": "on"}
        for i in range(MAX_STATUS_ROWS + 25)
    ]
    art = build_status_table(big, lang="de")
    assert len(art["data"]["rows"]) == MAX_STATUS_ROWS
    assert art["_truncated"] is True
    assert art["_total"] == MAX_STATUS_ROWS + 25


@pytest.mark.unit
def test_build_status_table_missing_friendly_name_falls_back_to_entity_id():
    art = build_status_table(
        [{"entity_id": "light.kueche_spots", "friendly_name": "",
          "domain": "light", "room": "küche", "state": "on"}],
        lang="de",
    )
    flat = " | ".join(c for r in art["data"]["rows"] for c in r)
    assert "kueche spots" in flat


# --- artifact passes the backend validator (caps gate) ----------------------

@pytest.mark.unit
def test_artifact_passes_validate_artifacts():
    art = build_status_table(_ENTITY_MAP, lang="de")
    # The internal hint keys must be stripped before emit — simulate the tool.
    art.pop("_truncated", None)
    art.pop("_total", None)
    cleaned = validate_artifacts([art])
    assert len(cleaned) == 1
    assert cleaned[0]["kind"] == "table"
    assert cleaned[0]["id"] == art["id"]
    # No internal underscore keys leak into the validated output.
    assert "_truncated" not in cleaned[0]
    assert "_total" not in cleaned[0]
