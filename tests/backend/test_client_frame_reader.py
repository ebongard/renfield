"""ClientFrameReader and the arrival-time rate limit, without a server (#1570).

The behaviour under a real uvicorn is in test_ws_keepalive_during_turn.py and
test_ws_keepalive_device_satellite.py; these pin the reader's contract.
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import WebSocketDisconnect

import api.websocket.shared as ws_shared
from api.websocket.shared import ClientFrameReader
from services.websocket_rate_limiter import WSRateLimiter

pytestmark = [pytest.mark.unit]


class _Socket:
    """Plays ASGI messages; blocks once the script is empty."""

    def __init__(self, *messages):
        self._script = list(messages)
        self.closed_with = None
        self._idle = asyncio.Event()

    async def receive(self):
        if self._script:
            item = self._script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        await self._idle.wait()  # never set: an idle client
        raise AssertionError("unreachable")

    async def close(self, code=1000, reason=None):
        self.closed_with = (code, reason)


def _text(payload) -> dict:
    return {"type": "websocket.receive", "text": json.dumps(payload)}


DISCONNECT = {"type": "websocket.disconnect", "code": 1001, "reason": "away"}


async def test_json_frames_in_order_then_a_sticky_disconnect():
    reader = ClientFrameReader(_Socket(_text({"n": 1}), _text({"n": 2}), DISCONNECT), decode="json", label="t")
    try:
        assert await reader.next() == {"n": 1}
        assert await reader.next() == {"n": 2}
        for _ in range(3):  # every call after the end repeats it
            with pytest.raises(WebSocketDisconnect) as exc:
                await reader.next()
            assert exc.value.code == 1001
    finally:
        await reader.aclose()
    assert reader.task.done()


async def test_raw_mode_returns_the_disconnect_message_again_and_again():
    reader = ClientFrameReader(_Socket(_text({"n": 1}), DISCONNECT), decode="raw", label="t")
    try:
        assert (await reader.next())["text"] == '{"n": 1}'
        assert await reader.next() == DISCONNECT
        assert await reader.next() == DISCONNECT
    finally:
        await reader.aclose()


async def test_a_receive_error_is_raised_in_place_and_stays():
    boom = RuntimeError("receive failed")
    reader = ClientFrameReader(_Socket(_text({"n": 1}), boom), decode="json", label="t")
    try:
        assert await reader.next() == {"n": 1}
        for _ in range(2):
            with pytest.raises(RuntimeError) as exc:
                await reader.next()
            assert exc.value is boom
    finally:
        await reader.aclose()


async def test_a_frame_that_does_not_parse_ends_the_stream_and_the_reading():
    reader = ClientFrameReader(
        _Socket({"type": "websocket.receive", "text": "{not json"}, _text({"n": 2})),
        decode="json", label="t",
    )
    try:
        with pytest.raises(json.JSONDecodeError):
            await reader.next()
        with pytest.raises(json.JSONDecodeError):  # sticky: {"n": 2} is never handed out
            await reader.next()
        await asyncio.wait({reader.task}, timeout=1)
        assert reader.task.done()
    finally:
        await reader.aclose()


async def test_arrived_at_is_the_arrival_not_the_hand_over():
    reader = ClientFrameReader(_Socket(_text({"n": 1})), decode="json", label="t")
    try:
        await asyncio.sleep(0.05)  # the reader takes the frame now ...
        handed_over = datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=0.05)
        await asyncio.sleep(0.1)  # ... the handler only later
        assert await reader.next() == {"n": 1}
        assert reader.arrived_at is not None and reader.arrived_at < handed_over
    finally:
        await reader.aclose()


async def test_a_flood_over_the_budget_closes_once_and_discards_the_waiting_frames(monkeypatch):
    monkeypatch.setattr(ws_shared, "CLIENT_FRAME_BUDGET_BYTES", 1500)
    socket = _Socket(*[_text({"n": i}) for i in range(50)])
    reader = ClientFrameReader(socket, decode="json", label="t")
    try:
        await asyncio.wait({reader.task}, timeout=1)
        assert reader.task.done()  # the reader ended, it did not block
        assert reader.closed_for_flood
        assert socket.closed_with == (1013, "client frame budget exceeded")
        for _ in range(2):
            with pytest.raises(WebSocketDisconnect) as exc:
                await reader.next()
            assert exc.value.code == 1013
    finally:
        await reader.aclose()


async def test_a_single_frame_larger_than_the_budget_is_taken_when_nothing_waits(monkeypatch):
    monkeypatch.setattr(ws_shared, "CLIENT_FRAME_BUDGET_BYTES", 100)
    big = {"blob": "x" * 5000}
    reader = ClientFrameReader(_Socket(_text(big)), decode="json", label="t")
    try:
        assert await reader.next() == big
        assert not reader.closed_for_flood
    finally:
        await reader.aclose()


async def test_stop_is_synchronous_and_aclose_waits():
    reader = ClientFrameReader(_Socket(), decode="raw", label="t")
    await asyncio.sleep(0)
    reader.stop()
    await reader.aclose()
    assert reader.task.cancelled()


def test_rate_limit_judges_frames_by_their_arrival():
    """60 heartbeats/BLE reports that arrived over a minute, taken at once after
    a long turn, are no burst. Judged at hand-over time they would be."""
    start = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=60)
    by_arrival = WSRateLimiter(per_second=50, per_minute=1000, enabled=True)
    assert all(
        by_arrival.check("sat", at=start + timedelta(seconds=i))[0] for i in range(60)
    )
    at_hand_over = WSRateLimiter(per_second=50, per_minute=1000, enabled=True)
    results = [at_hand_over.check("sat")[0] for _ in range(60)]
    assert results.count(False) == 10  # the control: the old judgement refuses
