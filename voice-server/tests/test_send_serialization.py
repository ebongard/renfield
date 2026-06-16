"""Regression test: concurrent websocket sends are serialized.

TTS streams audio frames from a background asyncio.Task (`_run_tts`) while the
receive loop concurrently emits `partial_transcript` / `final_transcript` frames
via `_send_json`. Two coroutines doing `await ws.send_*` on the SAME websocket at
once tripped the `websockets` legacy-protocol drain assert
(`_drain_helper: assert waiter is None or waiter.cancelled()`), which surfaced as
an empty-message `AssertionError` reported to the client as `tts_failed:` (no
detail). The fix is a per-connection send lock guarding the two raw send sites
(`_send_json` send_text + `_run_tts` send_bytes).

This test reproduces the overlap window: a FakeWebSocket whose sends yield to the
event loop and flag any second send that begins while one is still in flight.
Without the lock the gather() below interleaves a TTS send_bytes with a
_send_json send_text → overlap flagged. With the lock they serialize.

Environment: like test_cancel_ack, importing `voice_server.api.ws_voice` pulls in
the service modules, so run where the voice-server deps are installed (the
voice-server image / the .159 build box). No Piper / Whisper / GPU is exercised.

    cd voice-server && pip install -r requirements.txt -r requirements-dev.txt
    pytest tests/test_send_serialization.py
"""
from __future__ import annotations

import asyncio
import json
import uuid

from voice_server.api.ws_voice import SessionState, _send_json, _spawn_tts


class OverlapDetectingWebSocket:
    """Records frames AND flags any two sends that overlap in flight.

    Each send sets an `_active` flag, yields to the loop (so a concurrent send
    can interleave if it isn't serialized), then clears the flag. A send that
    finds `_active` already set on entry means two drains overlapped — exactly
    what the websockets assert guards against.
    """

    def __init__(self) -> None:
        self.text_frames: list[dict] = []
        self.bytes_frames: list[bytes] = []
        self._active = False
        self.overlap_detected = False

    async def _guarded(self, record) -> None:
        if self._active:
            self.overlap_detected = True
        self._active = True
        await asyncio.sleep(0)  # yield — an unserialized concurrent send slips in here
        record()
        await asyncio.sleep(0)
        self._active = False

    async def send_text(self, text: str) -> None:
        await self._guarded(lambda: self.text_frames.append(json.loads(text)))

    async def send_bytes(self, data: bytes) -> None:
        await self._guarded(lambda: self.bytes_frames.append(data))

    def frames_of_type(self, frame_type: str) -> list[dict]:
        return [f for f in self.text_frames if f.get("type") == frame_type]


class MultiFrameTTS:
    """Fake TTSService that streams several frames, yielding between each so a
    concurrent _send_json has a window to interleave."""

    async def stream_sentences(self, text, request_id, language=None):
        for i in range(6):
            await asyncio.sleep(0)
            yield f"frame-{i}".encode()


async def test_concurrent_tts_and_transcript_sends_are_serialized() -> None:
    ws = OverlapDetectingWebSocket()
    state = SessionState(user_id="u1")
    rid = str(uuid.uuid4())

    await _spawn_tts(ws, state, {"request_id": rid, "text": "hallo welt"}, MultiFrameTTS())
    tts_task = state.tts_tasks[rid]

    async def spam_partials() -> None:
        # Mirror the receive loop emitting partial transcripts WHILE TTS streams.
        for i in range(12):
            await _send_json(ws, {"type": "partial_transcript", "text": f"p{i}", "confidence": 0.9})
            await asyncio.sleep(0)

    await asyncio.gather(tts_task, spam_partials())

    # The whole point: no two sends overlapped.
    assert ws.overlap_detected is False
    # And both streams actually delivered (the lock didn't drop/deadlock anything).
    assert ws.bytes_frames == [f"frame-{i}".encode() for i in range(6)]
    assert ws.frames_of_type("tts_done") == [{"type": "tts_done", "request_id": rid}]
    assert len(ws.frames_of_type("partial_transcript")) == 12
    assert rid not in state.tts_tasks  # _wrapped finally cleaned up
