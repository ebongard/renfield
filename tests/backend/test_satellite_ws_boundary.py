"""Device-supplied satellite fields are typed at the boundary (#1279).

Every value a satellite reports (register, heartbeat, OTA frames) is JSON from a
LAN device and ends up in the roster the ADMIN list is built from. Pydantic v2
does not coerce a dict into ``str``, so a single wrong type used to raise at
response-build time -- and ``list_satellites`` had no per-entry guard, so ONE
bad satellite turned ``GET /api/satellites`` into a 500 for the whole fleet.

Two lines of defence, each tested here:

1. the boundary (``satellite_manager`` helpers, ``_register_fields``) brings
   every field into its declared type;
2. the list and detail routes render an entry that still cannot be built as a
   flagged placeholder instead of failing the response.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient

from ha_glue.services.satellite_manager import (
    SatelliteCapabilities,
    SatelliteInfo,
    SatelliteManager,
    device_bool,
    device_float,
    device_int,
    device_text,
    get_satellite_manager,
)

# ==========================================================================
# 1. The coercion helpers
# ==========================================================================


@pytest.mark.backend
@pytest.mark.unit
def test_device_text_bounds_and_stringifies():
    assert device_text(None) is None
    assert device_text("") is None
    assert device_text("abc") == "abc"
    assert device_text({"a": 1}) == "{'a': 1}"
    assert device_text("x" * 500) == "x" * 64
    assert device_text("x" * 500, 10) == "x" * 10


@pytest.mark.backend
@pytest.mark.unit
def test_device_int_refuses_bool_and_strings():
    assert device_int(5, 0) == 5
    assert device_int(True, 7) == 7
    assert device_int("5", 7) == 7
    assert device_int(1.5, 7) == 7


@pytest.mark.backend
@pytest.mark.unit
def test_device_bool_takes_only_real_booleans():
    assert device_bool(False, True) is False
    assert device_bool(1, True) is True  # the default, not the truthiness
    assert device_bool("no", False) is False


@pytest.mark.backend
@pytest.mark.unit
def test_device_float_takes_numbers_only():
    assert device_float(3) == 3.0
    assert device_float(2.5) == 2.5
    assert device_float(True) is None
    assert device_float("12.5") is None
    assert device_float({"x": 1}) is None


# ==========================================================================
# 2. Register: capabilities, room, language, version
# ==========================================================================


@pytest.mark.backend
@pytest.mark.unit
async def test_register_types_every_capability():
    mgr = SatelliteManager()
    ws = AsyncMock()
    await mgr.register(
        satellite_id="sat-1",
        room="Flur",
        websocket=ws,
        capabilities={
            "local_wakeword": "yes",
            "speaker": 0,
            "led_count": "drei",
            "button": None,
            "led_type": {"kind": "apa102"},
            "mic_count": True,
            "has_camera": "true",
            "has_display": [1],
            "has_enviro": 1,
        },
    )
    caps = mgr.get_satellite("sat-1").capabilities
    assert caps == SatelliteCapabilities(led_type="{'kind': 'apa102'}")


@pytest.mark.backend
@pytest.mark.unit
async def test_register_survives_non_dict_capabilities():
    mgr = SatelliteManager()
    await mgr.register(
        satellite_id="sat-1", room="Flur", websocket=AsyncMock(), capabilities=["led"]
    )
    assert mgr.get_satellite("sat-1").capabilities == SatelliteCapabilities()


@pytest.mark.backend
@pytest.mark.unit
def test_register_fields_types_the_frame():
    from ha_glue.api.websocket.satellite_handler import _register_fields
    from utils.config import settings

    room, caps, language, version = _register_fields({
        "room": {"name": "Küche"},
        "capabilities": "none",
        "language": ["de"],
        "version": 147,
    })
    # Wrong types become the DEFAULT, never text: `str({...})` would turn
    # into a room the DB then auto-creates.
    assert room == "Unknown Room"
    assert caps == {}
    assert language == settings.default_language
    assert version == "unknown"

    # Over-long strings are bounded.
    room, _caps, language, _version = _register_fields({"room": "R" * 500, "language": "x" * 50})
    assert room == "R" * 64
    assert language == "x" * 16

    # Absent fields fall back to the same defaults as before.
    room, caps, language, version = _register_fields({})
    assert (room, caps, language, version) == (
        "Unknown Room", {}, settings.default_language, "unknown"
    )


@pytest.mark.backend
@pytest.mark.unit
def test_register_fields_keeps_well_formed_values():
    from ha_glue.api.websocket.satellite_handler import _register_fields

    caps = {"led_count": 12, "audio_codec": "opus"}
    assert _register_fields({
        "room": "Arbeitszimmer", "capabilities": caps, "language": "en", "version": "1.4.12",
    }) == ("Arbeitszimmer", caps, "en", "1.4.12")


# ==========================================================================
# 3. Heartbeat: version and metrics
# ==========================================================================


def _manager_with_satellite() -> SatelliteManager:
    mgr = SatelliteManager()
    mgr.satellites["sat-1"] = SatelliteInfo(
        satellite_id="sat-1",
        room="Flur",
        websocket=MagicMock(),
        capabilities=SatelliteCapabilities(),
        version="1.4.6",
    )
    return mgr


@pytest.mark.backend
@pytest.mark.unit
def test_heartbeat_types_every_metric():
    mgr = _manager_with_satellite()
    mgr.update_heartbeat("sat-1", {
        "audio_rms": "laut",
        "audio_db": -12,
        "is_speech": "ja",
        "cpu_percent": 12.5,
        "memory_percent": {"x": 1},
        "temperature": True,
        "last_wakeword": "renfield",
        "session_count_1h": "viele",
        "error_count_1h": 3,
    })
    m = mgr.get_satellite("sat-1").metrics
    assert m == {
        "audio_rms": None,
        "audio_db": -12.0,
        "is_speech": None,
        "cpu_percent": 12.5,
        "memory_percent": None,
        "temperature": None,
        "last_wakeword": None,
        "session_count_1h": 0,
        "error_count_1h": 3,
    }


@pytest.mark.backend
@pytest.mark.unit
def test_heartbeat_keeps_well_formed_metrics():
    mgr = _manager_with_satellite()
    wake = {"keyword": "renfield", "confidence": 0.9}
    mgr.update_heartbeat("sat-1", {"is_speech": True, "last_wakeword": wake})
    m = mgr.get_satellite("sat-1").metrics
    assert m["is_speech"] is True
    assert m["last_wakeword"] == wake


@pytest.mark.backend
@pytest.mark.unit
def test_heartbeat_ignores_non_dict_metrics():
    mgr = _manager_with_satellite()
    mgr.update_heartbeat("sat-1", ["cpu", 12])
    assert mgr.get_satellite("sat-1").metrics == {}


@pytest.mark.backend
@pytest.mark.unit
def test_heartbeat_version_is_bounded():
    mgr = _manager_with_satellite()
    mgr.update_heartbeat("sat-1", None, {"v": "1.5"})
    assert mgr.get_satellite("sat-1").version == "{'v': '1.5'}"
    mgr.update_heartbeat("sat-1", None, "9" * 500)
    assert mgr.get_satellite("sat-1").version == "9" * 64


# ==========================================================================
# 4. The routes: one bad entry does not take the list down
# ==========================================================================


def _poisoned(sat_id: str) -> SatelliteInfo:
    """A roster entry that bypassed the boundary -- the second line of defence
    must hold on its own."""
    sat = SatelliteInfo(
        satellite_id=sat_id,
        room="Keller",
        websocket=MagicMock(),
        capabilities=SatelliteCapabilities(),
    )
    sat.update_stage = {"boom": 1}
    return sat


@pytest.fixture
def roster():
    """The global roster, emptied for the test and RESTORED afterwards."""
    manager = get_satellite_manager()
    saved = dict(manager.satellites)
    manager.satellites.clear()
    yield manager
    manager.satellites.clear()
    manager.satellites.update(saved)


@pytest.mark.integration
async def test_list_survives_one_corrupt_satellite(async_client: AsyncClient, roster):
    roster.satellites["good-sat"] = SatelliteInfo(
        satellite_id="good-sat",
        room="Wohnzimmer",
        websocket=MagicMock(),
        capabilities=SatelliteCapabilities(led_count=12),
    )
    bad = _poisoned("bad-sat")
    roster.satellites["bad-sat"] = bad

    response = await async_client.get("/api/satellites")

    assert response.status_code == 200
    data = response.json()
    assert data["total_count"] == 2
    by_id = {s["satellite_id"]: s for s in data["satellites"]}
    assert by_id["good-sat"]["corrupt"] is False
    assert by_id["good-sat"]["capabilities"]["led_count"] == 12
    entry = by_id["bad-sat"]
    assert entry["corrupt"] is True
    assert entry["room"] == "Keller"
    assert entry["state"] == "error"
    # Never dressed up as fresh: the durations say "unknown", and the
    # heartbeat timestamp is the backend's own, not "now".
    assert entry["heartbeat_ago_seconds"] == -1.0
    assert entry["uptime_seconds"] == -1.0
    assert entry["last_heartbeat"] == datetime.fromtimestamp(bad.last_heartbeat).isoformat()


@pytest.mark.integration
async def test_detail_of_a_corrupt_satellite_is_flagged_not_500(async_client: AsyncClient, roster):
    roster.satellites["bad-sat"] = _poisoned("bad-sat")

    response = await async_client.get("/api/satellites/bad-sat")

    assert response.status_code == 200
    assert response.json()["corrupt"] is True


@pytest.mark.backend
@pytest.mark.unit
def test_a_backend_fault_is_not_disguised_as_a_corrupt_entry(monkeypatch):
    """Only a bad roster VALUE becomes a placeholder; a fault elsewhere (update
    service, session store) must stay an honest error."""
    import ha_glue.api.routes.satellites as routes

    def _boom(*_a, **_k):
        raise RuntimeError("update service down")

    monkeypatch.setattr(routes, "_satellite_to_response", _boom)
    with pytest.raises(RuntimeError):
        routes._satellite_to_response_guarded("sat-1", {"room": "Flur"})


@pytest.mark.backend
@pytest.mark.unit
def test_corrupt_placeholder_never_fails_on_its_own_inputs():
    """The fallback itself must not raise, whatever the entry holds."""
    from ha_glue.api.routes.satellites import _corrupt_satellite_response

    resp = _corrupt_satellite_response(42, {"room": {"x": 1}, "last_heartbeat": "gestern"})
    assert resp.satellite_id == "42"
    assert resp.room == ""
    assert resp.corrupt is True
    assert resp.last_heartbeat == datetime.fromtimestamp(0)
    assert resp.heartbeat_ago_seconds == -1.0

    resp = _corrupt_satellite_response("sat-1", None)
    assert resp.satellite_id == "sat-1"
    assert resp.room == ""

    resp = _corrupt_satellite_response("sat-1", {"connected_at": 10**20, "last_heartbeat": True})
    assert resp.connected_at == datetime.fromtimestamp(0)
    assert resp.last_heartbeat == datetime.fromtimestamp(0)


# ==========================================================================
# 5. The register frame's satellite id
# ==========================================================================


@pytest.mark.backend
@pytest.mark.unit
@pytest.mark.parametrize("bad_id", [5, {"id": "flur"}, "", "x" * 129, None])
def test_register_satellite_id_refuses_unusable_ids(bad_id):
    from ha_glue.api.websocket.satellite_handler import _register_satellite_id

    assert _register_satellite_id({"satellite_id": bad_id}) is None


@pytest.mark.backend
@pytest.mark.unit
def test_register_satellite_id_keeps_valid_and_default_ids():
    from ha_glue.api.websocket.satellite_handler import _register_satellite_id

    assert _register_satellite_id({"satellite_id": "sat-flur"}) == "sat-flur"
    assert _register_satellite_id({}) == "unknown"
