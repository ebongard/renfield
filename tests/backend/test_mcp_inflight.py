"""Refresh and self-heal must not tear down a session while a call is running on it.

Red-team finding, 2026-09-14: the scanner server was busy with OCR, answered
`list_tools` late, and the background refresh (10s) and the self-heal probe (2s)
read that as a dead session. The reconnect closed the stream the running
`scan_document` was waiting on, and the automatic retry scanned an empty feeder.
A session that is answering a call is alive by definition.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.mcp_client import MCPManager, MCPServerConfig, MCPServerState, MCPToolInfo

pytestmark = [pytest.mark.unit]


def _manager(session, inflight=0):
    manager = MCPManager()
    state = MCPServerState(config=MCPServerConfig(name="scanner"), connected=True, session=session)
    state.inflight_calls = inflight
    manager._servers["scanner"] = state
    return manager, state


async def test_refresh_leaves_a_busy_session_alone():
    session = AsyncMock()
    session.list_tools = AsyncMock(side_effect=TimeoutError())
    manager, state = _manager(session, inflight=1)

    await manager.refresh_tools()

    session.list_tools.assert_not_called()
    assert state.connected is True


async def test_refresh_still_checks_an_idle_session():
    """The skip must be narrow: an idle session that fails list_tools is still
    marked disconnected, exactly as before."""
    session = AsyncMock()
    session.list_tools = AsyncMock(side_effect=TimeoutError())
    manager, state = _manager(session, inflight=0)

    await manager.refresh_tools()

    assert state.connected is False


async def test_probe_does_not_reconnect_under_a_running_call():
    session = AsyncMock()
    session.list_tools = AsyncMock(side_effect=TimeoutError())
    manager, state = _manager(session, inflight=1)
    manager._reconnect_server = AsyncMock()

    result = await manager.probe_server("scanner")

    assert result["ok"] is True and "in flight" in result["detail"]
    manager._reconnect_server.assert_not_called()
    session.list_tools.assert_not_called()


def _tool_manager(call_tool):
    manager = MCPManager()
    manager._tool_index["mcp.scanner.scan_document"] = MCPToolInfo(
        "scanner", "scan_document", "mcp.scanner.scan_document", "Scan")
    session = AsyncMock()
    session.call_tool = call_tool
    state = MCPServerState(config=MCPServerConfig(name="scanner"), connected=True, session=session)
    manager._servers["scanner"] = state
    return manager, state


async def test_a_call_is_counted_while_it_runs_and_released_after():
    seen = []

    async def call_tool(*args, **kwargs):
        seen.append(state.inflight_calls)
        return MagicMock(isError=False, content=[])

    manager, state = _tool_manager(call_tool)
    await manager.execute_tool("mcp.scanner.scan_document", {})

    assert seen == [1]
    assert state.inflight_calls == 0


async def test_the_count_is_released_when_the_call_times_out():
    async def call_tool(*args, **kwargs):
        await asyncio.sleep(1)

    manager, state = _tool_manager(call_tool)
    result = await manager.execute_tool("mcp.scanner.scan_document", {}, call_timeout=0.01)

    assert "Timeout" in result["message"]
    assert state.inflight_calls == 0
