"""#1579 — a server's transport + session are entered AND exited by ONE task.

anyio requires a cancel scope to be exited in the task that entered it. The
transports (`streamablehttp_client`, `stdio_client`, `sse_client`) open an anyio
task group, so their stack used to be entered in whichever task connected and
exited in whichever task reconnected or shut down -- "Attempted to exit cancel
scope in a different task than it was entered in", every time.

Worse, a REFUSED connection surfaced as a bare CancelledError: the transport's
task group cancels its own scope when its writer fails, and only that scope's
`__aexit__` turns the cancellation back into the real error. `_connect_server`
caught the CancelledError before any exit, took it for an outer cancellation,
logged nothing, and the cancellation stayed delivered to the CALLER -- the
refresh loop died on its first reconnect of a refused server (measured on the
build box: "reconnecting to 'dlna' (attempt 2)...", then silence for 15 days)
and a self-heal guard got a CancelledError instead of its TimeoutError.

Now one owner task per session enters and exits the stack; everyone else asks it
to stop.
"""

from __future__ import annotations

import ast
import asyncio
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from services.mcp_client import (
    ExponentialBackoff,
    MCPManager,
    MCPServerConfig,
    MCPServerState,
    MCPTransportType,
)

_MCP_CLIENT = Path(__file__).resolve().parents[2] / "src" / "backend" / "services" / "mcp_client.py"


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _refused_state() -> MCPServerState:
    return MCPServerState(
        config=MCPServerConfig(
            name="refused",
            url=f"http://127.0.0.1:{_closed_port()}/mcp",
            transport=MCPTransportType.STREAMABLE_HTTP,
        ),
        backoff=ExponentialBackoff(),
    )


