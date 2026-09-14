"""Refresh and self-heal must not tear down a session while a call is running on it.

Red-team finding, 2026-09-14: the scanner server was busy with OCR, answered
`list_tools` late, and the background refresh (10s) and the self-heal probe (2s)
read that as a dead session. The reconnect closed the stream the running
`scan_document` was waiting on, and the automatic retry scanned an empty feeder.

The shield is bounded (review follow-up): only a call still INSIDE its own timeout
protects the session, and a skipped probe reports "skipped", never healthy — else
a dead server under steady traffic of hung calls would look busy forever.
"""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.mcp_client import MCPManager, MCPServerConfig, MCPServerState, MCPToolInfo

pytestmark = [pytest.mark.unit]


def _manager(session, deadlines=()):
    manager = MCPManager()
    state = MCPServerState(config=MCPServerConfig(name="scanner"), connected=True, session=session)
    state.inflight_deadlines = list(deadlines)
    manager._servers["scanner"] = state
    return manager, state


def _running():
    return [time.monotonic() + 60]


def _overdue():
    return [time.monotonic() - 1]


async def test_refresh_leaves_a_busy_session_alone():
    session = AsyncMock()
    session.list_tools = AsyncMock(side_effect=TimeoutError())
    manager, state = _manager(session, _running())

    await manager.refresh_tools()

    session.list_tools.assert_not_called()
    assert state.connected is True


async def test_refresh_still_checks_an_idle_session():
    """The skip must be narrow: an idle session that fails list_tools is still
    marked disconnected, exactly as before."""
    session = AsyncMock()
    session.list_tools = AsyncMock(side_effect=TimeoutError())
    manager, state = _manager(session)

    await manager.refresh_tools()

    assert state.connected is False


async def test_a_call_past_its_timeout_shields_nothing():
    """A hung call must not keep a dead server looking busy."""
    session = AsyncMock()
    session.list_tools = AsyncMock(side_effect=TimeoutError())
    manager, state = _manager(session, _overdue())

    await manager.refresh_tools()

    session.list_tools.assert_awaited()
    assert state.connected is False


async def test_probe_skips_under_a_running_call_and_says_so():
    session = AsyncMock()
    session.list_tools = AsyncMock(side_effect=TimeoutError())
    manager, state = _manager(session, _running())
    manager._reconnect_server = AsyncMock()

    result = await manager.probe_server("scanner")

    assert result["ok"] is None and "skipped" in result["detail"]
    manager._reconnect_server.assert_not_called()
    session.list_tools.assert_not_called()


async def test_probe_checks_normally_once_the_call_is_overdue():
    session = AsyncMock()
    session.list_tools = AsyncMock(side_effect=TimeoutError())
    manager, state = _manager(session, _overdue())
    manager._reconnect_server = AsyncMock(return_value=False)

    result = await manager.probe_server("scanner")

    assert result["ok"] is False
    manager._reconnect_server.assert_awaited_once()


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
        seen.append((state.inflight_calls, state.shielded_by_inflight_call()))
        return MagicMock(isError=False, content=[])

    manager, state = _tool_manager(call_tool)
    await manager.execute_tool("mcp.scanner.scan_document", {})

    assert seen == [(1, True)]
    assert state.inflight_calls == 0


async def test_the_count_is_released_when_the_call_times_out():
    async def call_tool(*args, **kwargs):
        await asyncio.sleep(1)

    manager, state = _tool_manager(call_tool)
    result = await manager.execute_tool("mcp.scanner.scan_document", {}, call_timeout=0.01)

    assert "Timeout" in result["message"]
    assert state.inflight_calls == 0


async def test_overlapping_calls_each_release_only_their_own_entry():
    gate = asyncio.Event()

    async def call_tool(name, arguments):
        if arguments.get("wait"):
            await gate.wait()
        return MagicMock(isError=False, content=[])

    manager, state = _tool_manager(call_tool)
    slow = asyncio.create_task(manager.execute_tool("mcp.scanner.scan_document", {"wait": True}))
    await asyncio.sleep(0)
    await manager.execute_tool("mcp.scanner.scan_document", {})

    assert state.inflight_calls == 1  # the slow one is still running
    gate.set()
    await slow
    assert state.inflight_calls == 0
