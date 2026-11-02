"""Real frames through the satellite receive loop (#1279, #1280).

The static check in ``test_satellite_ota_run_id.py`` pins that the three OTA
branches call the run gate; these tests push actual frames through
``satellite_websocket`` so the gate, ``_register_fields`` and the satellite-id
refusal are exercised at runtime. Everything outside the loop that would need
a database or a network (auth, wake-word config, LED dimming, room sync) is
replaced by a stand-in.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest


class _FakeSocket:
    """A WebSocket that plays a script: frames (dicts) and callables.

    A callable runs at its place in the script — that is how a test acts
    between two frames (start a run, snapshot the roster).
    """

    def __init__(self, script):
        self._script = list(script)
        self.client = MagicMock(host="192.0.2.50")
        self.app = MagicMock()
        self.accept = AsyncMock()
        self.close = AsyncMock()
        self.sent: list[dict] = []

    async def send_json(self, payload):
        self.sent.append(payload)

    async def receive(self):
        while self._script:
            item = self._script.pop(0)
            if callable(item):
                item()
                continue
            return {"type": "websocket.receive", "text": json.dumps(item)}
        return {"type": "websocket.disconnect", "code": 1000}


@pytest.fixture
def loop_env(monkeypatch):
    """Stub every dependency of the receive loop that is not under test; hand
    back a fresh manager the loop uses."""
    import ha_glue.api.websocket.satellite_handler as sh
    import ha_glue.services.led_dimming_service as led
    import ha_glue.services.satellite_manager as sm

    mgr = sm.SatelliteManager()
    monkeypatch.setattr(sm, "get_satellite_manager", lambda: mgr)
    monkeypatch.setattr(sh, "authenticate_websocket", AsyncMock(return_value={"type": "anonymous"}))
    limiter = MagicMock()
    limiter.can_connect.return_value = (True, "")
    monkeypatch.setattr(sh, "get_connection_limiter", lambda: limiter)
    monkeypatch.setattr(sh, "_rate_verdict", lambda *a, **k: (True, ""))
    monkeypatch.setattr(sh.settings, "satellite_enrollment_enabled", False)
    monkeypatch.setattr(sh.ha_glue_settings, "rooms_auto_create_from_satellite", False)
    monkeypatch.setattr(sh.ha_glue_settings, "presence_enabled", False)

    @asynccontextmanager
    async def _no_db():
        yield MagicMock()

    monkeypatch.setattr(sh, "AsyncSessionLocal", _no_db)
    config = MagicMock()
    config.to_satellite_config.return_value = {}
    wakeword = MagicMock()
    wakeword.get_config = AsyncMock(return_value=config)
    monkeypatch.setattr(sh, "get_wakeword_config_manager", lambda: wakeword)
    dimming = MagicMock()
    dimming.get_current_led_brightness.return_value = 1.0
    monkeypatch.setattr(led, "get_led_dimming_service", lambda: dimming)
    return mgr


def _register_frame(**overrides):
    frame = {
        "type": "register", "satellite_id": "sat-loop", "room": "Flur",
        "capabilities": {"led_count": 3}, "version": "1.4.6",
    }
    frame.update(overrides)
    return frame


@pytest.mark.backend
async def test_ota_frames_are_gated_by_the_run_id_in_the_loop(loop_env):
    from ha_glue.api.websocket.satellite_handler import satellite_websocket
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = loop_env
    run = {}
    seen = {}

    def _start_run():
        run["id"] = mgr.begin_update_run("sat-loop")

    def _snapshot(label):
        def _take():
            sat = mgr.get_satellite("sat-loop")
            seen[label] = (sat.update_status, sat.update_stage, sat.version)
        return _take

    ws = _FakeSocket([
        _register_frame(room={"name": "Flur"}),
        _start_run,
        # A straggler of an older run on every one of the three branches.
        {"type": "update_progress", "stage": "rolling_back", "progress": 0,
         "message": "Rolling back...", "run_id": "old-run"},
        {"type": "update_failed", "stage": "installing", "error": "alt",
         "rolled_back": True, "run_id": "old-run"},
        {"type": "update_complete", "success": True, "old_version": "1.4.5",
         "new_version": "9.9.9", "run_id": "old-run"},
        _snapshot("after_stragglers"),
    ])
    # The run's own frames, inserted once its id exists.
    def _own_frames():
        ws._script[0:0] = [
            {"type": "update_progress", "stage": "installing", "progress": 70,
             "message": "", "run_id": run["id"]},
            _snapshot("after_progress"),
            {"type": "update_complete", "success": True, "old_version": "1.4.6",
             "new_version": "1.5.0", "run_id": run["id"]},
            _snapshot("after_complete"),
        ]

    ws._script.append(_own_frames)

    await satellite_websocket(ws, token=None)

    assert seen["after_stragglers"] == (US.IN_PROGRESS, "initiating", "1.4.6")
    assert seen["after_progress"] == (US.IN_PROGRESS, "installing", "1.4.6")
    assert seen["after_complete"] == (US.COMPLETED, "completed", "1.5.0")
    # _register_fields typed the room on its way in.
    assert ws.sent[0]["type"] == "register_ack"


@pytest.mark.backend
async def test_legacy_ota_frames_without_run_id_still_apply_in_the_loop(loop_env):
    from ha_glue.api.websocket.satellite_handler import satellite_websocket
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = loop_env
    seen = {}

    def _take():
        sat = mgr.get_satellite("sat-loop")
        seen["state"] = (sat.update_status, sat.update_stage, sat.update_error)

    ws = _FakeSocket([
        _register_frame(),
        lambda: mgr.begin_update_run("sat-loop"),
        {"type": "update_failed", "stage": "verifying", "error": "bad signature",
         "rolled_back": True},
        _take,
    ])

    await satellite_websocket(ws, token=None)

    assert seen["state"] == (US.FAILED, "verifying", "bad signature")


@pytest.mark.backend
@pytest.mark.parametrize("bad_id", [5, {"id": "flur"}])
async def test_register_with_a_non_string_id_is_refused(loop_env, bad_id):
    from ha_glue.api.websocket.satellite_handler import satellite_websocket

    mgr = loop_env
    ws = _FakeSocket([_register_frame(satellite_id=bad_id)])

    await satellite_websocket(ws, token=None)

    assert mgr.satellites == {}
    assert ws.sent and ws.sent[0]["type"] == "error"
    assert "invalid-satellite-id" in json.dumps(ws.sent[0])
    ws.close.assert_awaited()
    assert ws.close.await_args.kwargs.get("reason") == "invalid-satellite-id"