class _FakeSession:
    def __init__(self, *_a, **_k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def initialize(self):
        return None

    async def list_tools(self):
        return SimpleNamespace(tools=[])


# --- the refused server: a failure with a reason, never a cancellation -------


@pytest.mark.unit
async def test_a_refused_connect_is_a_failure_with_a_reason_not_a_cancellation(monkeypatch):
    import services.mcp_client as mc

    monkeypatch.setattr(mc.settings, "mcp_connect_timeout", 3.0)
    manager = MCPManager()
    state = _refused_state()

    await manager._connect_server(state)  # used to raise CancelledError

    assert state.connected is False
    assert state.last_error and "cancelled" not in state.last_error, state.last_error
    assert state.backoff.attempt_count == 1
    assert asyncio.current_task().cancelling() == 0
    await asyncio.sleep(0.05)  # no cancellation is still pending on this task


@pytest.mark.unit
async def test_a_refused_server_does_not_kill_the_refresh_loop(monkeypatch):
    import services.mcp_client as mc

    monkeypatch.setattr(mc.settings, "mcp_connect_timeout", 3.0)
    monkeypatch.setattr(mc.settings, "mcp_refresh_interval", 0.05)
    manager = MCPManager()
    state = _refused_state()
    state.backoff = ExponentialBackoff(initial_delay=0.05, max_delay=0.1)
    manager._servers["refused"] = state

    await manager.start_refresh_loop()
    try:
        for _ in range(100):
            await asyncio.sleep(0.05)
            if state.backoff.attempt_count >= 2 or manager._refresh_task.done():
                break
        assert not manager._refresh_task.done(), "the refresh loop died on a refused server"
        assert state.backoff.attempt_count >= 2, "the loop stopped retrying"
    finally:
        await manager.shutdown()


@pytest.mark.unit
async def test_a_guard_cancelled_connect_cancels_only_the_guard(monkeypatch):
    """A self-heal guard that times out a hanging connect gets ITS TimeoutError,
    and the owner task it left behind ends by itself."""
    import services.mcp_client as mc

    monkeypatch.setattr(mc.settings, "mcp_connect_timeout", 30.0)

    async def _hang(_r, _w):
        await asyncio.sleep(3600)

    server = await asyncio.start_server(_hang, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        manager = MCPManager()
        state = MCPServerState(
            config=MCPServerConfig(name="silent", url=f"http://127.0.0.1:{port}/mcp"),
            backoff=ExponentialBackoff(),
        )
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.3):
                await manager._reconnect_server(state)
        assert asyncio.current_task().cancelling() == 0
        assert state.connected is False and state.session is None
        def _mine():
            return [t for t in mc._session_owner_tasks if t.get_name() == "mcp-session:silent"]

        for _ in range(100):
            if not _mine():
                break
            await asyncio.sleep(0.05)
        assert not _mine(), "the abandoned owner task never ended"
    finally:
        server.close()


# --- the invariant: one task enters and exits ---------------------------------


@pytest.mark.unit
async def test_the_transport_is_exited_in_the_task_that_entered_it(monkeypatch):
    import mcp as mcp_mod
    import mcp.client.streamable_http as sh

    record: list[dict] = []

    class _Transport:
        async def __aenter__(self):
            self.entry = {"entered": asyncio.current_task(), "exited": None}
            record.append(self.entry)
            return (MagicMock(), MagicMock())

        async def __aexit__(self, *_exc):
            self.entry["exited"] = asyncio.current_task()
            return False

    monkeypatch.setattr(sh, "streamablehttp_client", lambda **kw: _Transport())
    monkeypatch.setattr(mcp_mod, "ClientSession", _FakeSession)

    manager = MCPManager()
    state = MCPServerState(config=MCPServerConfig(name="srv", url="http://srv:1/mcp"))
    manager._servers["srv"] = state

    await asyncio.create_task(manager._connect_server(state))  # connected from task A
    assert state.connected is True
    manager._set_connected(state, False)
    await asyncio.create_task(manager._reconnect_server(state))  # torn down + reopened from B
    assert state.connected is True
    await manager.shutdown()  # torn down from the test task

    assert len(record) == 2
    for entry in record:
        assert entry["exited"] is not None, "a transport was never closed"
        assert entry["exited"] is entry["entered"], "exited in another task than it was entered in"


@pytest.mark.unit
async def test_a_session_that_dies_marks_the_server_disconnected(monkeypatch):
    """The transport's own task group fails after the connect (the server went
    away): the owner reports it and the server reads as disconnected, so the
    refresh loop reconnects it instead of handing out a dead session."""
    import anyio
    import mcp as mcp_mod
    import mcp.client.streamable_http as sh

    @asynccontextmanager
    async def _dying_transport(**_kw):
        async with anyio.create_task_group() as tg:
            async def _die():
                await anyio.sleep(0.1)
                raise ConnectionResetError("server went away")

            tg.start_soon(_die)
            yield (MagicMock(), MagicMock())

    monkeypatch.setattr(sh, "streamablehttp_client", _dying_transport)
    monkeypatch.setattr(mcp_mod, "ClientSession", _FakeSession)

    manager = MCPManager()
    state = MCPServerState(config=MCPServerConfig(name="srv", url="http://srv:1/mcp"))
    manager._servers["srv"] = state
    await manager._connect_server(state)
    assert state.connected is True

    for _ in range(100):
        if not state.connected:
            break
        await asyncio.sleep(0.02)
    assert state.connected is False
    assert state.session is None
    assert "server went away" in (state.last_error or ""), state.last_error


# --- structural guard: nobody but the owner touches the stack -----------------


def _manual_context_calls(source: str) -> list[int]:
    """Lines that call `__aenter__`/`__aexit__` by hand -- the only way to enter
    a stack in one task and exit it in another."""
    return sorted(
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"__aenter__", "__aexit__"}
    )


@pytest.mark.unit
def test_mcp_client_never_enters_or_exits_a_context_by_hand():
    found = _manual_context_calls(_MCP_CLIENT.read_text())
    assert found == [], (
        f"mcp_client.py calls __aenter__/__aexit__ by hand at lines {found}: a stack "
        "must be entered and exited with `async with` inside the session owner task (#1579)"
    )


@pytest.mark.unit
def test_the_manual_context_check_sees_a_hand_exit():
    src = "async def f(stack):\n    await stack.__aenter__()\n    await stack.__aexit__(None, None, None)\n"
    assert _manual_context_calls(src) == [2, 3]
