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
    state.exit_stack = None

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
