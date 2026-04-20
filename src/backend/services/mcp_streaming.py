"""
MCP streaming surface — types for AsyncIterator-based tool execution.

`MCPManager.execute_tool_streaming` returns `AsyncIterator[ProgressChunk | dict]`.
Most tools yield exactly one final dict (the same shape `execute_tool` returns).
Streaming-capable tools yield N `ProgressChunk` followed by exactly one final dict.

Why this exists (Lane F1 of second-brain-circles):
  - v2 federation `query_brain` is a long-running responder operation
    (peer wakes Ollama → retrieves → synthesizes → answers). The asker
    wants to surface live progress in chat UI ("asking Mom's brain...
    synthesizing...") instead of a frozen spinner for 10 seconds.
  - Generalises beyond federation: long n8n workflows, streaming TTS,
    image-generation tools all benefit.

Why not a callback / queue: AsyncIterator composes naturally with FastAPI
streaming responses (`StreamingResponse`) and with WebSocket relays in
chat_handler. One iterator → one consumer → no fan-out coordination.

Side-channel rules (per design doc § Federated MCP, "streaming-progress
side-channel mitigation"):
  - `label` is drawn from a small fixed vocabulary; never embed query
    specifics or row counts.
  - `detail` is for caller-side context only (e.g., the peer name being
    queried). Tools MUST NOT leak per-query data through it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# Locked vocabulary. Add labels here, not at call sites — keeps the
# side-channel surface auditable.
#
# Two audiences share this vocabulary:
#   1. Federation responders (query_brain): restricted to
#      FEDERATION_PROGRESS_LABELS below. Generic labels like
#      `awaiting_input` could signal responder-side user behavior
#      ("the remote user is being prompted"), which is a leak.
#   2. Generic long-running MCP tools (n8n workflows, streaming TTS):
#      the full PROGRESS_LABELS set. `awaiting_input` + `tool_running`
#      are safe there because the asker owns both ends.
#
# F1.3 enforces the federation subset at the streamable_http-wire level
# when the tool is served by a paired peer.
PROGRESS_LABEL_WAKING_UP = "waking_up"
PROGRESS_LABEL_RETRIEVING = "retrieving"
PROGRESS_LABEL_SYNTHESIZING = "synthesizing"
PROGRESS_LABEL_TOOL_RUNNING = "tool_running"     # generic non-federation case
PROGRESS_LABEL_AWAITING_INPUT = "awaiting_input"  # interactive tools
PROGRESS_LABEL_COMPLETE = "complete"
PROGRESS_LABEL_FAILED = "failed"

# Federation responders — rate-limited to ≤ 4 chunks per request per
# design doc § "streaming-progress side-channel mitigation".
FEDERATION_PROGRESS_LABELS = frozenset({
    PROGRESS_LABEL_WAKING_UP,
    PROGRESS_LABEL_RETRIEVING,
    PROGRESS_LABEL_SYNTHESIZING,
    PROGRESS_LABEL_COMPLETE,
    PROGRESS_LABEL_FAILED,
})

# Full vocabulary (federation subset + generic-tool extras).
PROGRESS_LABELS = FEDERATION_PROGRESS_LABELS | frozenset({
    PROGRESS_LABEL_TOOL_RUNNING,
    PROGRESS_LABEL_AWAITING_INPUT,
})


@dataclass(frozen=True)
class ProgressChunk:
    """
    One progress notification from a streaming MCP tool.

    Attributes:
        label: One of PROGRESS_LABELS (validated at construction).
        detail: Optional caller-side context (e.g., peer name being queried).
                MUST NOT carry per-query specifics from the responder side
                (atom counts, query terms) — that is a federation side-channel.
        sequence: Monotonic counter starting at 1, incremented per chunk per
                request. Lets the consumer detect missed/reordered chunks.
    """
    label: str
    detail: dict[str, Any] = field(default_factory=dict)
    sequence: int = 0

    def __post_init__(self) -> None:
        if self.label not in PROGRESS_LABELS:
            raise ValueError(
                f"ProgressChunk label {self.label!r} not in PROGRESS_LABELS; "
                f"add it to services/mcp_streaming.PROGRESS_LABELS first."
            )
        if self.sequence < 0:
            raise ValueError(f"ProgressChunk sequence must be >= 0, got {self.sequence}")


# FinalResult is the same shape as MCPManager.execute_tool's return value:
# {"success": bool, "message": str, "data": Any}. Kept as an alias rather
# than a wrapper so the streaming and non-streaming code paths return
# byte-identical final dicts (no consumer changes needed).
#
# Consumer discrimination contract: because FinalResult is a plain dict
# alias, `isinstance(item, FinalResult)` does not work. Consumers of
# `MCPManager.execute_tool_streaming` switch on the positive case:
#     async for item in mgr.execute_tool_streaming(...):
#         if isinstance(item, ProgressChunk):
#             relay_progress_to_ui(item)
#         else:
#             final = item  # FinalResult dict — guaranteed last yield
FinalResult = dict[str, Any]
