"""MCP self-detection Phase 3 follow-ups.

1. ``execute_tool_streaming`` recorded neither timeouts nor throttles. A streaming
   call fed nothing into the timeout window (``recent_outcomes``) or the rate-limit
   signal, did not shield its session from the refresh/self-heal reconnect, ignored
   the Retry-After gate, marked the server DISCONNECTED on any app error (a 429
   included), and ran a ``per_user_auth`` server on the shared operator session.
   The streaming path now uses the same accounting as ``execute_tool``.
2. ``refresh_tools`` rebuilt the tool list without the ``tool_hints`` from
   ``mcp_servers.yaml``, so every hint vanished after the first refresh (default
   300 s). Connect and refresh now install tools through one function; that also
   drops a tool the server no longer offers from the index on a reconnect.

Pinned against a REAL ``MCPManager`` loaded from YAML — faked dicts are how the
Phase-3 alert defects stayed invisible.
"""
import asyncio
import textwrap
import time
from types import SimpleNamespace

import pytest

from services.mcp_client import (
    MCPManager,
    MCPServerConfig,
    MCPServerState,
    MCPToolInfo,
    MCPTransportType,
    _server_call_timeout,
)
from services.mcp_streaming import PROGRESS_LABEL_RETRIEVING, ProgressChunk
from utils.config import settings

pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def _flags_off(monkeypatch):
    monkeypatch.setattr(settings, "mcp_health_rate_limit_signal_enabled", False)
    monkeypatch.setattr(settings, "mcp_rate_limit_backoff_enabled", False)


def _result(text: str, is_error: bool = False):
    return SimpleNamespace(isError=is_error, content=[SimpleNamespace(type="text", text=text)])


def _streaming(call_tool, **config_kw):
    """A real manager with one `streaming: true` server and one tool."""
    manager = MCPManager()
    config = MCPServerConfig(
        name="peer1",
        url="http://peer1.local/mcp",
        transport=MCPTransportType.STREAMABLE_HTTP,
        streaming=True,
        **config_kw,
    )
    state = MCPServerState(config=config, connected=True, session=SimpleNamespace(call_tool=call_tool))
    manager._servers["peer1"] = state
    manager._tool_index["mcp.peer1.query_brain"] = MCPToolInfo(
        "peer1", "query_brain", "mcp.peer1.query_brain", "federated peer query",
        {"type": "object", "properties": {}},
    )
    return manager, state


async def _drain(manager, **kw):
    return [i async for i in manager.execute_tool_streaming("mcp.peer1.query_brain", {}, **kw)]


def _finals(items):
    return [i for i in items if not isinstance(i, ProgressChunk)]


# ===========================================================================
# 1. Streaming accounting
# ===========================================================================

