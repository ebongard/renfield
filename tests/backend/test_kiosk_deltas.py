"""Backend tests for the Phase-2 kiosk liveness deltas.

Each producer pushes a CONTENT-FREE delta through the merged
``broadcast_kiosk_event`` at the right transition — and, critically, does NOT
over-fire on a non-transition (the presence-chatter / now-playing / tool-health
diff guards). Every producer does a lazy ``from api.websocket.kiosk_handler
import broadcast_kiosk_event`` at call time, so patching the module attribute
intercepts the push.

Covers:
  * satellite liveness  — ``satellite_online`` on register, ``satellite_offline``
    on unregister and heartbeat-timeout.
  * ``presence_changed`` — fires on an occupant-set change, NOT on a bare RSSI
    tick with no membership change.
  * ``now_playing_changed`` — diff-gated: fires on a real change, silent on a
    repeat.
  * ``tool_health_changed`` — ``_set_connected`` fires only on a transition.
  * ``weather_updated`` — ``refresh_and_push_kiosk_weather`` diff-gated.

Run on the .159 build box (CI is non-functional): see
``memory/reference_test_runner_159.md``.
"""
from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock, patch

import pytest


def _patch_broadcast() -> AsyncMock:
    """Patch the hub broadcast and return the mock. Callers use it inside a
    ``with`` block (context manager) so the patch is scoped to the test."""
    return patch("api.websocket.kiosk_handler.broadcast_kiosk_event", new=AsyncMock())


def _events(mock: AsyncMock) -> list[dict]:
    return [c.args[0] for c in mock.call_args_list]


def _events_of_type(mock: AsyncMock, type_: str) -> list[dict]:
    return [e for e in _events(mock) if e.get("type") == type_]


# ==========================================================================
# 1. Satellite liveness
# ==========================================================================


@pytest.mark.backend
@pytest.mark.asyncio
async def test_register_broadcasts_satellite_online():
    from ha_glue.services.satellite_manager import SatelliteManager

    mgr = SatelliteManager()
    with _patch_broadcast() as bcast:
        await mgr.register(
            satellite_id="sat-küche",
            room="Küche",
            websocket=AsyncMock(),
            capabilities={},
        )

    online = _events_of_type(bcast, "satellite_online")
    assert len(online) == 1
    ev = online[0]
    assert ev["satellite_id"] == "sat-küche"
    assert ev["room"] == "Küche"
    assert ev["online"] is True
    # Content-free: only ids/names/state keys, never an utterance or user id.
    assert set(ev) == {"type", "satellite_id", "room", "room_id", "online"}


@pytest.mark.backend
@pytest.mark.asyncio
async def test_unregister_broadcasts_satellite_offline():
    from ha_glue.services.satellite_manager import SatelliteManager

    mgr = SatelliteManager()
    await mgr.register("sat-1", "Room", AsyncMock(), {})
    with _patch_broadcast() as bcast:
        await mgr.unregister("sat-1")

    offline = _events_of_type(bcast, "satellite_offline")
    assert len(offline) == 1
    assert offline[0]["satellite_id"] == "sat-1"
    assert offline[0]["online"] is False


@pytest.mark.backend
@pytest.mark.asyncio
async def test_stale_unregister_does_not_drop_reconnected_satellite():
    """Fast reconnect: a NEW connection re-registers sat-1 before the OLD
    socket's finally→unregister runs. The identity-guarded unregister(old_ws)
    must NOT delete the live (new) entry nor push a spurious satellite_offline."""
    from ha_glue.services.satellite_manager import SatelliteManager

    mgr = SatelliteManager()
    old_ws, new_ws = AsyncMock(), AsyncMock()
    await mgr.register("sat-1", "Room", old_ws, {})
    await mgr.register("sat-1", "Room", new_ws, {})  # reconnect replaces the entry
    assert mgr.satellites["sat-1"].websocket is new_ws

    with _patch_broadcast() as bcast:
        await mgr.unregister("sat-1", websocket=old_ws)  # the DYING old socket

    assert "sat-1" in mgr.satellites  # live entry preserved
    assert mgr.satellites["sat-1"].websocket is new_ws
    assert _events_of_type(bcast, "satellite_offline") == []  # no spurious offline


