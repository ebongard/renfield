"""/ws/satellite and /ws/device keep their socket through a long inline turn (#1570).

Both run a whole turn inline in their receive loop while the client keeps
sending: a satellite a heartbeat every 30 s and a BLE report every ~3 s, a web
device a heartbeat every 30 s. Under uvicorn's ``websockets-sansio`` protocol
the first such frame paused reading and the server closed its own socket with
1011 after its keepalive budget. These tests run the real endpoints under a
REAL in-process uvicorn (``uvicorn_harness``) with a 0.3 s ping budget:

* during a turn far longer than that budget, the client sends more state
  frames than the reader's first, count-bounded queue held (64), paced well
  inside the 50/s rate limit -- as a satellite does over minutes;
* the socket stays open, the turn ends, every frame is processed afterwards in
  arrival order (no frame is merged: BLE reports feed a per-report filter, see
  the reader's docstring), and the REAL rate limiter accepts all of them,
  because it judges arrival time, not the burst in which the busy handler
  takes them;
* a genuine flood (audio during the turn) ends the connection with 1013.

The turn is stubbed at the first coroutine the handler awaits inline:
``SatelliteManager.set_session_state(PROCESSING)`` for a satellite's
``audio_end``, ``_process_text_input`` for a device's ``text``.
"""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from websockets.asyncio.client import connect

import api.websocket.shared as ws_shared
from services.websocket_rate_limiter import WSRateLimiter
from tests.backend.uvicorn_harness import PING, expect_close, recv_json, serve

pytestmark = [pytest.mark.backend, pytest.mark.integration]

TURN = 10 * PING  # far beyond the ping budget (2 x PING)
STATE_FRAMES = 70  # more than the first reader's 64-frame queue
PACE = TURN * 0.8 / STATE_FRAMES  # ~29 frames/s: inside 50/s, all within the turn


@asynccontextmanager
async def _no_db():
    yield MagicMock()


def _fresh_rate_limiter() -> WSRateLimiter:
    return WSRateLimiter(per_second=50, per_minute=1000, enabled=True)


# --------------------------------------------------------------------------
# /ws/satellite
# --------------------------------------------------------------------------

@pytest.fixture
def satellite_app(monkeypatch):
    import ha_glue.api.websocket.satellite_handler as sh
    import ha_glue.services.led_dimming_service as led
    import ha_glue.services.presence_service as presence
    import ha_glue.services.satellite_manager as sm
    from ha_glue.services.satellite_manager import SatelliteState

    mgr = sm.SatelliteManager()
    monkeypatch.setattr(sm, "get_satellite_manager", lambda: mgr)
    monkeypatch.setattr(sh, "authenticate_websocket", AsyncMock(return_value={"type": "anonymous"}))
    limiter = MagicMock()
    limiter.can_connect.return_value = (True, "")
    monkeypatch.setattr(sh, "get_connection_limiter", lambda: limiter)
    monkeypatch.setattr(sh, "get_rate_limiter", _fresh_rate_limiter)
    monkeypatch.setattr(sh.settings, "satellite_enrollment_enabled", False)
    monkeypatch.setattr(sh.ha_glue_settings, "rooms_auto_create_from_satellite", False)
    monkeypatch.setattr(sh.ha_glue_settings, "presence_enabled", True)
    monkeypatch.setattr(sh, "AsyncSessionLocal", _no_db)
    config = MagicMock()
    config.to_satellite_config.return_value = {}
    wakeword = MagicMock()
    wakeword.get_config = AsyncMock(return_value=config)
    monkeypatch.setattr(sh, "get_wakeword_config_manager", lambda: wakeword)
    dimming = MagicMock()
    dimming.get_current_led_brightness.return_value = 1.0
    monkeypatch.setattr(led, "get_led_dimming_service", lambda: dimming)

    reports: list[list] = []
    scanners: list = []
    forgotten: list[str] = []

    class _Presence:
        async def process_ble_report(self, *, satellite_id, room_id, devices, room_name=None,
                                     scanner=None):
            reports.append(devices)
            scanners.append(scanner)

        def forget_scanner_status(self, satellite_id):
            forgotten.append(satellite_id)

    monkeypatch.setattr(presence, "get_presence_service", lambda: _Presence())

    events: list[str] = []
    original = mgr.set_session_state

    async def _turn(session_id, state):
        if state == SatelliteState.PROCESSING:
            events.append("turn-start")
            await asyncio.sleep(TURN)  # the inline turn: STT, agent, TTS
            events.append("turn-end")
        await original(session_id, state)

    monkeypatch.setattr(mgr, "set_session_state", _turn)

    app = FastAPI()
    app.include_router(sh.router)
    app.state.mgr, app.state.reports, app.state.events = mgr, reports, events
    app.state.scanners, app.state.forgotten = scanners, forgotten
    return app


