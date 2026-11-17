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

Starlette's ``TestClient`` cannot show this: it does not run uvicorn's
protocol, has no transport to pause and sends no keepalive pings. These tests
therefore run the real chat router under a REAL in-process uvicorn server with
a small ping budget and talk to it with the ``websockets`` client.

The turn is stubbed at ``_resolve_wire_speaker``, the first coroutine the
handler awaits inline for a ``text`` frame: the stub sleeps for the frame's
``speaker_audio_duration_s``. ``detect_injection`` then blocks every text, so
the turn ends with the refusal ``stream`` + ``done`` frames and touches no
database and no model.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from types import SimpleNamespace

import pytest
import uvicorn
from fastapi import FastAPI, WebSocket
from loguru import logger
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

import api.websocket.chat_handler as ch
import api.websocket.shared as ws_shared
from services.input_guard import InjectionResult

pytestmark = [pytest.mark.backend, pytest.mark.integration]

# Server keepalive budget: a ping every PING seconds, closed after PING more
# without a pong. A turn of SLOW seconds outlives the budget comfortably.
PING = 0.3
SLOW = 5 * PING
REFUSAL = {"type": "stream", "content": "Ich kann diese Anfrage nicht verarbeiten."}
DONE = {"type": "done"}


class _InProcessServer(uvicorn.Server):
    """uvicorn without signal handlers, so it can run inside the test loop."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


@contextlib.asynccontextmanager
async def _serve(app: FastAPI):
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        ws="websockets-sansio",
        ws_ping_interval=PING,
        ws_ping_timeout=PING,
        lifespan="off",
        log_config=None,
        log_level="warning",
    )
    server = _InProcessServer(config)
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)


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
        def check(self, _ip):
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


async def _recv(ws, timeout: float = SLOW + 5) -> dict:
    try:
        return json.loads(await asyncio.wait_for(ws.recv(), timeout))
    except ConnectionClosed as exc:
        rcvd = exc.rcvd
        pytest.fail(
            f"server closed the socket: code={rcvd.code if rcvd else None} "
            f"reason={rcvd.reason if rcvd else None!r}"
        )


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

    async with _serve(app) as base, connect(f"{base}/inline", ping_interval=None) as ws:
        await ws.send(json.dumps({"n": 1}))
        await ws.send(json.dumps({"n": 2}))  # arrives while the handler is busy
        with pytest.raises(ConnectionClosed) as closed:
            await asyncio.wait_for(ws.recv(), SLOW + 5)
    assert closed.value.rcvd is not None
    assert closed.value.rcvd.code == 1011
    assert closed.value.rcvd.reason == "keepalive ping timeout"


async def test_register_frame_during_a_long_turn_keeps_the_socket_and_delivers_the_turn(chat_app, events):
    async with _serve(chat_app) as base, connect(f"{base}/ws", ping_interval=None) as ws:
        await ws.send(_text("slow", SLOW))
        await ws.send(_register_frame("session-a"))  # the production race

        assert await _recv(ws) == REFUSAL
        assert await _recv(ws) == DONE

        # The socket is still open and still serves turns.
        await ws.send(_text("next"))
        assert await _recv(ws) == REFUSAL
        assert await _recv(ws) == DONE

    # The register frame was read during the turn but processed AFTER it, in
    # arrival order -- exactly as before.
    assert events == [
        ("start", SLOW), ("end", SLOW), ("check", "slow"),
        ("register", "session-a"),
        ("start", 0.0), ("end", 0.0), ("check", "next"),
    ]


async def test_two_text_frames_back_to_back_run_as_two_turns_in_order(chat_app, events):
    async with _serve(chat_app) as base, connect(f"{base}/ws", ping_interval=None) as ws:
        await ws.send(_text("first", SLOW))
        await ws.send(_text("second", 0.1))
        received = [await _recv(ws) for _ in range(4)]

    assert received == [REFUSAL, DONE, REFUSAL, DONE]
    # Never interleaved: the second turn starts only after the first one ended.
    assert events == [
        ("start", SLOW), ("end", SLOW), ("check", "first"),
        ("start", 0.1), ("end", 0.1), ("check", "second"),
    ]


async def test_client_disconnect_mid_turn_lets_the_turn_finish_and_leaks_no_task(chat_app, events, readers):
    async with _serve(chat_app) as base:
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


async def test_a_flood_during_a_turn_is_bounded_and_then_processed_in_order(chat_app, events, readers, monkeypatch):
    bound = 4
    flood = 20
    monkeypatch.setattr(ws_shared, "MAX_PENDING_CLIENT_FRAMES", bound)
    warnings: list[str] = []
    sink = logger.add(lambda m: warnings.append(m.record["message"]), level="WARNING")
    try:
        async with _serve(chat_app) as base, connect(f"{base}/ws", ping_interval=None) as ws:
            # Short enough that the paused read stays inside the ping budget.
            await ws.send(_text("busy", PING / 2))
            for i in range(flood):
                await ws.send(_register_frame(f"s-{i}"))
            await ws.send(_text("after"))
            received = [await _recv(ws) for _ in range(4)]
    finally:
        logger.remove(sink)

    assert received == [REFUSAL, DONE, REFUSAL, DONE]
    assert readers[0].max_pending == bound
    # The queue never held more than the bound (asyncio.Queue enforces maxsize)
    # and reaching it is visible in the log.
    assert any("frame queue is full" in w for w in warnings), warnings
    # Nothing was dropped and nothing reordered.
    assert [e for e in events if e[0] == "register"] == [("register", f"s-{i}") for i in range(flood)]
    assert events[-3:] == [("start", 0.0), ("end", 0.0), ("check", "after")]