@pytest.mark.backend
@pytest.mark.asyncio
async def test_cleanup_stale_broadcasts_satellite_offline():
    from ha_glue.services.satellite_manager import SatelliteManager

    mgr = SatelliteManager()
    await mgr.register("sat-stale", "Room", AsyncMock(), {})
    # Force the heartbeat far into the past so cleanup_stale evicts it.
    mgr.satellites["sat-stale"].last_heartbeat = 0.0
    with _patch_broadcast() as bcast:
        await mgr.cleanup_stale()

    assert "sat-stale" not in mgr.satellites
    offline = _events_of_type(bcast, "satellite_offline")
    assert len(offline) == 1
    assert offline[0]["satellite_id"] == "sat-stale"
    assert offline[0]["online"] is False


# ==========================================================================
# 2. presence_changed
# ==========================================================================


def _presence_service():
    """A fresh PresenceService with the attributes the hot path touches, without
    importing settings (mirrors test_presence_service's fixture)."""
    from ha_glue.services.presence_service import PresenceService

    svc = PresenceService.__new__(PresenceService)
    svc._mac_to_user = {"AA:BB:CC:DD:EE:01": 1}
    svc._mac_to_method = {"AA:BB:CC:DD:EE:01": "ble"}
    svc._presence = {}
    svc._sightings = {}
    svc._hysteresis_threshold = 2
    svc._stale_timeout = 120.0
    svc._rssi_threshold = -80
    svc._filter_enabled = False
    svc._filter_alpha_up = 0.5
    svc._filter_alpha_down = 0.1
    svc._filter_fresh_seconds = 35.0
    svc._switch_enter_margin_db = 8.0
    svc._room_names = {}
    svc._user_names = {1: "alice"}
    svc._user_first_names = {}
    svc._user_last_names = {}
    svc._pending_events = []
    return svc


@pytest.mark.backend
@pytest.mark.asyncio
async def test_presence_changed_fires_on_occupant_change():
    svc = _presence_service()
    with _patch_broadcast() as bcast:
        await svc.process_ble_report(
            satellite_id="sat-kitchen",
            room_id=10,
            devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -50}],
            room_name="Kitchen",
        )

    changed = _events_of_type(bcast, "presence_changed")
    assert len(changed) == 1
    ev = changed[0]
    assert ev["people_present"] == 1
    assert ev["occupied_rooms"] == 1
    assert ev["rooms"] == [{"room_id": 10, "room_name": "Kitchen", "occupants": 1}]
    # Content-free: no user ids anywhere in the payload.
    assert set(ev) == {"type", "rooms", "people_present", "occupied_rooms"}


@pytest.mark.backend
@pytest.mark.asyncio
async def test_presence_changed_does_not_fire_on_bare_rssi_tick():
    """The §6 chatter guard: once the user holds a room, a further identical
    sighting is a bare RSSI tick with no membership change → NO broadcast."""
    svc = _presence_service()
    # First report establishes the room (fires once).
    await svc.process_ble_report(
        satellite_id="sat-kitchen",
        room_id=10,
        devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -50}],
        room_name="Kitchen",
    )
    # Second identical report — same room, no occupant-set change.
    with _patch_broadcast() as bcast:
        await svc.process_ble_report(
            satellite_id="sat-kitchen",
            room_id=10,
            devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -50}],
            room_name="Kitchen",
        )

    assert _events_of_type(bcast, "presence_changed") == []


# ==========================================================================
# 3. now_playing_changed
# ==========================================================================


@pytest.mark.backend
@pytest.mark.asyncio
async def test_now_playing_broadcast_diff_gated():
    from ha_glue.services.media_follow_service import MediaFollowService, MediaType

    svc = MediaFollowService()
    svc.register_playback(
        user_id=1,
        room_id=10,
        room_name="Wohnzimmer",
        media_type=MediaType.RADIO,
        station_name="BBC",
        title="BBC",
    )
    # register_playback scheduled a fire-and-forget push (real hub = no-op with
    # no clients, but it may set _last_now_playing). Drain it and reset the
    # diff-gate so this test exercises the helper deterministically.
    await asyncio.sleep(0)
    if svc._np_bg_tasks:
        await asyncio.gather(*svc._np_bg_tasks)
    svc._last_now_playing = None
    with _patch_broadcast() as bcast:
        # First push: the set changed vs the (None) baseline.
        await svc._broadcast_now_playing_if_changed()
        assert len(_events_of_type(bcast, "now_playing_changed")) == 1
        ev = _events_of_type(bcast, "now_playing_changed")[0]
        assert ev["sessions"] and ev["sessions"][0]["room"] == "Wohnzimmer"
        # Second push with nothing changed: diff-gated → silent.
        await svc._broadcast_now_playing_if_changed()
        assert len(_events_of_type(bcast, "now_playing_changed")) == 1


