"""The /ws/voice utterance cap is a far, configurable bound (renfield #1613).

It was a hardcoded 60 s and cut long browser dictation mid-sentence. Now it is
`settings.max_utterance_seconds` (env MAX_UTTERANCE_SECONDS, default 600 s):
a guard against a forgotten open microphone, not a dictation limit. At the cap
the server still finalizes what it has, then closes the socket with a reason,
and logs the reason as a content-free line.

Environment: like `test_session_cap.py`, importing `voice_server.api.ws_voice`
pulls in the STT/speaker/decoder modules, so this runs where the voice-server
deps are installed (the .159 build box or the voice-server image).
"""
from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock

import numpy as np
import pytest
import voice_server.api.ws_voice as wsv
from voice_server.config import Settings, settings


class _OneSecondDecoder:
    """Every pushed chunk decodes to one second of 16 kHz mono PCM."""

    def __init__(self, codec):
        self.codec = codec

    async def start(self):
        pass

    async def push(self, _b):
        pass

    async def take_pcm(self):
        return np.zeros(16000, dtype=np.float32)

    async def close(self):
        pass


class _GoodState:
    stt = MagicMock()
    tts = MagicMock()
    speaker = MagicMock()


class _FakeApp:
    state = _GoodState()


class _FakeWS:
    """Plays `session_start` and then `chunks` binary frames, then disconnects."""

    def __init__(self, chunks: int):
        self.app = _FakeApp()
        self.scope: dict = {}
        self.closed: tuple | None = None
        self._frames = [{"type": "websocket.receive", "text": json.dumps(
            {"type": "session_start", "codec": "webm-opus"})}]
        self._frames += [{"type": "websocket.receive", "bytes": b"\x00"}] * chunks

    async def accept(self):
        pass

    async def close(self, code=None, reason=None):
        self.closed = (code, reason)

    async def receive(self):
        if self._frames and self.closed is None:
            return self._frames.pop(0)
        return {"type": "websocket.disconnect"}

    async def send_text(self, _t):
        pass

    async def send_bytes(self, _b):
        pass


@pytest.fixture
def finalized(monkeypatch):
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(wsv, "AudioDecoder", _OneSecondDecoder)

    async def _no_partial(*_a, **_k):
        return None

    monkeypatch.setattr(wsv, "_maybe_emit_partial", _no_partial)
    calls: list[float] = []

    async def _record_finalize(_ws, state, _stt, _speaker):
        calls.append(wsv._accumulated_seconds(state))

    monkeypatch.setattr(wsv, "_finalize", _record_finalize)
    wsv._active_sessions = 0
    yield calls
    wsv._active_sessions = 0


def test_the_default_is_a_far_bound_not_60_seconds():
    assert Settings.model_fields["max_utterance_seconds"].default == 600


def test_the_cap_reads_the_environment(monkeypatch):
    monkeypatch.setenv("MAX_UTTERANCE_SECONDS", "900")
    # auth_required=False only so the placeholder-key guard does not refuse.
    assert Settings(auth_required=False).max_utterance_seconds == 900


async def test_below_the_cap_nothing_is_cut(finalized, monkeypatch):
    """Negative control: 61 s of audio used to be cut at 60 s; now it is not."""
    monkeypatch.setattr(settings, "max_utterance_seconds", 600)
    ws = _FakeWS(chunks=61)
    await wsv.ws_voice(ws, token=None)
    assert finalized == []
    assert ws.closed is None


async def test_at_the_cap_it_finalizes_then_closes_with_a_reason(finalized, monkeypatch, caplog):
    monkeypatch.setattr(settings, "max_utterance_seconds", 3)
    ws = _FakeWS(chunks=10)
    with caplog.at_level(logging.WARNING, logger=wsv.logger.name):
        await wsv.ws_voice(ws, token=None)

    assert finalized == [3.0], "finalized exactly once, at the cap, with the audio so far"
    assert ws.closed == (1011, "max_utterance_seconds (3s)")
    lines = [r.getMessage() for r in caplog.records if "max_utterance" in r.getMessage()]
    assert len(lines) == 1
    assert "reason=max_utterance cap=3s" in lines[0]
    assert wsv._active_sessions == 0
