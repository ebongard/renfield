"""Partials transcribe a trailing window, never the whole utterance (renfield #1613).

`_maybe_emit_partial` runs inline in the receive loop every ~0.7 s. Over the
WHOLE buffer that work grows with the utterance, and the 600 s cap allows ten
times the old 60 s. Partials now cover at most `PARTIAL_WINDOW_S` of audio from
a committed boundary; the text before it is kept as a prefix, because the
client REPLACES its preview with every `partial_transcript`
(`useVoiceStream.ts`: `setPartialText(text)`). The final transcription still
gets everything, and the server-VAD backstop (fed by confident partials) keeps
working on long dictation.

Environment: like `test_session_cap.py` (voice-server deps installed).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from itertools import pairwise
from unittest.mock import MagicMock

import numpy as np
import pytest
import voice_server.api.ws_voice as wsv
from voice_server.config import settings

SR = 16000
WINDOW = int(wsv.PARTIAL_WINDOW_S * SR)


@dataclass
class _Seg:
    text: str
    confidence: float


def _second(index: int, speech: bool = True) -> np.ndarray:
    """One second of PCM whose value encodes its index (0 = silence)."""
    return np.full(SR, float(index + 1) if speech else 0.0, dtype=np.float32)


class _FakeSTT:
    """Records what each call receives. A partial names the seconds it heard
    (`s<first>-<last>`) and is confident only if its LAST second has speech.
    `partial_limit_s` refuses longer partial inputs -- a transcription that
    cannot keep up with a growing buffer."""

    def __init__(self, partial_limit_s: float | None = None):
        self.partial_sizes: list[int] = []
        self.final_sizes: list[int] = []
        self.partial_limit_s = partial_limit_s
        self.last_language = "de"

    async def transcribe_stream(self, audio, language=None, *, partial=False):
        (self.partial_sizes if partial else self.final_sizes).append(audio.size)
        if partial and self.partial_limit_s and audio.size > self.partial_limit_s * SR:
            raise RuntimeError("too slow")
        heard = audio[audio > 0]
        speech = audio[-1] > 0
        text = f"s{int(heard[0]) - 1}-{int(audio[-1]) - 1}" if speech else ""
        yield _Seg(text=text, confidence=0.9 if speech else 0.1)


class _Speaker:
    async def embed(self, audio):
        return np.zeros(4, dtype=np.float32)


class _SinkWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, t):
        self.sent.append(json.loads(t))


def _partials(ws) -> list[str]:
    return [m["text"] for m in ws.sent if m.get("type") == "partial_transcript"]


async def _feed(state, stt, ws, seconds: int, speech: bool = True, start: int = 0):
    for i in range(start, start + seconds):
        state.audio_pcm.append(_second(i, speech))
        state.last_partial_at = 0.0  # every chunk is due for a partial
        await wsv._maybe_emit_partial(ws, state, stt)


async def test_a_short_utterance_is_transcribed_whole():
    """Below the window nothing changes: the partial covers everything."""
    stt, ws = _FakeSTT(), _SinkWS()
    state = wsv.SessionState(user_id="u")
    await _feed(state, stt, ws, 10)
    assert stt.partial_sizes[-1] == 10 * SR
    assert _partials(ws)[-1] == "s0-9"


async def test_past_the_window_a_partial_gets_at_most_the_window():
    stt, ws = _FakeSTT(), _SinkWS()
    state = wsv.SessionState(user_id="u")
    await _feed(state, stt, ws, 95)
    assert max(stt.partial_sizes) <= WINDOW
    assert stt.partial_sizes[-1] < 95 * SR


async def test_the_preview_keeps_its_beginning():
    """The client replaces its preview with each partial: the committed prefix
    must stay in front, so the preview grows instead of losing the start."""
    stt, ws = _FakeSTT(), _SinkWS()
    state = wsv.SessionState(user_id="u")
    await _feed(state, stt, ws, 95)
    texts = _partials(ws)
    last = texts[-1]
    assert last.startswith("s0-29 "), last
    assert last.endswith("-94"), last
    # The heard seconds are contiguous: every window starts where the previous ended.
    spans = [tuple(int(x) for x in part[1:].split("-")) for part in last.split()]
    assert spans[0][0] == 0
    for (_a, end), (start, _b) in pairwise(spans):
        assert start == end + 1, spans


async def test_silence_past_the_window_slides_without_a_prefix():
    stt, ws = _FakeSTT(), _SinkWS()
    state = wsv.SessionState(user_id="u")
    await _feed(state, stt, ws, 40, speech=False)
    await _feed(state, stt, ws, 5, start=40)
    assert max(stt.partial_sizes) <= WINDOW
    assert _partials(ws)[-1] == "s40-44"


async def test_the_final_transcription_gets_everything():
    stt, ws = _FakeSTT(), _SinkWS()
    state = wsv.SessionState(user_id="u")
    await _feed(state, stt, ws, 95)
    await wsv._finalize(ws, state, stt, _Speaker())
    assert stt.final_sizes == [95 * SR]
    # The next utterance starts with a fresh window.
    assert (state.partial_base_samples, state.partial_base_text) == (0, "")


# --------------------------------------------------------- the whole socket
class _Decoder:
    """Decodes chunk i to second i: speech unless the byte says silence."""

    def __init__(self, codec):
        self.n = 0
        self.pending: np.ndarray | None = None

    async def start(self):
        pass

    async def push(self, b):
        self.pending = _second(self.n, speech=b != b"\x00")
        self.n += 1

    async def take_pcm(self):
        out, self.pending = self.pending, None
        return out if out is not None else np.zeros(0, dtype=np.float32)

    async def flush(self):
        return np.zeros(0, dtype=np.float32)

    async def close(self):
        pass


class _ClockWS:
    def __init__(self, speech_s: int, silence_s: int, stt):
        state = MagicMock()
        state.stt, state.tts, state.speaker = stt, MagicMock(), _Speaker()
        self.app = MagicMock(state=state)
        self.scope: dict = {}
        self.closed = None
        self.sent: list[dict] = []
        self._frames = [{"type": "websocket.receive", "text": json.dumps(
            {"type": "session_start", "codec": "webm-opus"})}]
        self._frames += [{"type": "websocket.receive", "bytes": b"\x01"}] * speech_s
        self._frames += [{"type": "websocket.receive", "bytes": b"\x00"}] * silence_s

    async def accept(self):
        pass

    async def close(self, code=None, reason=None):
        self.closed = (code, reason)

    async def receive(self):
        if self._frames:
            return self._frames.pop(0)
        return {"type": "websocket.disconnect"}

    async def send_text(self, t):
        self.sent.append(json.loads(t))

    async def send_bytes(self, _b):
        pass


@pytest.fixture
def socket_env(monkeypatch):
    """One chunk = one second of audio = one second of wall clock."""
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(wsv, "AudioDecoder", _Decoder)
    clock = {"t": 1000.0}

    def _monotonic():
        clock["t"] += 0.5  # two reads per chunk: the partial and the loop
        return clock["t"]

    monkeypatch.setattr(wsv.time, "monotonic", _monotonic)
    finals: list[float] = []
    real_finalize = wsv._finalize

    async def _finalize(ws, state, stt, speaker):
        finals.append(wsv._accumulated_seconds(state))
        await real_finalize(ws, state, stt, speaker)

    monkeypatch.setattr(wsv, "_finalize", _finalize)
    wsv._active_sessions = 0
    yield finals
    wsv._active_sessions = 0


async def test_the_backstop_does_not_fire_during_90_s_of_speech(socket_env):
    """The partial STT here cannot handle more than the window plus a little
    -- with whole-buffer partials it fails from ~32 s on, `last_speech_at`
    freezes and the backstop cuts the dictation (the negative control)."""
    finals = socket_env
    stt = _FakeSTT(partial_limit_s=wsv.PARTIAL_WINDOW_S + 2)
    ws = _ClockWS(speech_s=90, silence_s=0, stt=stt)
    await wsv.ws_voice(ws, token=None)
    assert finals == [], "no auto-finalize while the speaker keeps talking"
    assert ws.closed is None
    assert max(stt.partial_sizes) <= WINDOW


async def test_the_backstop_still_fires_after_speech_stops(socket_env):
    finals = socket_env
    stt = _FakeSTT(partial_limit_s=wsv.PARTIAL_WINDOW_S + 2)
    ws = _ClockWS(speech_s=90, silence_s=5, stt=stt)
    await wsv.ws_voice(ws, token=None)
    assert len(finals) == 1 and 90 < finals[0] < 95
    assert stt.final_sizes == [int(finals[0] * SR)], "the final pass covers the whole utterance"
