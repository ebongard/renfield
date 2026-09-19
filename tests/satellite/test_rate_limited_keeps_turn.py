"""
``RATE_LIMITED`` beendet keinen Sprachzug (#1284).

Der Server meldet damit, dass EIN Frame verworfen wurde — Verbindung und Sitzung
sind intakt. Der Satellit reichte die Meldung aber an ``on_error`` weiter, und
``Satellite._on_error`` setzt bei jedem Serverfehler die laufende Sitzung zurueck.
Ein einziger abgewiesener Heartbeat oder BLE-Bericht mitten im Audio-Schwall
beendete so den Zug; der Sprecher bekam keine Antwort.

Zusaetzlich schlief der Client 1 s IN der Empfangsschleife — das bremste nie das
Senden, es verzoegerte nur eingehende Frames (TTS, Bestaetigungen).
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
    """Durch die ECHTE Verdrahtung: Client -> Satellite._on_error / _on_rate_limited.

    Auf dem alten Code ruft der Client ``on_error``, und ``Satellite._on_error``
    plant bei laufendem Zug ``_reset_session`` ein — genau das darf nicht mehr
    geschehen.
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
    # ... aber sichtbar bleibt es: die Drosselung zaehlt in die Fehlermetrik.
    sat._error_counter.record.assert_called_once()


@pytest.mark.satellite
async def test_echter_fehler_setzt_den_zug_weiterhin_zurueck():
    """Gegenprobe zur Verdrahtung oben — sonst bewiese der Test nichts."""
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
    """Nur RATE_LIMITED ist ausgenommen — echte Sitzungsfehler setzen weiter zurueck."""
    client = _client()
    errors = []
    client.on_error(errors.append)

    await client._handle_message({"type": "error", "code": code, "message": "boom"})

    assert errors == [f"{code}: boom"]
