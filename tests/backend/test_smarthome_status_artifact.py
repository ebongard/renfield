"""
Tests for the first Lane A artifact producer: smart-home status → typed `table`.

Covers (per the task brief):
  * the producer returns a well-formed `table` artifact for a status request
    given mocked HA state, and it passes ``validate_artifacts`` (within caps);
  * NO artifact (graceful, prose-only) when HA is unavailable / empty;
  * the dispatch handler is INERT (declines) when the flag is off, and declines
    for any role/sub_intent that isn't ``smart_home/status``;
  * intent detection (``_infer_sub_intent``) fires on the German status triggers
    and does NOT fire on an actuation command ("mach das Licht an").
"""
import pytest

from ha_glue.services.smarthome_status import (
    MAX_STATUS_ROWS,
    build_status_table,
    ha_dispatch_smarthome_status,
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


class _FakeHAClient:
    """Stand-in for HomeAssistantClient with a canned get_entity_map()."""

    def __init__(self, entity_map):
        self._entity_map = entity_map

    async def get_entity_map(self):
        return self._entity_map


def _patch_ha(monkeypatch, entity_map=None, *, raises=False):
    """Patch the HomeAssistantClient the handler imports lazily."""
    import ha_glue.integrations.homeassistant as ha_mod

    def _factory():
        if raises:
            raise RuntimeError("HA unreachable")
        return _FakeHAClient(entity_map if entity_map is not None else [])

    monkeypatch.setattr(ha_mod, "HomeAssistantClient", _factory)


def _enable_flag(monkeypatch, value=True):
    from utils.config import settings
    monkeypatch.setattr(settings, "artifacts_typed_enabled", value, raising=False)


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
    # The internal hint keys must be stripped before emit — simulate the handler.
    art.pop("_truncated", None)
    art.pop("_total", None)
    cleaned = validate_artifacts([art])
    assert len(cleaned) == 1
    assert cleaned[0]["kind"] == "table"
    assert cleaned[0]["id"] == art["id"]
    # No internal underscore keys leak into the validated output.
    assert "_truncated" not in cleaned[0]
    assert "_total" not in cleaned[0]


# --- dispatch handler: happy path -------------------------------------------

@pytest.mark.unit
async def test_dispatch_returns_artifact_for_status(monkeypatch):
    _enable_flag(monkeypatch, True)
    _patch_ha(monkeypatch, _ENTITY_MAP)
    res = await ha_dispatch_smarthome_status(
        role="smart_home", sub_intent="status",
        handler_name="smarthome_status", message="Wie ist der Status im Haus?",
        lang="de",
    )
    assert res is not None
    assert res["handled"] is True
    assert res["card"] is None
    assert isinstance(res["answer"], str) and res["answer"]
    arts = res["artifacts"]
    assert len(arts) == 1
    # Internal hint keys stripped before return.
    assert "_truncated" not in arts[0]
    assert "_total" not in arts[0]
    # And it validates against the backend caps gate.
    assert len(validate_artifacts(arts)) == 1


@pytest.mark.unit
async def test_dispatch_truncation_mentioned_in_prose(monkeypatch):
    _enable_flag(monkeypatch, True)
    big = [
        {"entity_id": f"light.x{i}", "friendly_name": f"Lampe {i}",
         "domain": "light", "room": "wohnzimmer", "state": "on"}
        for i in range(MAX_STATUS_ROWS + 10)
    ]
    _patch_ha(monkeypatch, big)
    res = await ha_dispatch_smarthome_status(
        role="smart_home", sub_intent="status", handler_name="smarthome_status",
        message="Übersicht der Geräte", lang="de",
    )
    # Prose must honestly say the table is truncated (never silent).
    assert "insgesamt" in res["answer"].lower()
    assert len(res["artifacts"][0]["data"]["rows"]) == MAX_STATUS_ROWS


# --- dispatch handler: HA unavailable / empty → prose only ------------------

@pytest.mark.unit
async def test_dispatch_ha_unavailable_is_graceful(monkeypatch):
    _enable_flag(monkeypatch, True)
    _patch_ha(monkeypatch, raises=True)
    res = await ha_dispatch_smarthome_status(
        role="smart_home", sub_intent="status", handler_name="smarthome_status",
        message="Status im Haus", lang="de",
    )
    assert res["handled"] is True
    assert res["card"] is None
    # No artifact (graceful), prose only — never a crash, never an empty table.
    assert "artifacts" not in res
    assert isinstance(res["answer"], str) and res["answer"]


@pytest.mark.unit
async def test_dispatch_empty_entity_map_is_graceful(monkeypatch):
    _enable_flag(monkeypatch, True)
    _patch_ha(monkeypatch, [])
    res = await ha_dispatch_smarthome_status(
        role="smart_home", sub_intent="status", handler_name="smarthome_status",
        message="Status", lang="de",
    )
    assert res["handled"] is True
    assert "artifacts" not in res


# --- dispatch handler: inert / declines --------------------------------------

@pytest.mark.unit
async def test_dispatch_inert_when_flag_off(monkeypatch):
    _enable_flag(monkeypatch, False)
    # If the flag gate were broken this would crash (no HA patched) — assert it
    # declines WITHOUT touching HA.
    res = await ha_dispatch_smarthome_status(
        role="smart_home", sub_intent="status", handler_name="smarthome_status",
        message="Status", lang="de",
    )
    assert res is None


@pytest.mark.unit
async def test_dispatch_declines_other_role(monkeypatch):
    _enable_flag(monkeypatch, True)
    res = await ha_dispatch_smarthome_status(
        role="media", sub_intent="status", handler_name="smarthome_status",
        message="Status", lang="de",
    )
    assert res is None


@pytest.mark.unit
async def test_dispatch_declines_other_sub_intent(monkeypatch):
    _enable_flag(monkeypatch, True)
    res = await ha_dispatch_smarthome_status(
        role="smart_home", sub_intent="volume_control",
        handler_name="smarthome_status", message="lauter", lang="de",
    )
    assert res is None


@pytest.mark.unit
async def test_dispatch_declines_wrong_handler_name(monkeypatch):
    _enable_flag(monkeypatch, True)
    res = await ha_dispatch_smarthome_status(
        role="smart_home", sub_intent="status", handler_name="something_else",
        message="Status", lang="de",
    )
    assert res is None


# --- intent detection: _infer_sub_intent over the configured keywords --------

def _status_definitions():
    """The sub_intent definition exactly as agent_router parses it from YAML."""
    return {
        "status": {
            "de": (
                "Status, Übersicht, Überblick, Zustand, wie ist der status im haus, "
                "smart-home-status, geräteübersicht, alle geräte anzeigen, was ist an, "
                "gesamtüberblick"
            ),
            "en": (
                "status, overview, summary, state of the house, smart home status, "
                "device overview, show all devices, what is on, whole house status"
            ),
            # dispatch key is ignored by _infer_sub_intent (it reads only de/en).
            "dispatch": {"type": "handler", "handler": "smarthome_status"},
        }
    }


@pytest.mark.unit
@pytest.mark.parametrize("msg", [
    "Wie ist der Status im Haus?",
    "Zeig mir den Smart-Home-Status",
    "Übersicht der Geräte bitte",
    "Gib mir einen Überblick",
])
def test_infer_sub_intent_fires_on_status_triggers(msg):
    from services.agent_router import AgentRouter
    assert AgentRouter._infer_sub_intent(msg, _status_definitions(), "de") == "status"


@pytest.mark.unit
@pytest.mark.parametrize("msg", [
    "mach das Licht an",
    "schalte die Heizung aus",
    "dimme das Wohnzimmer auf 50%",
    "ist das Fenster im Bad offen?",
])
def test_infer_sub_intent_does_not_fire_on_actuation(msg):
    from services.agent_router import AgentRouter
    # No status/overview keyword → None → falls through to the normal agent loop.
    assert AgentRouter._infer_sub_intent(msg, _status_definitions(), "de") is None
