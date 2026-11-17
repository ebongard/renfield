"""A real in-process uvicorn for WebSocket tests that need uvicorn's protocol (#1570).

Starlette's ``TestClient`` does not run uvicorn's WebSocket protocol: it has no
transport to pause and sends no keepalive pings, so it cannot show a server
closing its own socket with 1011. This harness serves an app with the
``websockets-sansio`` protocol (uvicorn's default from 0.5x with websockets
>= 14) and a small ping budget, on a random local port.
"""
from __future__ import annotations

import asyncio
import contextlib
import json

import pytest
import uvicorn
from fastapi import FastAPI
from websockets.exceptions import ConnectionClosed

#: Server keepalive: a ping every PING seconds, closed after PING more without
#: a pong. A turn of SLOW seconds outlives that budget comfortably.
PING = 0.3
SLOW = 5 * PING


class _InProcessServer(uvicorn.Server):
    """uvicorn without signal handlers, so it can run inside the test loop."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


@contextlib.asynccontextmanager
async def serve(app: FastAPI):
    """Serve ``app``; yields the ``ws://host:port`` base URL."""
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


async def recv_json(ws, timeout: float = SLOW + 5) -> dict:
    """The next frame as JSON; a close fails the test with its code and reason."""
    try:
        return json.loads(await asyncio.wait_for(ws.recv(), timeout))
    except ConnectionClosed as exc:
        rcvd = exc.rcvd
        pytest.fail(
            f"server closed the socket: code={rcvd.code if rcvd else None} "
            f"reason={rcvd.reason if rcvd else None!r}"
        )


async def expect_close(ws, timeout: float = SLOW + 5):
    """Read until the server closes; returns the close frame it sent."""
    with pytest.raises(ConnectionClosed) as closed:
        async with asyncio.timeout(timeout):
            while True:
                await ws.recv()
    return closed.value.rcvd
