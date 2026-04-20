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
    async def test_consumer_can_break_early(self):
        """Breaking out of the iterator mid-stream must not raise or leak."""
        manager = MCPManager()
        sentinel = {"success": True, "message": "", "data": None}
        with patch.object(manager, "execute_tool", new=AsyncMock(return_value=sentinel)):
            it = manager.execute_tool_streaming("mcp.s.t", {})
            first = await it.__anext__()
            # Consumer aborts — no further iteration. Must not crash.
            await it.aclose()
        assert first == sentinel

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_result_shape_matches_execute_tool(self):
        """FinalResult yielded by streaming path is byte-identical to execute_tool output."""
        manager = MCPManager()
        full_shape = {
            "success": True,
            "message": "result text",
            "data": [{"type": "text", "text": "raw"}],
        }
        with patch.object(manager, "execute_tool", new=AsyncMock(return_value=full_shape)):
            chunks = [c async for c in manager.execute_tool_streaming("mcp.s.t", {})]
        assert chunks[0] is full_shape  # same object identity — no copy
