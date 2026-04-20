"""
Tests for the MCP streaming surface (services/mcp_streaming.py types +
MCPManager.execute_tool_streaming).

Lane F1 of the second-brain-circles federation plan. F1 ships the surface
+ types + non-streaming default yield-once behavior. F1.3 (follow-up)
adds the streaming wire for streamable_http transports.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from services.mcp_client import MCPManager
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
