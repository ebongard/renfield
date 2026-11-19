"""Reconnect + probe tests for MCPManager.

Covers the regressions raised in the 2026-04-28 health-check redesign review:
- C1: lock-creation race in _reconnect_server (50 concurrent callers must
       trigger exactly one _connect_server invocation).
- C3: execute_tool retries once on session-shape exceptions but NOT on
       application errors like McpError.
- I4: probe_server marks state.connected=False when the post-reconnect
       probe still fails.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import anyio
import httpx
import pytest

from services.mcp_client import (
    MCPManager,
    MCPServerConfig,
    MCPServerState,
    MCPToolInfo,
    MCPTransportType,
)


def _make_state(name: str = "demo") -> MCPServerState:
    cfg = MCPServerConfig(
        name=name,
        url="http://localhost:9999/mcp",
        transport=MCPTransportType.STREAMABLE_HTTP,
    )
    return MCPServerState(config=cfg, connected=True)


def _make_manager(state: MCPServerState) -> MCPManager:
    mgr = MCPManager()
    mgr._servers[state.config.name] = state
    return mgr


# ---------------------------------------------------------------------------
# C1 — _reconnect_server must serialize concurrent callers
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reconnect_server_serialises_concurrent_callers():
    """50 concurrent callers must result in exactly one _connect_server call.

    Pre-fix, lazy lock creation under contention let multiple callers each
    construct their own asyncio.Lock and reconnect in parallel — the bug the
    redesign exists to fix would silently linger.
    """
    state = _make_state()
    state.session = MagicMock()  # pretend it's connected
    mgr = _make_manager(state)

    invocations = 0
    connect_started = asyncio.Event()
    can_finish = asyncio.Event()

    async def _fake_connect(s: MCPServerState) -> None:
        nonlocal invocations
        invocations += 1
        connect_started.set()
        await can_finish.wait()
        s.connected = True
        s.session = MagicMock()

    mgr._connect_server = _fake_connect  # type: ignore[assignment]

    # Force "needs reconnect" state (drop session)
    state.connected = False
    state.session = None
    state.owner = None

    # Spawn 50 concurrent callers
    tasks = [asyncio.create_task(mgr._reconnect_server(state)) for _ in range(50)]
    await connect_started.wait()
    can_finish.set()
    results = await asyncio.gather(*tasks)

    assert all(results), "every caller should observe a successful reconnect"
    assert invocations == 1, (
        f"expected 1 _connect_server call, got {invocations} — concurrent reconnect"
    )


# ---------------------------------------------------------------------------
# C3 — execute_tool retries on session-shape errors only
# ---------------------------------------------------------------------------


@pytest.fixture
def manager_with_tool():
    state = _make_state(name="srv")
    tool = MCPToolInfo(
        server_name="srv",
        original_name="ping",
        namespaced_name="mcp.srv.ping",
        description="ping",
        input_schema={"type": "object", "properties": {}},
    )
    state.tools = [tool]
    state.session = AsyncMock()
    state.session.call_tool = AsyncMock()
    mgr = _make_manager(state)
    mgr._tool_index["mcp.srv.ping"] = tool
    return mgr, state


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_tool_retries_on_session_dead(manager_with_tool):
    """ClosedResourceError → reconnect, retry, success."""
    mgr, state = manager_with_tool
    # First call dies, second call succeeds.
    success_result = MagicMock(content=[MagicMock(type="text", text="ok")], isError=False)
    state.session.call_tool.side_effect = [
        anyio.ClosedResourceError(),
        success_result,
    ]
    reconnect_calls = 0

    async def _fake_reconnect(s):
        nonlocal reconnect_calls
        reconnect_calls += 1
        s.connected = True
        return True

    mgr._reconnect_server = _fake_reconnect  # type: ignore[assignment]

    out = await mgr.execute_tool("mcp.srv.ping", {}, user_permissions=None)
    assert out["success"] is True, out
    assert reconnect_calls == 1
    assert state.session.call_tool.await_count == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_tool_does_not_retry_on_application_error(manager_with_tool):
    """ValueError (stand-in for McpError / app-level exception) must NOT trigger reconnect.

    Pre-fix, any non-timeout Exception triggered reconnect — tearing down
    healthy sessions over malformed arguments.
    """
    mgr, state = manager_with_tool
    state.session.call_tool.side_effect = [ValueError("invalid argument")]
    reconnect_calls = 0

    async def _fake_reconnect(s):
        nonlocal reconnect_calls
        reconnect_calls += 1
        return True

    mgr._reconnect_server = _fake_reconnect  # type: ignore[assignment]

    out = await mgr.execute_tool("mcp.srv.ping", {}, user_permissions=None)
    assert out["success"] is False
    assert reconnect_calls == 0
    assert state.session.call_tool.await_count == 1


# ---------------------------------------------------------------------------
# On-demand reconnect: a tool call against a server that went DOWN since the
# last call (its pod/subprocess restarted → state.connected=False at call
# time) must attempt one reconnect instead of bailing with "nicht verbunden".
# This is the fix for the dlna-mcp regression where a `kubectl set image` on
# an MCP-server deploy broke the agent's access until a manual backend restart.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ensure_connected_short_circuits_when_connected():
    """No needless reconnect when the session is already live."""
    state = _make_state()
    state.session = MagicMock()
    mgr = _make_manager(state)
    mgr._reconnect_server = AsyncMock(return_value=True)  # type: ignore[assignment]

    assert await mgr._ensure_connected(state) is True
    mgr._reconnect_server.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ensure_connected_reconnects_when_down():
    """Down session → exactly one reconnect attempt; returns its result."""
    state = _make_state()
    state.connected = False
    state.session = None
    mgr = _make_manager(state)
    mgr._reconnect_server = AsyncMock(return_value=True)  # type: ignore[assignment]

    assert await mgr._ensure_connected(state) is True
    mgr._reconnect_server.assert_awaited_once()
    assert await mgr._ensure_connected(None) is False  # unknown server


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_tool_self_heals_when_down_at_call_time(manager_with_tool):
    """Server marked disconnected at call time → reconnect → call proceeds.

    Pre-fix, execute_tool short-circuited with "nicht verbunden" whenever
    state.connected was False, so a restarted MCP server pod stayed unusable
    until the background tick or a manual backend restart. Now the call
    self-heals.
    """
    mgr, state = manager_with_tool
    # Simulate the server's pod having restarted: session is gone.
    state.connected = False
    state.session = None
    success_result = MagicMock(content=[MagicMock(type="text", text="ok")], isError=False)

    async def _fake_reconnect(s):
        s.connected = True
        s.session = AsyncMock()
        s.session.call_tool = AsyncMock(return_value=success_result)
        return True

    mgr._reconnect_server = _fake_reconnect  # type: ignore[assignment]

    out = await mgr.execute_tool("mcp.srv.ping", {}, user_permissions=None)
    assert out["success"] is True, out
    assert out["message"] == "ok"
    state.session.call_tool.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_tool_not_connected_when_reconnect_fails(manager_with_tool):
    """If the on-demand reconnect can't restore the session, fail gracefully."""
    mgr, state = manager_with_tool
    state.connected = False
    state.session = None
    mgr._reconnect_server = AsyncMock(return_value=False)  # type: ignore[assignment]

    out = await mgr.execute_tool("mcp.srv.ping", {}, user_permissions=None)
    assert out["success"] is False
    assert "nicht verbunden" in out["message"]
    mgr._reconnect_server.assert_awaited_once()


