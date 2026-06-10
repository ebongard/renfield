"""Connection-robustness tests for the satellite WebSocket client.

Covers the fixes for the flaky-connectivity work (2026-06-10):
- `_register()` no longer blocks forever on a slow/hung backend (it had no
  timeout — a known wedge that left the satellite in CONNECTING with no reconnect)
- a failed heartbeat send now triggers reconnect immediately instead of being
  swallowed (which left a zombie connection until the WS ping timeout)
- the tighter ping / register-timeout knobs are plumbed through + have safe defaults
"""

import asyncio
import json

import pytest

from renfield_satellite.config import ServerConfig, load_config
from renfield_satellite.network.websocket_client import (
    ConnectionState,
    WebSocketClient,
)


def _client(**kw):
    return WebSocketClient(satellite_id="sat-test", room="TestRoom",
                           server_url="wss://x/ws/satellite", **kw)


class _HangingWS:
    async def send(self, _msg):
        pass

    async def recv(self):
        await asyncio.Event().wait()  # never resolves — simulates a hung backend


class _AckWS:
    def __init__(self, ack):
        self._ack = ack
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)

    async def recv(self):
        return json.dumps(self._ack)


class _FailingWS:
    async def send(self, _msg):
        raise ConnectionError("broken pipe")


# -- _register timeout --------------------------------------------------------

@pytest.mark.satellite
async def test_register_times_out_instead_of_wedging():
    c = _client(register_timeout=0.05)
    c._ws = _HangingWS()
    with pytest.raises(Exception, match="register ack not received"):
        await c._register()


@pytest.mark.satellite
async def test_register_succeeds_within_timeout():
    c = _client(register_timeout=5)
    c._ws = _AckWS({
        "type": "register_ack", "success": True,
        "config": {"wake_words": ["hey_renfield"], "threshold": 0.6},
        "protocol_version": "1.0",
    })
    got = {}
    c._on_connected = lambda sc: got.update(wake=sc.wake_words, thr=sc.threshold)
    await c._register()
    assert got["wake"] == ["hey_renfield"] and got["thr"] == 0.6


# -- heartbeat-failure → reconnect -------------------------------------------

@pytest.mark.satellite
async def test_heartbeat_send_failure_triggers_disconnect():
    c = _client(heartbeat_interval=0)  # don't wait between ticks
    c._running = True
    c._ws = _FailingWS()
    fired = {"n": 0}
    c._on_disconnected = lambda: fired.__setitem__("n", fired["n"] + 1)

    # Loop must fire the disconnect callback once and then exit (not spin/swallow).
    await asyncio.wait_for(c._heartbeat_loop(), timeout=2)

    assert fired["n"] == 1
    assert c._state == ConnectionState.DISCONNECTED


# -- knobs plumbed + safe defaults -------------------------------------------

@pytest.mark.satellite
def test_robustness_knobs_stored():
    c = _client(ping_interval=15, ping_timeout=8, register_timeout=12)
    assert c._ping_interval == 15
    assert c._ping_timeout == 8
    assert c._register_timeout == 12


@pytest.mark.satellite
def test_robustness_knobs_default_to_serverconfig_values():
    # Constructor defaults match ServerConfig so a no-arg caller gets the tuned
    # values (no drift between the two default sets).
    c = _client()
    assert c._ping_interval == ServerConfig().ping_interval == 15
    assert c._ping_timeout == ServerConfig().ping_timeout == 8
    assert c._register_timeout == 15


# -- config wiring ------------------------------------------------------------

@pytest.mark.satellite
def test_serverconfig_robustness_defaults():
    s = ServerConfig()
    assert s.ping_interval == 15
    assert s.ping_timeout == 8
    assert s.register_timeout == 15
    assert s.max_disconnected_seconds == 300


@pytest.mark.satellite
def test_config_loads_robustness_knobs(tmp_path):
    p = tmp_path / "satellite.yaml"
    p.write_text(
        "server:\n"
        "  url: wss://renfield.local/ws/satellite\n"
        "  ping_interval: 12\n"
        "  ping_timeout: 6\n"
        "  register_timeout: 9\n"
        "  max_disconnected_seconds: 240\n"
    )
    cfg = load_config(str(p))
    assert cfg.server.ping_interval == 12
    assert cfg.server.ping_timeout == 6
    assert cfg.server.register_timeout == 9
    assert cfg.server.max_disconnected_seconds == 240