@pytest.mark.asyncio
class TestStreamingAccounting:
    async def test_timeout_after_partial_progress_is_a_failure_sample(self):
        async def call_tool(name, arguments, progress_callback=None):
            await progress_callback(0.5, 1.0, PROGRESS_LABEL_RETRIEVING)
            await asyncio.sleep(5)

        manager, state = _streaming(call_tool, call_timeout=0.05)
        items = await _drain(manager)

        assert any(isinstance(i, ProgressChunk) for i in items)  # partial result first
        assert "Timeout" in _finals(items)[0]["message"]
        assert list(state.recent_outcomes) == [False]
        assert state.inflight_calls == 0

    async def test_clean_completion_is_an_ok_sample_and_lifts_the_horizon(self):
        async def call_tool(name, arguments, progress_callback=None):
            return _result("fine")

        manager, state = _streaming(call_tool)
        state.rate_limited_until["query_brain"] = time.monotonic() + 100
        items = await _drain(manager)

        assert _finals(items)[0]["success"] is True
        assert list(state.recent_outcomes) == [True]
        assert state.last_successful_call > 0
        assert "query_brain" not in state.rate_limited_until

    async def test_app_error_result_is_not_a_sample(self):
        async def call_tool(name, arguments, progress_callback=None):
            return _result('{"error": "peer offline"}')

        manager, state = _streaming(call_tool)
        items = await _drain(manager)
        assert _finals(items)[0]["success"] is False
        assert list(state.recent_outcomes) == []

    async def test_caller_cancellation_is_not_a_server_failure(self):
        release = asyncio.Event()
        shielded = []

        async def call_tool(name, arguments, progress_callback=None):
            shielded.append(state.shielded_by_inflight_call())
            await progress_callback(0.1, 1.0, PROGRESS_LABEL_RETRIEVING)
            await release.wait()

        manager, state = _streaming(call_tool)
        got_chunk = asyncio.Event()

        async def consume():
            async for item in manager.execute_tool_streaming("mcp.peer1.query_brain", {}):
                if isinstance(item, ProgressChunk):
                    got_chunk.set()

        task = asyncio.create_task(consume())
        await asyncio.wait_for(got_chunk.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert shielded == [True]  # the running call shielded its session
        assert list(state.recent_outcomes) == []
        assert state.rate_limit_count() == 0
        assert state.connected is True
        assert state.inflight_calls == 0

    async def test_throttled_error_result_records_a_rate_limit_event(self, monkeypatch):
        monkeypatch.setattr(settings, "mcp_health_rate_limit_signal_enabled", True)

        async def call_tool(name, arguments, progress_callback=None):
            return _result("Client error '429 Too Many Requests'", is_error=True)

        manager, state = _streaming(call_tool)
        await _drain(manager)
        assert state.rate_limit_count() == 1
        assert list(state.recent_outcomes) == []

    async def test_throttled_app_exception_is_recorded_and_keeps_the_session(self, monkeypatch):
        import httpx

        monkeypatch.setattr(settings, "mcp_health_rate_limit_signal_enabled", True)
        request = httpx.Request("POST", "https://upstream.example/")
        response = httpx.Response(429, headers={"Retry-After": "20"}, request=request)

        async def call_tool(name, arguments, progress_callback=None):
            raise httpx.HTTPStatusError("throttled", request=request, response=response)

        manager, state = _streaming(call_tool)
        items = await _drain(manager)

        assert _finals(items)[0]["success"] is False
        assert state.rate_limit_count() == 1
        assert state.rate_limited_until["query_brain"] > 0
        # An app error says nothing about the session — a throttle must not read "down".
        assert state.connected is True

    async def test_a_dead_session_still_disconnects(self):
        import anyio

        async def call_tool(name, arguments, progress_callback=None):
            raise anyio.ClosedResourceError()

        manager, state = _streaming(call_tool)
        await _drain(manager)
        assert state.connected is False

    async def test_honours_the_retry_after_gate(self, monkeypatch):
        monkeypatch.setattr(settings, "mcp_rate_limit_backoff_enabled", True)
        calls = []

        async def call_tool(name, arguments, progress_callback=None):
            calls.append(name)
            return _result("fine")

        manager, state = _streaming(call_tool)
        state.rate_limited_until["query_brain"] = time.monotonic() + 60
        items = await _drain(manager)

        assert calls == []
        assert "Rate-Limit" in _finals(items)[0]["message"]
        assert state.rate_limit_count() == 0  # our own refusal is not new evidence

    async def test_per_user_auth_server_never_rides_the_shared_session(self):
        calls = []

        async def call_tool(name, arguments, progress_callback=None):
            calls.append(name)
            return _result("operator data")

        manager, _state = _streaming(call_tool, per_user_auth=True)
        items = await _drain(manager, user_id=None)

        assert calls == []
        assert _finals(items)[0]["success"] is False


# ===========================================================================
# 2. Refresh keeps what the YAML configured
# ===========================================================================

_YAML = textwrap.dedent(
    """
    servers:
      - name: docs
        transport: streamable_http
        url: http://docs.local/mcp
        enabled: true
        prompt_tools: [search_docs, delete_doc, slow_export]
        tool_hints:
          search_docs: "Nutze dieses Werkzeug fuer Dokumentsuchen."
        tool_permissions:
          search_docs: mcp.docs.read
          delete_doc: mcp.docs.manage
        call_timeout:
          slow_export: 120
          default: 20
        health_probe:
          tool: search_docs
          interval: 300
          timeout: 10
    """
)


def _tool(name, description="upstream text"):
    return SimpleNamespace(name=name, description=description, inputSchema={"type": "object"})


class _Upstream:
    """What the fake server currently offers; mutated between connect/refresh."""

    def __init__(self, tools):
        self.tools = list(tools)


def _patch_transport(monkeypatch, upstream: _Upstream):
    import mcp as mcp_mod
    import mcp.client.streamable_http as sh

    class _Transport:
        async def __aenter__(self):
            return (object(), object())

        async def __aexit__(self, *exc):
            return False

    class _Session:
        def __init__(self, read, write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def initialize(self):
            return None

        async def list_tools(self):
            return SimpleNamespace(tools=list(upstream.tools))

    monkeypatch.setattr(sh, "streamablehttp_client", lambda **kw: _Transport())
    monkeypatch.setattr(mcp_mod, "ClientSession", _Session)


def _loaded_manager(tmp_path) -> tuple[MCPManager, MCPServerState]:
    cfg = tmp_path / "mcp_servers.yaml"
    cfg.write_text(_YAML)
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    manager = MCPManager()
    manager.load_config(str(cfg), overlay_dir=str(overlay))
    return manager, manager._servers["docs"]


def _described(manager, name):
    return manager._tool_index[f"mcp.docs.{name}"].description


@pytest.mark.asyncio
class TestRefreshKeepsConfiguredToolShape:
    async def _connect_then_refresh(self, tmp_path, monkeypatch):
        upstream = _Upstream(
            [_tool("search_docs"), _tool("delete_doc"), _tool("slow_export"), _tool("internal_only")]
        )
        _patch_transport(monkeypatch, upstream)
        manager, state = _loaded_manager(tmp_path)
        await manager._connect_server(state)
        assert state.connected is True
        return manager, state, upstream

    async def test_tool_hint_survives_a_refresh(self, tmp_path, monkeypatch):
        manager, state, _ = await self._connect_then_refresh(tmp_path, monkeypatch)
        hint = "Nutze dieses Werkzeug fuer Dokumentsuchen."
        assert _described(manager, "search_docs") == f"upstream text {hint}"

        await manager.refresh_tools()

        assert _described(manager, "search_docs") == f"upstream text {hint}"
        discovered = {t.original_name: t.description for t in state.all_discovered_tools}
        assert discovered["search_docs"] == f"upstream text {hint}"
        assert _described(manager, "delete_doc") == "upstream text"  # no hint, none invented

    async def test_refresh_is_identical_to_a_fresh_connect(self, tmp_path, monkeypatch):
        manager, state, _ = await self._connect_then_refresh(tmp_path, monkeypatch)
        before = (dict(manager._tool_index), list(state.tools), list(state.all_discovered_tools))
        await manager.refresh_tools()
        after = (dict(manager._tool_index), list(state.tools), list(state.all_discovered_tools))
        assert after == before

    async def test_prompt_tools_filter_permissions_timeouts_and_probe_survive(
        self, tmp_path, monkeypatch
    ):
        manager, state, _ = await self._connect_then_refresh(tmp_path, monkeypatch)
        await manager.refresh_tools()

        assert sorted(t.original_name for t in state.tools) == [
            "delete_doc", "search_docs", "slow_export",
        ]
        assert "mcp.docs.internal_only" not in manager._tool_index
        assert len(state.all_discovered_tools) == 4  # admin UI still sees everything

        read_only = ["mcp.docs.read"]
        assert manager._check_tool_permission(manager._tool_index["mcp.docs.search_docs"], read_only) is None
        assert manager._check_tool_permission(manager._tool_index["mcp.docs.delete_doc"], read_only)

        assert _server_call_timeout(state, "slow_export") == 120
        assert _server_call_timeout(state, "search_docs") == 20
        assert state.config.health_probe["tool"] == "search_docs"

    async def test_db_override_survives_a_refresh(self, tmp_path, monkeypatch):
        manager, state, _ = await self._connect_then_refresh(tmp_path, monkeypatch)
        manager._tool_overrides["docs"] = ["search_docs"]
        manager._refilter_server("docs")
        await manager.refresh_tools()
        assert [t.original_name for t in state.tools] == ["search_docs"]
        assert "mcp.docs.delete_doc" not in manager._tool_index

    async def test_a_tool_the_server_dropped_leaves_the_index_on_reconnect(
        self, tmp_path, monkeypatch
    ):
        manager, state, upstream = await self._connect_then_refresh(tmp_path, monkeypatch)
        assert "mcp.docs.delete_doc" in manager._tool_index

        upstream.tools = [_tool("search_docs"), _tool("slow_export")]
        await manager._connect_server(state)  # the reconnect path

        assert "mcp.docs.delete_doc" not in manager._tool_index
        assert sorted(t.original_name for t in state.tools) == ["search_docs", "slow_export"]
