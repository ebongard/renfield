"""The metrics wired up for the Grafana dashboards (#82).

Three of them were registered and documented long before, but nothing ever
recorded a sample: `renfield_websocket_connections`,
`renfield_llm_call_duration_seconds` and `renfield_mcp_tool_duration_seconds`.
`renfield_rag_search_duration_seconds` is new. Each is checked against real
`prometheus_client` collectors for: recorded on the path it claims, with the
labels it claims, silent while metrics are disabled, harmless when recording
fails, and label values inside a fixed set.

The agent loop's LLM call sites are covered in `test_agent_service.py`
(`TestLlmCallDurationMetric`), next to the mocks that drive the loop.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

import utils.metrics as metrics_module
from services.mcp_client import (
    MCPManager,
    MCPServerConfig,
    MCPServerState,
    MCPToolInfo,
    MCPTransportType,
)
from tests.backend.metrics_registry import label_values, live_metrics, sample_count

pytestmark = [pytest.mark.unit]

BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"

MCP_COUNT = "renfield_mcp_tool_duration_seconds_count"
RAG_COUNT = "renfield_rag_search_duration_seconds_count"
WS_GAUGE = "renfield_websocket_connections"


@pytest.fixture
def registry():
    with live_metrics() as live:
        yield live


def _boom(*_args, **_kwargs):
    raise RuntimeError("metrics backend exploded")


# =============================================================================
# observe_duration
# =============================================================================


class TestObserveDuration:
    def test_reports_labels_duration_and_ok(self, registry):
        seen = []
        with metrics_module.observe_duration(lambda *a: seen.append(a), "m", "t"):
            pass
        ((model, call_type, seconds, outcome),) = seen
        assert (model, call_type, outcome) == ("m", "t", "ok")
        assert 0 <= seconds < 5

    @pytest.mark.parametrize(
        ("raised", "outcome"),
        [(TimeoutError(), "timeout"), (ValueError("x"), "error")],
        ids=["timeout", "error"],
    )
    def test_exception_is_classified_and_propagates_unchanged(self, registry, raised, outcome):
        seen = []
        with pytest.raises(type(raised)) as caught:
            with metrics_module.observe_duration(lambda *a: seen.append(a)):
                raise raised
        assert caught.value is raised
        assert [args[-1] for args in seen] == [outcome]

    def test_cancellation_is_not_recorded(self, registry):
        seen = []
        with pytest.raises(asyncio.CancelledError):
            with metrics_module.observe_duration(lambda *a: seen.append(a)):
                raise asyncio.CancelledError
        assert seen == []

    def test_a_failing_recorder_does_not_reach_the_caller(self, registry):
        with metrics_module.observe_duration(_boom, "m"):
            result = "the block's own result"
        assert result == "the block's own result"

    def test_a_failing_recorder_does_not_mask_the_blocks_exception(self, registry):
        with pytest.raises(ValueError, match="original"):
            with metrics_module.observe_duration(_boom):
                raise ValueError("original")

    def test_disabled_runs_the_block_and_never_calls_the_recorder(self):
        assert metrics_module._metrics_initialized is False
        record = MagicMock()
        with metrics_module.observe_duration(record, "m"):
            ran = True
        assert ran
        record.assert_not_called()

    def test_outcome_vocabulary_is_what_the_timer_can_produce(self):
        assert metrics_module.CALL_OUTCOMES == ("ok", "timeout", "error")


# =============================================================================
# Recorders: fixed label vocabularies
# =============================================================================


class TestRecorderLabelVocabulary:
    def test_llm_call_clamps_call_type_and_outcome(self, registry):
        metrics_module.record_llm_call("model-a", "agent_step", 0.2, "ok")
        metrics_module.record_llm_call("model-a", "written by a caller", 0.2, "whatever")

        family = "renfield_llm_call_duration_seconds"
        assert label_values(registry, family, "call_type") == {"agent_step", "other"}
        assert label_values(registry, family, "outcome") == {"ok", "error"}
        assert label_values(registry, family, "call_type") <= {*metrics_module.LLM_CALL_TYPES, "other"}

    def test_mcp_tool_call_clamps_outcome(self, registry):
        for outcome in (*metrics_module.MCP_TOOL_OUTCOMES, "Tool-Aufruf fehlgeschlagen: boom"):
            metrics_module.record_mcp_tool_call("weather", 0.1, outcome)

        values = label_values(registry, "renfield_mcp_tool_duration_seconds", "outcome")
        assert values == set(metrics_module.MCP_TOOL_OUTCOMES)
        assert sample_count(registry, MCP_COUNT, server="weather", outcome="error") == 2

    def test_rag_search_clamps_outcome(self, registry):
        metrics_module.record_rag_search(0.1, "ok")
        metrics_module.record_rag_search(0.1, "a query text")

        values = label_values(registry, "renfield_rag_search_duration_seconds", "outcome")
        assert values == {"ok", "error"}

    def test_websocket_type_is_clamped(self, registry):
        metrics_module.record_websocket_connect("/ws/some/raw/path?token=secret")
        assert sample_count(registry, WS_GAUGE, type="other") == 1
        assert label_values(registry, WS_GAUGE, "type") == {"other"}

    def test_no_instrumented_metric_has_a_tool_or_identity_label(self, registry):
        from tests.backend.metrics_registry import registered_series

        forbidden = {"tool", "user", "user_id", "session", "session_id", "query", "path", "entity"}
        for name in (
            "renfield_mcp_tool_duration_seconds_bucket",
            "renfield_llm_call_duration_seconds_bucket",
            "renfield_rag_search_duration_seconds_bucket",
            WS_GAUGE,
        ):
            assert not (registered_series()[name] & forbidden), name

    def test_recorders_are_silent_while_disabled(self, monkeypatch):
        assert metrics_module._metrics_initialized is False
        collectors = MagicMock()
        for slot in (
            "_llm_call_duration_seconds",
            "_mcp_tool_duration_seconds",
            "_rag_search_duration_seconds",
            "_websocket_connections",
        ):
            monkeypatch.setattr(metrics_module, slot, getattr(collectors, slot))

        metrics_module.record_llm_call("m", "agent_step", 1.0, "ok")
        metrics_module.record_mcp_tool_call("weather", 1.0, "ok")
        metrics_module.record_rag_search(1.0, "ok")
        metrics_module.record_websocket_connect("chat")
        metrics_module.record_websocket_disconnect("chat")

        assert collectors.mock_calls == []


# =============================================================================
# MCP tool calls
# =============================================================================


def _text_result(text: str = "fine", is_error: bool = False):
    return SimpleNamespace(isError=is_error, content=[SimpleNamespace(type="text", text=text)])


def _manager(call_tool, *, streaming: bool = False, schema: dict | None = None) -> MCPManager:
    """An MCPManager with one connected server `weather` and one tool."""
    manager = MCPManager()
    state = MCPServerState(config=MCPServerConfig(
        name="weather",
        url="http://weather.local/mcp",
        transport=MCPTransportType.STREAMABLE_HTTP,
        streaming=streaming,
    ))
    state.session = MagicMock()
    state.session.call_tool = call_tool
    state.connected = True
    manager._servers["weather"] = state
    manager._tool_index["mcp.weather.forecast"] = MCPToolInfo(
        server_name="weather",
        original_name="forecast",
        namespaced_name="mcp.weather.forecast",
        description="forecast",
        input_schema=schema or {"type": "object", "properties": {}},
    )
    return manager


async def _streamed(manager: MCPManager) -> dict:
    items = [item async for item in manager.execute_tool_streaming("mcp.weather.forecast", {})]
    return items[-1]


class TestMcpToolCallMetric:
    async def test_clean_result_is_ok_under_the_server_name(self, registry):
        manager = _manager(AsyncMock(return_value=_text_result()))

        result = await manager.execute_tool("mcp.weather.forecast", {})

        assert result["success"] is True
        assert sample_count(registry, MCP_COUNT, server="weather", outcome="ok") == 1
        assert label_values(registry, "renfield_mcp_tool_duration_seconds", "server") == {"weather"}

    async def test_error_result_is_app_error_not_a_failed_call(self, registry):
        manager = _manager(AsyncMock(return_value=_text_result("device is off", is_error=True)))

        result = await manager.execute_tool("mcp.weather.forecast", {})

        assert result["success"] is False
        assert sample_count(registry, MCP_COUNT, server="weather", outcome="app_error") == 1
        assert sample_count(registry, MCP_COUNT, server="weather", outcome="error") == 0

    async def test_inner_error_envelope_is_app_error(self, registry):
        manager = _manager(AsyncMock(return_value=_text_result('{"ok": false, "error": "nope"}')))

        await manager.execute_tool("mcp.weather.forecast", {})

        assert sample_count(registry, MCP_COUNT, server="weather", outcome="app_error") == 1

    async def test_timeout_is_timeout_like_the_health_verdict_counts_it(self, registry):
        manager = _manager(AsyncMock(side_effect=TimeoutError()))

        result = await manager.execute_tool("mcp.weather.forecast", {})

        assert result["success"] is False
        assert sample_count(registry, MCP_COUNT, server="weather", outcome="timeout") == 1
        # The same exit that feeds `calls_failing`.
        assert list(manager._servers["weather"].recent_outcomes) == [False]

    async def test_application_exception_is_error(self, registry):
        manager = _manager(AsyncMock(side_effect=ValueError("upstream said no")))

        result = await manager.execute_tool("mcp.weather.forecast", {})

        assert result["success"] is False
        assert sample_count(registry, MCP_COUNT, server="weather", outcome="error") == 1

    async def test_refusal_before_dispatch_is_not_observed(self, registry):
        call_tool = AsyncMock(return_value=_text_result())
        manager = _manager(call_tool, schema={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        })

        unknown = await manager.execute_tool("mcp.nowhere.invented_by_the_model", {})
        invalid = await manager.execute_tool("mcp.weather.forecast", {})

        assert unknown["success"] is False and invalid["success"] is False
        call_tool.assert_not_called()
        assert label_values(registry, "renfield_mcp_tool_duration_seconds", "server") == set()

    async def test_streaming_path_shares_the_accounting(self, registry):
        async def call_tool(name, arguments, progress_callback=None):
            return _text_result()

        final = await _streamed(_manager(call_tool, streaming=True))

        assert final["success"] is True
        assert sample_count(registry, MCP_COUNT, server="weather", outcome="ok") == 1

    async def test_streaming_timeout_and_error(self, registry):
        async def times_out(name, arguments, progress_callback=None):
            raise TimeoutError

        async def raises(name, arguments, progress_callback=None):
            raise ValueError("upstream said no")

        assert (await _streamed(_manager(times_out, streaming=True)))["success"] is False
        assert (await _streamed(_manager(raises, streaming=True)))["success"] is False

        assert sample_count(registry, MCP_COUNT, server="weather", outcome="timeout") == 1
        assert sample_count(registry, MCP_COUNT, server="weather", outcome="error") == 1

    @pytest.mark.parametrize("streaming", [False, True], ids=["plain", "streaming"])
    async def test_a_failing_recorder_does_not_reach_the_caller(self, registry, monkeypatch, streaming):
        monkeypatch.setattr(metrics_module, "record_mcp_tool_call", _boom)

        async def call_tool(name, arguments, progress_callback=None):
            return _text_result("the answer")

        manager = _manager(call_tool, streaming=streaming)
        if streaming:
            result = await _streamed(manager)
        else:
            result = await manager.execute_tool("mcp.weather.forecast", {})

        assert result == {
            "success": True,
            "message": "the answer",
            "data": [{"type": "text", "text": "the answer"}],
        }

    async def test_disabled_touches_no_collector(self, monkeypatch):
        assert metrics_module._metrics_initialized is False
        histogram = MagicMock()
        monkeypatch.setattr(metrics_module, "_mcp_tool_duration_seconds", histogram)
        manager = _manager(AsyncMock(return_value=_text_result()))

        result = await manager.execute_tool("mcp.weather.forecast", {})

        assert result["success"] is True
        assert histogram.mock_calls == []


# =============================================================================
# RAG search
# =============================================================================


class TestRagSearchMetric:
    def _retrieval(self, pipeline):
        from services.rag_retrieval import RAGRetrieval

        retrieval = RAGRetrieval(MagicMock())
        retrieval._search_pipeline = pipeline
        return retrieval

    async def test_search_is_timed_and_returns_the_pipeline_result(self, registry):
        hits = [{"chunk": "c", "document": "d", "similarity": 0.9}]
        pipeline = AsyncMock(return_value=hits)

        result = await self._retrieval(pipeline).search(
            "a question", top_k=3, knowledge_base_id=7, similarity_threshold=0.4, user_id=2,
        )

        assert result is hits
        pipeline.assert_awaited_once_with("a question", 3, 7, 0.4, 2, None)
        assert sample_count(registry, RAG_COUNT, outcome="ok") == 1

    async def test_failing_search_is_error_and_still_raises(self, registry):
        with pytest.raises(RuntimeError, match="database gone"):
            await self._retrieval(AsyncMock(side_effect=RuntimeError("database gone"))).search("q")

        assert sample_count(registry, RAG_COUNT, outcome="error") == 1
        assert sample_count(registry, RAG_COUNT, outcome="ok") == 0

    async def test_a_failing_recorder_does_not_reach_the_caller(self, registry, monkeypatch):
        import services.rag_retrieval as rag_retrieval

        monkeypatch.setattr(rag_retrieval, "record_rag_search", _boom)

        assert await self._retrieval(AsyncMock(return_value=["hit"])).search("q") == ["hit"]

    async def test_disabled_never_calls_the_recorder(self, monkeypatch):
        import services.rag_retrieval as rag_retrieval

        assert metrics_module._metrics_initialized is False
        record = MagicMock()
        monkeypatch.setattr(rag_retrieval, "record_rag_search", record)

        assert await self._retrieval(AsyncMock(return_value=["hit"])).search("q") == ["hit"]
        record.assert_not_called()

    def test_search_is_the_only_caller_of_the_pipeline(self):
        """Every retrieval goes through `search`; a second caller of the
        untimed pipeline would be a search the metric does not see."""
        callers = []
        for path in BACKEND.rglob("*.py"):
            if "satellite" in path.relative_to(BACKEND).parts[:1]:
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "_search_pipeline"
                ):
                    callers.append(path.relative_to(BACKEND).as_posix())
        assert callers == ["services/rag_retrieval.py"]


# =============================================================================
# WebSocket connection gauge
# =============================================================================


def _websocket_app():
    app = FastAPI()

    async def echo(websocket: WebSocket):
        await websocket.accept()
        try:
            while True:
                await websocket.send_text(await websocket.receive_text())
        except WebSocketDisconnect:
            pass

    async def refuses(websocket: WebSocket):
        await websocket.close(code=4401)

    async def crashes(websocket: WebSocket):
        await websocket.accept()
        await websocket.receive_text()
        raise RuntimeError("handler bug")

    app.add_api_websocket_route("/ws/kiosk", echo)
    app.add_api_websocket_route("/ws/plugin/{room}", echo)
    app.add_api_websocket_route("/ws/user", refuses)
    app.add_api_websocket_route("/ws/device", crashes)

    @app.get("/plain")
    async def plain():
        return {"ok": True}

    app.add_middleware(metrics_module.WebSocketGaugeMiddleware)
    return app


class TestWebSocketGauge:
    def test_open_connection_is_counted_and_released(self, registry):
        from starlette.testclient import TestClient

        with TestClient(_websocket_app()) as client:
            with client.websocket_connect("/ws/kiosk") as first:
                first.send_text("ping")
                assert first.receive_text() == "ping"
                assert sample_count(registry, WS_GAUGE, type="kiosk") == 1
                with client.websocket_connect("/ws/kiosk?token=abc") as second:
                    second.send_text("ping")
                    second.receive_text()
                    assert sample_count(registry, WS_GAUGE, type="kiosk") == 2
            assert sample_count(registry, WS_GAUGE, type="kiosk") == 0
        assert label_values(registry, WS_GAUGE, "type") == {"kiosk"}

    def test_unlisted_path_counts_as_other_and_never_as_the_path(self, registry):
        from starlette.testclient import TestClient

        with TestClient(_websocket_app()) as client:
            with client.websocket_connect("/ws/plugin/kitchen") as socket:
                socket.send_text("ping")
                socket.receive_text()
                assert sample_count(registry, WS_GAUGE, type="other") == 1
        assert label_values(registry, WS_GAUGE, "type") == {"other"}

    def test_refused_handshake_is_never_counted(self, registry):
        from starlette.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect

        with TestClient(_websocket_app()) as client:
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect("/ws/user"):
                    pass
        assert label_values(registry, WS_GAUGE, "type") == set()

    def test_crashing_handler_releases_its_connection(self, registry):
        from starlette.testclient import TestClient

        with TestClient(_websocket_app()) as client:
            with pytest.raises(RuntimeError, match="handler bug"):
                with client.websocket_connect("/ws/device") as socket:
                    socket.send_text("go")
                    socket.receive_text()
        assert label_values(registry, WS_GAUGE, "type") == {"device"}
        assert sample_count(registry, WS_GAUGE, type="device") == 0

    def test_http_requests_pass_through_untouched(self, registry):
        from starlette.testclient import TestClient

        with TestClient(_websocket_app()) as client:
            assert client.get("/plain").json() == {"ok": True}
        assert label_values(registry, WS_GAUGE, "type") == set()

    def test_a_failing_recorder_does_not_break_the_connection(self, registry, monkeypatch):
        from starlette.testclient import TestClient

        monkeypatch.setattr(metrics_module, "record_websocket_connect", _boom)
        monkeypatch.setattr(metrics_module, "record_websocket_disconnect", _boom)

        with TestClient(_websocket_app()) as client:
            with client.websocket_connect("/ws/kiosk") as socket:
                socket.send_text("ping")
                assert socket.receive_text() == "ping"

    def test_setup_installs_the_middleware_and_primes_every_type(self, registry, monkeypatch):
        from fastapi import FastAPI

        monkeypatch.setattr(metrics_module.settings, "metrics_enabled", True)
        monkeypatch.setattr("utils.llm_client.default_agent_model", lambda: "")
        app = FastAPI()

        metrics_module.setup_metrics(app)

        installed = [middleware.cls for middleware in app.user_middleware]
        assert metrics_module.WebSocketGaugeMiddleware in installed
        expected = {*metrics_module.WEBSOCKET_TYPES.values(), "other"}
        assert label_values(registry, WS_GAUGE, "type") == expected
        assert all(sample_count(registry, WS_GAUGE, type=t) == 0 for t in expected)

    def test_disabled_setup_installs_nothing(self, monkeypatch):
        from fastapi import FastAPI

        monkeypatch.setattr(metrics_module.settings, "metrics_enabled", False)
        app = FastAPI()

        metrics_module.setup_metrics(app)

        assert app.user_middleware == []
        assert all(getattr(route, "path", None) != "/metrics" for route in app.routes)

    def test_every_websocket_route_of_the_backend_has_a_type(self):
        """A route missing from WEBSOCKET_TYPES would be lumped into `other`."""
        paths = set()
        for path in BACKEND.rglob("*.py"):
            if "satellite" in path.relative_to(BACKEND).parts[:1]:
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "websocket"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    paths.add(node.args[0].value)
        assert paths, "the scan found no WebSocket route at all"
        assert paths == set(metrics_module.WEBSOCKET_TYPES)
