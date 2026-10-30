"""
``RATE_LIMITED`` ends no voice turn (#1284).

With it the server reports that ONE frame was dropped -- connection and session
are intact. But the satellite passed the message on to ``on_error``, and
``Satellite._on_error`` resets the running session on every server error. So a
single rejected heartbeat or BLE report in the middle of the audio burst ended
the turn; the speaker got no answer.

On top of that the client slept 1 s INSIDE the receive loop -- that never slowed
sending down, it only delayed incoming frames (TTS, confirmations).
"""

import asyncio
import time

import pytest
from renfield_satellite.network.websocket_client import WebSocketClient


def _client() -> WebSocketClient:
    return WebSocketClient(satellite_id="sat-test", room="Test Room")


@pytest.mark.satellite
async def test_rate_limited_erreicht_den_fehler_callback_nicht():
    client = _client()
    errors = []
    client.on_error(errors.append)

    await client._handle_message({"type": "error", "code": "RATE_LIMITED", "message": "max 50 per second"})

    assert errors == []


@pytest.mark.satellite
async def test_rate_limited_setzt_den_zug_des_satelliten_nicht_zurueck():
    """Through the REAL wiring: Client -> Satellite._on_error / _on_rate_limited.

    On the old code the client calls ``on_error``, and ``Satellite._on_error``
    schedules ``_reset_session`` while a turn is running -- which is exactly
    what must not happen any more.
    """
    from unittest.mock import MagicMock

    from renfield_satellite.satellite import Satellite, SatelliteState

    sat = object.__new__(Satellite)
    sat._session_id = "sess-1"
    sat._state = SatelliteState.LISTENING
    sat._error_counter = MagicMock()
    sat.leds = MagicMock()
    sat._schedule_async = MagicMock()
    sat._reset_session = MagicMock(return_value=None)

    client = _client()
    client.on_error(sat._on_error)
    client.on_rate_limited(sat._on_rate_limited)

    await client._handle_message({"type": "error", "code": "RATE_LIMITED", "message": "x"})

    sat._schedule_async.assert_not_called()
    sat.leds.set_pattern.assert_not_called()
    assert sat._session_id == "sess-1"
    # ... but it stays visible: the throttling counts towards the error metric.
    sat._error_counter.record.assert_called_once()


@pytest.mark.satellite
async def test_echter_fehler_setzt_den_zug_weiterhin_zurueck():
    """Counter-check on the wiring above -- otherwise the test would prove nothing."""
    from unittest.mock import MagicMock

    from renfield_satellite.satellite import Satellite, SatelliteState

    sat = object.__new__(Satellite)
    sat._session_id = "sess-1"
    sat._state = SatelliteState.LISTENING
    sat._error_counter = MagicMock()
    sat.leds = MagicMock()
    sat._schedule_async = MagicMock()
    sat._reset_session = MagicMock(return_value=None)

    client = _client()
    client.on_error(sat._on_error)
    client.on_rate_limited(sat._on_rate_limited)

    await client._handle_message({"type": "error", "code": "SESSION_ERROR", "message": "x"})

    sat._schedule_async.assert_called_once()


@pytest.mark.satellite
async def test_rate_limited_blockiert_die_empfangsschleife_nicht():
    client = _client()

    started = time.monotonic()
    await asyncio.wait_for(
        client._handle_message({"type": "error", "code": "RATE_LIMITED", "message": "x"}),
        timeout=0.5,
    )

    assert time.monotonic() - started < 0.5


@pytest.mark.satellite
@pytest.mark.parametrize("code", ["BUFFER_FULL", "SESSION_ERROR", "UNAUTHORIZED", "UNKNOWN"])
async def test_andere_fehler_erreichen_den_callback_weiterhin(code):
    """Only RATE_LIMITED is exempt -- real session errors still reset."""
    client = _client()
    errors = []
    client.on_error(errors.append)

    await client._handle_message({"type": "error", "code": code, "message": "boom"})

    assert errors == [f"{code}: boom"]
