"""Anonymous-client listener (registry mode second port) — PR #987 review.

Finding 1: a second in-process uvicorn.Server must NOT capture process
signals — uvicorn >= 0.30 registers SIGTERM/SIGINT unconditionally inside
serve() (capture_signals), and the last server started would steal the
primary's handlers, breaking graceful shutdown. _AnonServer overrides
capture_signals to a no-op.

Finding 2: a bind failure must fail LOUD (lifespan raises → pod non-ready),
not linger as an unretrieved task exception behind a green /health.

These tests exercise _AnonServer against a trivial ASGI app on real
sockets — no voice models involved.
"""
from __future__ import annotations

import asyncio
import signal
import socket

import pytest
import uvicorn

from voice_server.main import _AnonServer


async def _dummy_app(scope, receive, send):
    if scope["type"] == "http":
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_capture_signals_is_a_noop():
    """Entering _AnonServer.capture_signals must leave the process signal
    handlers untouched (the primary server owns them)."""
    server = _AnonServer(uvicorn.Config(_dummy_app, lifespan="off"))
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    with server.capture_signals():
        during = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    assert during == before


@pytest.mark.asyncio
async def test_serve_does_not_touch_signal_handlers_and_stops_on_should_exit():
    """Full serve() lifecycle on a real socket: handlers unchanged while
    serving, and should_exit still stops the server (graceful path)."""
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    port = _free_port()
    server = _AnonServer(
        uvicorn.Config(_dummy_app, host="127.0.0.1", port=port,
                       lifespan="off", log_level="error")
    )
    task = asyncio.get_running_loop().create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started, "anon server never started"

    during = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    assert during == before, "anon server captured process signal handlers"

    server.should_exit = True
    await asyncio.wait_for(task, timeout=10)


@pytest.mark.asyncio
async def test_bind_conflict_is_detectable():
    """When the port is taken, serve() must terminate the task (uvicorn calls
    sys.exit) rather than hang — the lifespan's started-wait turns that into
    a loud RuntimeError. Mirrors the lifespan wiring: SystemExit is contained
    in the wrapper task."""
    blocker = socket.socket()
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        server = _AnonServer(
            uvicorn.Config(_dummy_app, host="127.0.0.1", port=port,
                           lifespan="off", log_level="critical")
        )

        async def _serve_contained():
            try:
                await server.serve()
            except asyncio.CancelledError:
                raise
            except BaseException:  # noqa: BLE001 — includes SystemExit
                pass

        task = asyncio.get_running_loop().create_task(_serve_contained())
        # The lifespan loop: started never flips, task completes instead.
        for _ in range(200):
            if task.done():
                break
            await asyncio.sleep(0.05)
        assert task.done(), "bind-conflict serve() neither started nor exited"
        assert not server.started
    finally:
        blocker.close()