def _sat(frame: dict) -> str:
    return json.dumps(frame)


SAT_ID = "sat-keepalive"
REGISTER = {
    "type": "register", "satellite_id": SAT_ID, "room": "Flur",
    "capabilities": {"led_count": 3}, "version": "1.4.6",
}


def _heartbeat(i: int) -> dict:
    return {"type": "heartbeat", "status": "idle", "uptime_seconds": i,
            "metrics": {"cpu_percent": float(i), "audio_db": -50.0, "audio_rms": 0.01}}


def _ble(i: int) -> dict:
    return {"type": "ble_presence", "satellite_id": SAT_ID,
            "devices": [{"mac": f"AA:BB:CC:DD:EE:{i % 256:02X}", "rssi": -60 - i % 20}]}


async def _until(ws, predicate, timeout):
    """Read frames until predicate(frame) holds; returns every frame read."""
    seen = []
    async with asyncio.timeout(timeout):
        while True:
            frame = await recv_json(ws, timeout)
            seen.append(frame)
            if predicate(frame):
                return seen


async def _start_turn(ws, app):
    await ws.send(_sat(REGISTER))
    await _until(ws, lambda f: f.get("type") == "register_ack", 5)
    await ws.send(_sat({"type": "wakeword_detected", "keyword": "test", "confidence": 0.9,
                        "session_id": "sess-1"}))
    await ws.send(_sat({"type": "audio_end", "session_id": "sess-1", "reason": "silence"}))
    async with asyncio.timeout(5):
        while "turn-start" not in app.state.events:
            await asyncio.sleep(0.01)


async def test_satellite_state_frames_during_a_long_turn_keep_the_socket(satellite_app):
    app = satellite_app
    async with serve(app) as base, connect(f"{base}/ws/satellite", ping_interval=None) as ws:
        await _start_turn(ws, app)
        # What a satellite sends while it waits for its answer.
        for i in range(STATE_FRAMES):
            await ws.send(_sat(_heartbeat(i) if i % 10 == 0 else _ble(i)))
            await asyncio.sleep(PACE)
        assert "turn-end" not in app.state.events  # all of them arrived mid-turn
        heartbeats = sum(1 for i in range(STATE_FRAMES) if i % 10 == 0)

        acks = 0
        async with asyncio.timeout(TURN + 5):
            while acks < heartbeats:
                frame = await recv_json(ws, TURN + 5)
                if frame.get("type") == "heartbeat_ack":
                    acks += 1
                elif frame.get("type") == "error":
                    pytest.fail(f"a frame was refused: {frame}")
        assert "turn-end" in app.state.events

        # The state reflects the latest heartbeat of the turn.
        sat = app.state.mgr.get_satellite(SAT_ID)
        assert sat.metrics["cpu_percent"] == float(max(i for i in range(STATE_FRAMES) if i % 10 == 0))

        # Still open: one more round trip moves `last_heartbeat`.
        before = time.time()
        await ws.send(_sat(_heartbeat(999)))
        await _until(ws, lambda f: f.get("type") == "heartbeat_ack", 5)
        assert sat.last_heartbeat >= before
        assert sat.metrics["cpu_percent"] == 999.0

    # Every BLE report reached presence, in arrival order, none merged.
    assert app.state.reports == [
        _ble(i)["devices"] for i in range(STATE_FRAMES) if i % 10 != 0
    ]


async def test_a_ble_report_hands_the_scanner_status_on(satellite_app):
    """#1551 stage 2 C: the status of a satellite >= 1.4.23 reaches the presence
    service, also in a report without devices; a (re)registration drops the old one."""
    app = satellite_app
    status = {"mode": "continuous", "running": True, "cutoff_dbm": -100, "last_advert_age_s": 1.5}
    async with serve(app) as base, connect(f"{base}/ws/satellite", ping_interval=None) as ws:
        await ws.send(_sat(REGISTER))
        await _until(ws, lambda f: f.get("type") == "register_ack", 5)
        await ws.send(_sat({"type": "ble_presence", "satellite_id": SAT_ID, "devices": [],
                            "scanner": status}))
        await ws.send(_sat(_ble(1)))  # an older satellite: no status
        async with asyncio.timeout(5):
            while len(app.state.scanners) < 2:
                await asyncio.sleep(0.01)
    assert app.state.forgotten == [SAT_ID]
    assert app.state.scanners == [status, None]
    assert app.state.reports[0] == []


