"""Concurrency-cap accounting for /ws/voice (review M4 + follow-up).

The `_active_sessions` counter must stay balanced: a capacity reject must not
touch it, and — critically — a setup failure AFTER the increment must still be
decremented (the increment now lives inside the try whose finally decrements,
so a slot can't leak permanently and wedge the cap).

Environment: importing `voice_server.api.ws_voice` pulls in the STT/speaker/
decoder modules, so this runs where the voice-server deps are installed (the
.159 build box or the voice-server image), not a bare dev machine.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import voice_server.api.ws_voice as wsv
from voice_server.config import settings


class _RaisingState:
    """app.state whose service attributes are not yet initialized."""
    def __getattr__(self, name):
        raise RuntimeError(f"app.state.{name} not initialized")


class _GoodState:
    stt = MagicMock()
    tts = MagicMock()
    speaker = MagicMock()


class _FakeApp:
    def __init__(self, state):
        self.state = state


class _FakeWS:
    def __init__(self, app):
        self.app = app
        self.accepted = False
        self.closed: tuple | None = None
        # Real ASGI websockets always carry a scope; registry auth reads
        # scope["server"] to detect the anonymous listener port.
        self.scope: dict = {}

    async def accept(self):
        self.accepted = True

    async def close(self, code=None, reason=None):
        self.closed = (code, reason)

    async def receive(self):
        # End the session immediately so the handler runs through its finally.
        return {"type": "websocket.disconnect"}

    async def send_text(self, _t):
        pass

    async def send_bytes(self, _b):
        pass


@pytest.fixture(autouse=True)
def _reset_counter(monkeypatch):
    monkeypatch.setattr(settings, "auth_required", False)  # token-less anon connect
    wsv._active_sessions = 0
    yield
    wsv._active_sessions = 0


@pytest.mark.asyncio
async def test_counter_balanced_on_setup_failure():
    """A failure resolving app.state services (after the increment) must still
    decrement — otherwise the slot leaks for the lifetime of the process."""
    ws = _FakeWS(_FakeApp(_RaisingState()))
    await wsv.ws_voice(ws, token=None)
    assert ws.accepted is True
    assert wsv._active_sessions == 0  # no leak


@pytest.mark.asyncio
async def test_counter_balanced_on_normal_session():
    ws = _FakeWS(_FakeApp(_GoodState()))
    await wsv.ws_voice(ws, token=None)
    assert wsv._active_sessions == 0


@pytest.mark.asyncio
async def test_capacity_reject_leaves_counter_untouched(monkeypatch):
    monkeypatch.setattr(settings, "max_concurrent_sessions", 1)
    wsv._active_sessions = 1  # at capacity
    ws = _FakeWS(_FakeApp(_GoodState()))
    await wsv.ws_voice(ws, token=None)
    assert wsv._active_sessions == 1  # reject must not increment/decrement
    assert ws.closed == (1013, "server at capacity")
