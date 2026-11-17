"""A client frame during a long chat turn must not let the server kill its own socket (#1570).

uvicorn's ``websockets-sansio`` protocol (the default from uvicorn 0.5x on, with
websockets >= 14) calls ``transport.pause_reading()`` as soon as a client
message is queued while the app is not inside ``receive()``. While reading is
paused the pong to the server's OWN keepalive ping is never read, so after
``ws_ping_interval + ws_ping_timeout`` the server fails the connection with
1011 "keepalive ping timeout". The chat handler processed a turn inline and
only called ``receive()`` again after it, so any frame sent during a turn that
outlived that budget (in production: the ``register`` frame racing the first
``text``) cut the turn off.

The tests run the real chat router under a REAL in-process uvicorn
(``uvicorn_harness``; the TestClient cannot show the bug) and talk to it with
the ``websockets`` client.

The turn is stubbed at ``_resolve_wire_speaker``, the first coroutine the
handler awaits inline for a ``text`` frame: the stub sleeps for the frame's
``speaker_audio_duration_s``. ``detect_injection`` then blocks every text, so
the turn ends with the refusal ``stream`` + ``done`` frames and touches no
database and no model.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, WebSocket
from loguru import logger
from websockets.asyncio.client import connect

import api.websocket.chat_handler as ch
import api.websocket.shared as ws_shared
from services.input_guard import InjectionResult
from tests.backend.uvicorn_harness import SLOW, expect_close, recv_json, serve

pytestmark = [pytest.mark.backend, pytest.mark.integration]

REFUSAL = {"type": "stream", "content": "Ich kann diese Anfrage nicht verarbeiten."}
DONE = {"type": "done"}


@pytest.fixture
def events() -> list[tuple]:
    return []


@pytest.fixture
def readers(monkeypatch) -> list:
    """Every frame reader the handler creates, so a test can inspect its task."""
    created: list = []

    class _Recording(ws_shared.ClientFrameReader):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(ch, "ClientFrameReader", _Recording, raising=False)
    return created


@pytest.fixture
def chat_app(monkeypatch, events) -> FastAPI:
    turn_started = asyncio.Event()

    async def _fake_auth(_ws, _token=None):
        return {"authenticated": True, "user_id": 42, "auth_method": "jwt"}

    class _AllowAll:
        def check(self, _ip, **_kwargs):
            return True, ""

    async def _turn(_embedding, duration):
        # The handler awaits this inline, exactly like a model call.
        events.append(("start", duration))
        turn_started.set()
        await asyncio.sleep(duration or 0)
        events.append(("end", duration))
        return None

    def _block(text):
        events.append(("check", text))
        return InjectionResult(score=0.99, blocked=True, matched_patterns=["test"])

    async def _no_replacement(_ws, _sid, _uid):
        return None

    def _register(sid, _ws):
        events.append(("register", sid))

    monkeypatch.setattr(ch, "authenticate_websocket", _fake_auth)
    monkeypatch.setattr(ch, "get_rate_limiter", lambda: _AllowAll())
    monkeypatch.setattr(ch, "_resolve_wire_speaker", _turn)
    monkeypatch.setattr(ch, "detect_injection", _block)
    monkeypatch.setattr(ch, "_replacement_session_for", _no_replacement)
    monkeypatch.setattr(ch, "register_ws_connection", _register)

    app = FastAPI()
    app.state.ollama = SimpleNamespace(default_lang="de")
    app.state.turn_started = turn_started
    app.include_router(ch.router)
    return app


def _text(content: str, seconds: float = 0.0) -> str:
    return json.dumps({"type": "text", "content": content, "speaker_audio_duration_s": seconds})


def _register_frame(sid: str) -> str:
    return json.dumps({"type": "register", "session_id": sid})


async def test_harness_reproduces_the_keepalive_kill_on_an_endpoint_that_does_not_read():
    """Control for the harness: a handler that does not read during long work
    really is closed with 1011 by uvicorn. Without this the tests below could
    pass on a harness that never pauses reading."""
    app = FastAPI()

    @app.websocket("/inline")
    async def _inline(websocket: WebSocket):
        await websocket.accept()
        await websocket.receive_json()
        await asyncio.sleep(SLOW)
        await websocket.send_json(DONE)
        await websocket.receive_json()

    async with serve(app) as base, connect(f"{base}/inline", ping_interval=None) as ws:
        await ws.send(json.dumps({"n": 1}))
        await ws.send(json.dumps({"n": 2}))  # arrives while the handler is busy
        rcvd = await expect_close(ws)
    assert rcvd is not None
    assert rcvd.code == 1011
    assert rcvd.reason == "keepalive ping timeout"


async def test_register_frame_during_a_long_turn_keeps_the_socket_and_delivers_the_turn(chat_app, events):
    async with serve(chat_app) as base, connect(f"{base}/ws", ping_interval=None) as ws:
        await ws.send(_text("slow", SLOW))
        await ws.send(_register_frame("session-a"))  # the production race

        assert await recv_json(ws) == REFUSAL
        assert await recv_json(ws) == DONE

        # The socket is still open and still serves turns.
        await ws.send(_text("next"))
        assert await recv_json(ws) == REFUSAL
        assert await recv_json(ws) == DONE

    # The register frame was read during the turn but processed AFTER it, in
    # arrival order -- exactly as before.
    assert events == [
        ("start", SLOW), ("end", SLOW), ("check", "slow"),
        ("register", "session-a"),
        ("start", 0.0), ("end", 0.0), ("check", "next"),
    ]


async def test_two_text_frames_back_to_back_run_as_two_turns_in_order(chat_app, events):
    async with serve(chat_app) as base, connect(f"{base}/ws", ping_interval=None) as ws:
        await ws.send(_text("first", SLOW))
        await ws.send(_text("second", 0.1))
        received = [await recv_json(ws) for _ in range(4)]

    assert received == [REFUSAL, DONE, REFUSAL, DONE]
    # Never interleaved: the second turn starts only after the first one ended.
    assert events == [
        ("start", SLOW), ("end", SLOW), ("check", "first"),
        ("start", 0.1), ("end", 0.1), ("check", "second"),
    ]


async def test_client_disconnect_mid_turn_lets_the_turn_finish_and_leaks_no_task(chat_app, events, readers):
    async with serve(chat_app) as base:
        before = asyncio.all_tasks()  # the server's own tasks are not the connection's
        ws = await connect(f"{base}/ws", ping_interval=None)
        await ws.send(_text("gone", SLOW))
        await asyncio.wait_for(chat_app.state.turn_started.wait(), 5)
        handler_tasks = asyncio.all_tasks() - before - {asyncio.current_task()}
        await ws.close()

        # Everything the connection started ends on its own.
        _done, pending = await asyncio.wait(handler_tasks, timeout=SLOW + 5)
        assert not pending, f"tasks still running after the disconnect: {pending}"

    # As before the fix the socket's death does not cancel the turn: it runs
    # to its end, its send then fails and the handler leaves.
    assert ("end", SLOW) in events
    assert len(readers) == 1
    assert readers[0].task in handler_tasks
    assert readers[0].task.done()


async def test_many_frames_during_a_turn_are_all_kept_and_processed_in_order(chat_app, events):
    """More frames than the old 64-frame queue: the reader never stops reading
    because of a count, so the socket survives and nothing is dropped."""
    many = 150
    async with serve(chat_app) as base, connect(f"{base}/ws", ping_interval=None) as ws:
        await ws.send(_text("busy", SLOW))
        for i in range(many):
            await ws.send(_register_frame(f"s-{i}"))
        await ws.send(_text("after"))
        received = [await recv_json(ws) for _ in range(4)]

    assert received == [REFUSAL, DONE, REFUSAL, DONE]
    assert [e for e in events if e[0] == "register"] == [("register", f"s-{i}") for i in range(many)]
    assert events[-3:] == [("start", 0.0), ("end", 0.0), ("check", "after")]


async def test_a_flood_over_the_byte_budget_closes_with_1013_and_ends_cleanly(chat_app, events, readers, monkeypatch):
    monkeypatch.setattr(ws_shared, "CLIENT_FRAME_BUDGET_BYTES", 4096)
    lines: list[tuple[str, str]] = []
    sink = logger.add(lambda m: lines.append((m.record["level"].name, m.record["message"])), level="INFO")
    try:
        async with serve(chat_app) as base, connect(f"{base}/ws", ping_interval=None) as ws:
            await ws.send(_text("busy", SLOW))
            await asyncio.wait_for(chat_app.state.turn_started.wait(), 5)
            for i in range(200):
                await ws.send(_register_frame(f"s-{i}"))
            rcvd = await expect_close(ws)
            # The handler ends on its own: the turn's next send hits the closed socket.
            await asyncio.wait({readers[0].task}, timeout=SLOW + 5)
            async with asyncio.timeout(SLOW + 5):
                while ("end", SLOW) not in events or not any("frame flood" in m for _, m in lines):
                    await asyncio.sleep(0.05)
    finally:
        logger.remove(sink)

    assert rcvd is not None and (rcvd.code, rcvd.reason) == (1013, "client frame budget exceeded")
    assert readers[0].closed_for_flood
    warnings = [m for lvl, m in lines if lvl == "WARNING" and "budget" in m]
    assert len(warnings) == 1, warnings  # once, with the counts
    assert "4096-byte budget" in warnings[0]
    # No frame of the flood was acted on, and the closed socket is no ERROR.
    assert not [e for e in events if e[0] == "register"]
    assert not [m for lvl, m in lines if lvl == "ERROR"], lines
