"""
Tests for the MCP streaming surface (services/mcp_streaming.py types +
MCPManager.execute_tool_streaming).

Lane F1 of the second-brain-circles federation plan.
- F1.1/F1.2 ship types + yield-once default.
- F1.3 adds the streaming wire for servers flagged `streaming: true`.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.mcp_client import (
    MCPManager,
    MCPServerConfig,
    MCPServerState,
    MCPToolInfo,
    MCPTransportType,
)
from services.mcp_streaming import (
    FEDERATION_PROGRESS_LABELS,
    PROGRESS_LABEL_COMPLETE,
    PROGRESS_LABEL_RETRIEVING,
    PROGRESS_LABEL_SYNTHESIZING,
    PROGRESS_LABEL_TOOL_RUNNING,
    PROGRESS_LABEL_WAKING_UP,
    PROGRESS_LABELS,
    ProgressChunk,
)


class TestProgressChunk:
    @pytest.mark.unit
    def test_known_label_accepted(self):
        chunk = ProgressChunk(label=PROGRESS_LABEL_WAKING_UP)
        assert chunk.label == "waking_up"
        assert chunk.detail == {}
        assert chunk.sequence == 0

    @pytest.mark.unit
    def test_unknown_label_rejected(self):
        with pytest.raises(ValueError, match="not in PROGRESS_LABELS"):
            ProgressChunk(label="peer_has_47_atoms")  # would leak atom count

    @pytest.mark.unit
    def test_negative_sequence_rejected(self):
        with pytest.raises(ValueError, match="sequence must be >= 0"):
            ProgressChunk(label=PROGRESS_LABEL_RETRIEVING, sequence=-1)

    @pytest.mark.unit
    def test_frozen_dataclass(self):
        chunk = ProgressChunk(label=PROGRESS_LABEL_COMPLETE)
        with pytest.raises(Exception):  # FrozenInstanceError under dataclass
            chunk.label = PROGRESS_LABEL_WAKING_UP  # type: ignore[misc]

    @pytest.mark.unit
    def test_locked_vocabulary_has_all_federation_labels(self):
        # Federation responder needs exactly these labels per design doc
        # § "streaming-progress side-channel mitigation".
        required = {
            PROGRESS_LABEL_WAKING_UP,
            PROGRESS_LABEL_RETRIEVING,
            PROGRESS_LABEL_SYNTHESIZING,
            PROGRESS_LABEL_COMPLETE,
            "failed",  # PROGRESS_LABEL_FAILED
        }
        assert required.issubset(PROGRESS_LABELS)

    @pytest.mark.unit
    def test_federation_subset_excludes_generic_labels(self):
        # Federation responders MUST NOT emit `tool_running` or
        # `awaiting_input` — those can leak responder-side user behavior.
        # F1.3 enforces this at the wire level when the tool is served
        # by a paired peer; this test guards the policy at the type layer.
        assert PROGRESS_LABEL_TOOL_RUNNING not in FEDERATION_PROGRESS_LABELS
        assert "awaiting_input" not in FEDERATION_PROGRESS_LABELS
        assert FEDERATION_PROGRESS_LABELS.issubset(PROGRESS_LABELS)


class TestExecuteToolStreamingYieldOnce:
    """F1.2: default implementation yields exactly one FinalResult."""

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_delegates_to_execute_tool_once(self):
        manager = MCPManager()
        sentinel = {"success": True, "message": "done", "data": None}
        with patch.object(manager, "execute_tool", new=AsyncMock(return_value=sentinel)) as mock:
            chunks = []
            async for item in manager.execute_tool_streaming(
                "mcp.server.tool", {"x": 1}, user_permissions=["a"], user_id=42,
            ):
                chunks.append(item)

        mock.assert_awaited_once_with(
            namespaced_name="mcp.server.tool",
            arguments={"x": 1},
            user_permissions=["a"],
            user_id=42,
        )
        assert chunks == [sentinel]

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_error_result_still_yields_once(self):
        """Non-streaming tools surface errors via the FinalResult dict, not exceptions."""
        manager = MCPManager()
        error = {"success": False, "message": "boom", "data": None}
        with patch.object(manager, "execute_tool", new=AsyncMock(return_value=error)):
            chunks = [c async for c in manager.execute_tool_streaming("mcp.s.t", {})]
        assert chunks == [error]
        assert chunks[0]["success"] is False

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_aclose_after_final_yield_is_safe(self):
        """Closing the iterator after it already produced its final result
        must be a no-op (the generator is already exhausted)."""
        manager = MCPManager()
        sentinel = {"success": True, "message": "", "data": None}
        with patch.object(manager, "execute_tool", new=AsyncMock(return_value=sentinel)):
            it = manager.execute_tool_streaming("mcp.s.t", {})
            first = await it.__anext__()
            # Generator is done after the single yield; aclose must not raise.
            await it.aclose()
        assert first == sentinel

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_aclose_before_first_anext_never_invokes_tool(self):
        """Creating the generator does not start work. Closing it before
        the first __anext__() MUST NOT call execute_tool at all — the
        docstring promises this semantic so consumers can defensively
        construct-then-close without side-effects."""
        manager = MCPManager()
        mock_execute = AsyncMock(return_value={"success": True, "message": "", "data": None})
        with patch.object(manager, "execute_tool", new=mock_execute):
            it = manager.execute_tool_streaming("mcp.s.t", {})
            await it.aclose()
        mock_execute.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_aclose_mid_call_cancels_background_task(self):
        """If the consumer closes the iterator while execute_tool is still
        running, the underlying await is cancelled. The result is discarded
        (not yielded anywhere)."""
        import asyncio

        manager = MCPManager()
        started = asyncio.Event()

        async def slow_tool(*args, **kwargs):
            started.set()
            await asyncio.sleep(10)  # would exceed test timeout if not cancelled
            return {"success": True, "message": "", "data": None}

        with patch.object(manager, "execute_tool", new=AsyncMock(side_effect=slow_tool)):
            it = manager.execute_tool_streaming("mcp.s.t", {})

            async def consume_first():
                return await it.__anext__()

            task = asyncio.create_task(consume_first())
            await started.wait()  # execute_tool is now running
            await it.aclose()     # close mid-await
            # The consume task should fail because the generator closed
            # before yielding anything.
            with pytest.raises((StopAsyncIteration, asyncio.CancelledError, GeneratorExit)):
                await task

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_result_shape_matches_execute_tool(self):
        """FinalResult yielded by streaming path equals execute_tool output."""
        manager = MCPManager()
        full_shape = {
            "success": True,
            "message": "result text",
            "data": [{"type": "text", "text": "raw"}],
        }
        with patch.object(manager, "execute_tool", new=AsyncMock(return_value=full_shape)):
            chunks = [c async for c in manager.execute_tool_streaming("mcp.s.t", {})]
        # Equality (not identity) — lets future wrappers add envelope
        # metadata (trace_id, timing) without breaking this regression guard.
        assert chunks[0] == full_shape


# =============================================================================
# F1.3 — actual streaming wire (servers flagged `streaming: true`)
# =============================================================================


def _streaming_manager(session_call_tool):
    """Build an MCPManager with one streaming-capable mock server + tool.

    `session_call_tool` is the coroutine function used as session.call_tool —
    it receives (name, arguments, progress_callback=...) and returns the
    CallToolResult mock.
    """
    manager = MCPManager()

    # Fake server state
    config = MCPServerConfig(
        name="peer1",
        url="http://peer1.local/mcp",
        transport=MCPTransportType.STREAMABLE_HTTP,
        streaming=True,
    )
    session = MagicMock()
    session.call_tool = session_call_tool
    state = MCPServerState(config=config)
    state.session = session
    state.connected = True
    manager._servers["peer1"] = state

    # Register a tool on it
    tool = MCPToolInfo(
        server_name="peer1",
        original_name="query_brain",
        namespaced_name="mcp.peer1.query_brain",
        description="federated peer query",
        input_schema={"type": "object", "properties": {}},
    )
    manager._tool_index["mcp.peer1.query_brain"] = tool
    return manager


class TestExecuteToolStreamingWire:
    """F1.3 — real streaming path (server opts in via streaming: true)."""

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_streaming_yields_progress_chunks_then_final(self):
        """The progress_callback is exposed via MCP SDK call_tool kwarg.
        Invoking it N times should produce N ProgressChunks before the
        final dict."""
        async def fake_call_tool(name, arguments, progress_callback=None, **kwargs):
            # Simulate a responder emitting three progress notifications.
            if progress_callback:
                await progress_callback(0.1, 1.0, PROGRESS_LABEL_WAKING_UP)
                await progress_callback(0.5, 1.0, PROGRESS_LABEL_RETRIEVING)
                await progress_callback(0.9, 1.0, PROGRESS_LABEL_SYNTHESIZING)
            # Final CallToolResult shape
            return SimpleNamespace(
                isError=False,
                content=[SimpleNamespace(type="text", text="Mom's favorite recipe is pasta.")],
            )

        manager = _streaming_manager(fake_call_tool)

        items = [item async for item in manager.execute_tool_streaming(
            "mcp.peer1.query_brain", {}, user_permissions=None,
        )]

        progress = [i for i in items if isinstance(i, ProgressChunk)]
        finals = [i for i in items if not isinstance(i, ProgressChunk)]
        assert len(progress) == 3
        assert [p.label for p in progress] == [
            PROGRESS_LABEL_WAKING_UP,
            PROGRESS_LABEL_RETRIEVING,
            PROGRESS_LABEL_SYNTHESIZING,
        ]
        assert [p.sequence for p in progress] == [1, 2, 3]
        # Sequence is monotonic, starting at 1.
        assert len(finals) == 1
        assert finals[0]["success"] is True
        assert "pasta" in finals[0]["message"]

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_unknown_progress_label_falls_back_to_tool_running(self):
        """A misbehaving responder emitting a label not in PROGRESS_LABELS
        must not crash the asker. The callback maps unknown labels to
        PROGRESS_LABEL_TOOL_RUNNING."""
        async def fake_call_tool(name, arguments, progress_callback=None, **kwargs):
            if progress_callback:
                await progress_callback(0.5, 1.0, "peer_has_47_atoms")  # leak attempt
            return SimpleNamespace(isError=False, content=[])

        manager = _streaming_manager(fake_call_tool)
        items = [item async for item in manager.execute_tool_streaming(
            "mcp.peer1.query_brain", {},
        )]

        progress = [i for i in items if isinstance(i, ProgressChunk)]
        assert len(progress) == 1
        assert progress[0].label == PROGRESS_LABEL_TOOL_RUNNING

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_zero_progress_notifications_still_yields_final(self):
        """A streaming-capable server that never emits progress should still
        produce a FinalResult — streaming path handles silent tools too."""
        async def fake_call_tool(name, arguments, progress_callback=None, **kwargs):
            return SimpleNamespace(isError=False, content=[SimpleNamespace(type="text", text="ok")])

        manager = _streaming_manager(fake_call_tool)
        items = [item async for item in manager.execute_tool_streaming(
            "mcp.peer1.query_brain", {},
        )]

        assert len(items) == 1
        assert items[0]["success"] is True

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_call_tool_without_progress_callback_kwarg_falls_back(self):
        """Older MCP SDK versions don't accept progress_callback. The
        streaming path must retry without the kwarg instead of crashing."""
        call_count = 0

        async def fake_call_tool(name, arguments, **kwargs):
            nonlocal call_count
            call_count += 1
            if "progress_callback" in kwargs:
                # Simulate older SDK: reject the kwarg
                raise TypeError("call_tool() got unexpected keyword argument 'progress_callback'")
            return SimpleNamespace(isError=False, content=[SimpleNamespace(type="text", text="legacy ok")])

        # The SDK raises TypeError at call_tool() invocation. To simulate
        # that accurately with the eager vs lazy split in our wrapper, we
        # use a callable whose first call raises.
        outer_calls = []

        def eager_call_tool(name, arguments, progress_callback=None):
            outer_calls.append(progress_callback)
            if progress_callback is not None:
                raise TypeError("unexpected kwarg progress_callback")
            # Return a coroutine that yields the mock result
            async def _run():
                return SimpleNamespace(
                    isError=False, content=[SimpleNamespace(type="text", text="legacy ok")],
                )
            return _run()

        manager = _streaming_manager(eager_call_tool)
        items = [item async for item in manager.execute_tool_streaming(
            "mcp.peer1.query_brain", {},
        )]
        assert len(items) == 1
        assert items[0]["success"] is True
        assert items[0]["message"] == "legacy ok"
        # First attempt used progress_callback, fallback without it succeeded.
        assert len(outer_calls) == 2
        assert outer_calls[0] is not None  # first try
        assert outer_calls[1] is None      # fallback

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_unrelated_typeerror_is_not_swallowed(self):
        """Narrow catch: only the 'unexpected keyword progress_callback'
        TypeError triggers the fallback. Other TypeErrors (e.g. bad
        arguments type) must propagate instead of silently retrying."""
        def bad_call_tool(name, arguments, progress_callback=None):
            # Simulates a genuinely broken call (e.g. arguments type mismatch)
            # that happens to raise TypeError without mentioning progress_callback.
            raise TypeError("arguments must be a dict, got list")

        manager = _streaming_manager(bad_call_tool)
        with pytest.raises(TypeError, match="must be a dict"):
            async for _ in manager.execute_tool_streaming("mcp.peer1.query_brain", []):
                pass

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_non_streaming_server_uses_yield_once_path(self):
        """Servers without `streaming: true` take the existing execute_tool
        wrapper path — no progress callback is wired, no chunks emitted."""
        manager = MCPManager()
        config = MCPServerConfig(name="plain", streaming=False)
        state = MCPServerState(config=config)
        state.session = MagicMock()
        state.connected = True
        manager._servers["plain"] = state
        manager._tool_index["mcp.plain.t"] = MCPToolInfo(
            server_name="plain", original_name="t", namespaced_name="mcp.plain.t",
            description="", input_schema={},
        )

        sentinel = {"success": True, "message": "fast path", "data": None}
        with patch.object(manager, "execute_tool", new=AsyncMock(return_value=sentinel)) as mock:
            items = [i async for i in manager.execute_tool_streaming("mcp.plain.t", {})]

        mock.assert_awaited_once()
        assert items == [sentinel]


class TestMCPServerConfigStreaming:
    @pytest.mark.unit
    def test_default_is_false(self):
        config = MCPServerConfig(name="x")
        assert config.streaming is False

    @pytest.mark.unit
    def test_explicit_true(self):
        config = MCPServerConfig(name="x", streaming=True)
        assert config.streaming is True
