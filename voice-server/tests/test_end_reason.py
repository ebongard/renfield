"""`final_transcript` says why the utterance ended (renfield #1613).

Three places finalize: the server-VAD backstop, the MAX_UTTERANCE_SECONDS cap
and the client's `stt_flush`. Each emits its own content-free `end_reason`; the
client refines `client_flush` (its own silence VAD or a manual stop). The other
fields are unchanged, so a client that ignores the new one keeps working.

Harness from `test_partial_window.py`: one chunk = one second of audio = one
second of wall clock. Environment: voice-server deps installed.
"""
from __future__ import annotations

import json

import pytest
import voice_server.api.ws_voice as wsv
from test_partial_window import _ClockWS, _FakeSTT, socket_env  # noqa: F401
from voice_server.config import settings


def _finals(ws) -> list[dict]:
    return [m for m in ws.sent if m.get("type") == "final_transcript"]


async def test_the_server_vad_backstop_says_server_vad(socket_env):
    ws = _ClockWS(speech_s=10, silence_s=5, stt=_FakeSTT())
    await wsv.ws_voice(ws, token=None)
    (final,) = _finals(ws)
    assert final["end_reason"] == "server_vad"


async def test_the_cap_says_max_utterance(socket_env, monkeypatch):
    monkeypatch.setattr(settings, "max_utterance_seconds", 5)
    ws = _ClockWS(speech_s=10, silence_s=0, stt=_FakeSTT())
    await wsv.ws_voice(ws, token=None)
    (final,) = _finals(ws)
    assert final["end_reason"] == "max_utterance"
    assert ws.closed == (1011, "max_utterance_seconds (5s)")


async def test_a_client_flush_says_client_flush(socket_env):
    ws = _ClockWS(speech_s=3, silence_s=0, stt=_FakeSTT())
    ws._frames.append({"type": "websocket.receive", "text": json.dumps({"type": "stt_flush"})})
    await wsv.ws_voice(ws, token=None)
    (final,) = _finals(ws)
    assert final["end_reason"] == "client_flush"


async def test_the_other_fields_are_unchanged(socket_env):
    """Backward compatible: only one key is added."""
    ws = _ClockWS(speech_s=3, silence_s=0, stt=_FakeSTT())
    ws._frames.append({"type": "websocket.receive", "text": json.dumps({"type": "stt_flush"})})
    await wsv.ws_voice(ws, token=None)
    (final,) = _finals(ws)
    assert set(final) == {
        "type", "text", "language", "audio_duration_s", "speaker_embedding", "end_reason",
    }
    assert final["audio_duration_s"] == pytest.approx(3.0)


async def test_an_unknown_reason_is_a_programming_error():
    state = wsv.SessionState(user_id="u")
    with pytest.raises(ValueError):
        await wsv._finalize(None, state, _FakeSTT(), None, end_reason="because")