@pytest.mark.backend
@pytest.mark.asyncio
async def test_register_playback_schedules_now_playing_push():
    from ha_glue.services.media_follow_service import MediaFollowService, MediaType

    svc = MediaFollowService()
    with _patch_broadcast() as bcast:
        svc.register_playback(
            user_id=1,
            room_id=10,
            room_name="Wohnzimmer",
            media_type=MediaType.RADIO,
            title="BBC",
        )
        # The sync mutation site scheduled a fire-and-forget task; let it run.
        await asyncio.sleep(0)
        await asyncio.gather(*svc._np_bg_tasks)

    assert len(_events_of_type(bcast, "now_playing_changed")) == 1


# ==========================================================================
# 4. tool_health_changed
# ==========================================================================


def _fake_state(name: str, connected: bool = False):
    return types.SimpleNamespace(
        connected=connected, config=types.SimpleNamespace(name=name)
    )


@pytest.mark.backend
@pytest.mark.asyncio
async def test_tool_health_broadcast_on_transition():
    from services.mcp_client import MCPManager

    mgr = MCPManager()
    state = _fake_state("weather", connected=False)
    with _patch_broadcast() as bcast:
        mgr._set_connected(state, True)  # False → True (transition)
        await asyncio.sleep(0)
        await asyncio.gather(*mgr._health_bg_tasks)

    events = _events_of_type(bcast, "tool_health_changed")
    assert len(events) == 1
    # The delta carries the folded health + reason code the snapshot does (added
    # with the node-degraded-health feature) — a healthy reconnect is health
    # 'healthy' with no impaired_code.
    assert events[0] == {
        "type": "tool_health_changed",
        "server": "weather",
        "connected": True,
        "health": "healthy",
        "impaired_code": None,
    }
    assert state.connected is True


@pytest.mark.backend
@pytest.mark.asyncio
async def test_tool_health_no_broadcast_on_same_value():
    from services.mcp_client import MCPManager

    mgr = MCPManager()
    state = _fake_state("weather", connected=True)
    with _patch_broadcast() as bcast:
        mgr._set_connected(state, True)  # True → True (no transition)
        await asyncio.sleep(0)
        if mgr._health_bg_tasks:
            await asyncio.gather(*mgr._health_bg_tasks)

    assert _events_of_type(bcast, "tool_health_changed") == []


# ==========================================================================
# 5. weather_updated
# ==========================================================================


@pytest.mark.backend
@pytest.mark.asyncio
async def test_weather_updated_diff_gated(monkeypatch):
    import api.websocket.kiosk_data as cc

    # Reset the module-level push-dedup state so the test is order-independent.
    monkeypatch.setattr(cc, "_weather_last_pushed", None, raising=False)

    weather = cc.KioskWeather(
        location="Korschenbroich",
        temp=18.0,
        unit="°C",
        code=1,
        condition="clear",
    )

    async def _fake_compute(_mgr, force=False):
        return weather

    monkeypatch.setattr(cc, "compute_kiosk_weather", _fake_compute)

    with _patch_broadcast() as bcast:
        await cc.refresh_and_push_kiosk_weather(mcp_manager=object())
        assert len(_events_of_type(bcast, "weather_updated")) == 1
        ev = _events_of_type(bcast, "weather_updated")[0]
        assert ev["weather"]["temp"] == 18.0
        # Same reading again → diff-gated silent.
        await cc.refresh_and_push_kiosk_weather(mcp_manager=object())
        assert len(_events_of_type(bcast, "weather_updated")) == 1


@pytest.mark.backend
@pytest.mark.asyncio
async def test_weather_updated_pushes_none_when_unavailable(monkeypatch):
    import api.websocket.kiosk_data as cc

    monkeypatch.setattr(cc, "_weather_last_pushed", {"temp": 5.0}, raising=False)

    async def _fake_compute(_mgr, force=False):
        return None

    monkeypatch.setattr(cc, "compute_kiosk_weather", _fake_compute)

    with _patch_broadcast() as bcast:
        await cc.refresh_and_push_kiosk_weather(mcp_manager=object())

    events = _events_of_type(bcast, "weather_updated")
    assert len(events) == 1
    assert events[0]["weather"] is None  # tile hides itself