async def test_satellite_audio_flood_during_a_turn_closes_with_1013(satellite_app, monkeypatch):
    monkeypatch.setattr(ws_shared, "CLIENT_FRAME_BUDGET_BYTES", 64 * 1024)
    app = satellite_app
    async with serve(app) as base, connect(f"{base}/ws/satellite", ping_interval=None) as ws:
        await _start_turn(ws, app)
        chunk = json.dumps({"type": "audio", "session_id": "sess-2", "sequence": 0,
                            "chunk": "A" * 4000})
        for _ in range(40):  # ~160 KB of a second recording while the first runs
            await ws.send(chunk)
        rcvd = await expect_close(ws, TURN + 5)
    assert (rcvd.code, rcvd.reason) == (1013, "client frame budget exceeded")


# --------------------------------------------------------------------------
# /ws/device
# --------------------------------------------------------------------------

@pytest.fixture
def device_app(monkeypatch):
    import ha_glue.api.websocket.device_handler as dh
    import ha_glue.services.room_service as rs
    from ha_glue.services.device_manager import DeviceManager

    mgr = DeviceManager()
    monkeypatch.setattr(dh, "get_device_manager", lambda: mgr)
    monkeypatch.setattr(dh, "authenticate_websocket", AsyncMock(return_value={"type": "anonymous"}))
    limiter = MagicMock()
    limiter.can_connect.return_value = (True, "")
    monkeypatch.setattr(dh, "get_connection_limiter", lambda: limiter)
    monkeypatch.setattr(dh, "get_rate_limiter", _fresh_rate_limiter)
    monkeypatch.setattr(dh, "AsyncSessionLocal", _no_db)

    class _Rooms:
        def __init__(self, _db):
            pass

        async def register_device(self, **_kwargs):
            return None

        async def set_device_online(self, *_args):
            return None

    monkeypatch.setattr(rs, "RoomService", _Rooms)
    config = MagicMock()
    config.to_satellite_config.return_value = {}
    wakeword = MagicMock()
    wakeword.get_config = AsyncMock(return_value=config)
    monkeypatch.setattr(dh, "get_wakeword_config_manager", lambda: wakeword)

    events: list[str] = []

    async def _turn(_app, _mgr, _session_id, text, *args, **kwargs):
        events.append(f"start:{text}")
        await asyncio.sleep(TURN)
        events.append(f"end:{text}")

    monkeypatch.setattr(dh, "_process_text_input", _turn)

    app = FastAPI()
    app.include_router(dh.router)
    app.state.mgr, app.state.events = mgr, events
    return app


async def test_device_heartbeats_during_a_long_turn_keep_the_socket(device_app):
    app = device_app
    async with serve(app) as base, connect(f"{base}/ws/device", ping_interval=None) as ws:
        await ws.send(json.dumps({"type": "register", "device_id": "panel-1",
                                  "device_type": "web_panel", "room": "Flur"}))
        await _until(ws, lambda f: f.get("type") == "register_ack", 5)
        await ws.send(json.dumps({"type": "text", "content": "slow"}))
        async with asyncio.timeout(5):
            while "start:slow" not in app.state.events:
                await asyncio.sleep(0.01)
        for _ in range(STATE_FRAMES):
            await ws.send(json.dumps({"type": "heartbeat", "status": "processing"}))
            await asyncio.sleep(PACE)
        assert "end:slow" not in app.state.events

        acks = 0
        async with asyncio.timeout(TURN + 5):
            while acks < STATE_FRAMES:
                frame = await recv_json(ws, TURN + 5)
                if frame.get("type") == "heartbeat_ack":
                    acks += 1
                elif frame.get("type") == "error":
                    pytest.fail(f"a frame was refused: {frame}")
        assert app.state.events == ["start:slow", "end:slow"]

        # Still open, and a heartbeat moves `last_heartbeat` as before.
        before = time.time()
        await ws.send(json.dumps({"type": "heartbeat", "status": "idle"}))
        await _until(ws, lambda f: f.get("type") == "heartbeat_ack", 5)
        device = app.state.mgr.get_device("panel-1")
        assert device is not None and device.last_heartbeat >= before