# ---------------------------------------------------------------------------
# "Session terminated": the streamable_http McpError raised after the server
# restarts. It is NOT a transport exception type, so it must be recognised by
# message and treated as session-death (reconnect + retry), while genuine
# application McpErrors still fall through without a needless reconnect.
# ---------------------------------------------------------------------------


def _session_terminated_error():
    """Build the real McpError the MCP SDK raises on a bounced session."""
    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData

    return McpError(ErrorData(code=32600, message="Session terminated"))


@pytest.mark.unit
def test_is_session_dead_classification():
    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData

    from services.mcp_client import _is_session_dead

    # Transport death — typed exceptions + the SDK's "Session terminated" McpError.
    assert _is_session_dead(_session_terminated_error()) is True
    assert _is_session_dead(anyio.ClosedResourceError()) is True

    # NOT session death — the classifier is gated on McpError + the exact
    # "session terminated" signal, so none of these reconnect-and-retry
    # (which could double-execute a mutating tool):
    #   - a plain exception whose text mentions a session (not an McpError)
    assert _is_session_dead(Exception("Session terminated")) is False
    #   - an application McpError that merely mentions a session (e.g. IMAP)
    assert _is_session_dead(McpError(ErrorData(code=-32000, message="session expired"))) is False
    #   - ordinary application errors
    assert _is_session_dead(McpError(ErrorData(code=-32602, message="Invalid params"))) is False
    assert _is_session_dead(ValueError("bad argument")) is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_tool_retries_on_session_terminated_mcperror(manager_with_tool):
    """The bounced-server McpError must trigger reconnect + retry, not fail.

    Reproduces the live dlna-mcp regression: the server pod restarts, the
    next call raises McpError 'Session terminated', and the agent must recover
    on the same turn (reconnect to the new pod) rather than reporting failure.
    """
    mgr, state = manager_with_tool
    success_result = MagicMock(content=[MagicMock(type="text", text="ok")], isError=False)
    state.session.call_tool.side_effect = [
        _session_terminated_error(),
        success_result,
    ]
    reconnect_calls = 0

    async def _fake_reconnect(s):
        nonlocal reconnect_calls
        reconnect_calls += 1
        s.connected = True
        return True

    mgr._reconnect_server = _fake_reconnect  # type: ignore[assignment]

    out = await mgr.execute_tool("mcp.srv.ping", {}, user_permissions=None)
    assert out["success"] is True, out
    assert reconnect_calls == 1
    assert state.session.call_tool.await_count == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_tool_retries_on_httpx_remote_protocol_error(manager_with_tool):
    """httpx.RemoteProtocolError is the typical streamable_http stream-died signal."""
    mgr, state = manager_with_tool
    success_result = MagicMock(content=[MagicMock(type="text", text="ok")], isError=False)
    state.session.call_tool.side_effect = [
        httpx.RemoteProtocolError("stream closed"),
        success_result,
    ]
    mgr._reconnect_server = AsyncMock(return_value=True)  # type: ignore[assignment]

    out = await mgr.execute_tool("mcp.srv.ping", {}, user_permissions=None)
    assert out["success"] is True
    mgr._reconnect_server.assert_awaited_once()


# ---------------------------------------------------------------------------
# I4 — probe_server marks connected=False when post-reconnect probe fails
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_probe_server_marks_disconnected_when_reconnect_doesnt_fix_it():
    state = _make_state(name="srv")
    state.session = AsyncMock()
    state.session.list_tools = AsyncMock(side_effect=anyio.ClosedResourceError())
    mgr = _make_manager(state)

    # Reconnect "succeeds" but the new session also fails list_tools.
    async def _fake_reconnect(s):
        s.connected = True  # _connect_server would set this
        # session still raises
        return True

    mgr._reconnect_server = _fake_reconnect  # type: ignore[assignment]

    result = await mgr.probe_server("srv")
    assert result["ok"] is False
    assert state.connected is False, (
        "probe_server must clear connected=True when the fresh session also fails"
    )
    assert state.last_error is not None
