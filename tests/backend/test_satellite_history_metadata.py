"""
Regression guard for the metadata contract between satellite_handler.py and the
agent prompt builder.

Context: #431 — satellite_conversation_history used to append assistant turns as
``{"role", "content"}`` without metadata, so the #430 marker for failed actions
never fired on satellite-sourced history. After the fix, each assistant append
carries ``metadata`` with ``intent`` + ``action_success``, produced by the
module-level ``_build_assistant_metadata`` helper in ``satellite_handler``.

These tests exercise the real helper (not a mirror) so any drift in the shape
will fail the suite:

1. ``_build_assistant_metadata`` returns ``action_success`` as bool (or None)
   so ``agent_service._build_agent_prompt`` can detect failed turns.
2. ``None`` (no tool run) is distinguishable from ``False`` (tool failed).
3. The 10-message in-memory trim preserves the metadata key on retained
   entries — nothing in the trim logic silently drops it.

End-to-end marker rendering is already covered by
``tests/backend/test_agent_service.py::test_failed_action_history_marker_present``;
these tests nail down the producer-side shape that feeds it.
"""
import pytest

from ha_glue.api.websocket.satellite_handler import _build_assistant_metadata


def _append_turn(
    history: list[dict],
    user_text: str,
    response_text: str,
    intent: dict | None,
    action_result: dict | None,
) -> None:
    """Simulate one satellite round-trip by calling the real helper and trimming.

    Only the trim semantics are inlined — the metadata shape comes from the
    production ``_build_assistant_metadata``, so drift in the shape will be
    caught by the shape-focused tests below.
    """
    assistant_metadata = _build_assistant_metadata(intent, action_result)
    history.append({"role": "user", "content": user_text})
    history.append({
        "role": "assistant",
        "content": response_text,
        "metadata": assistant_metadata,
    })
    if len(history) > 10:
        history[:] = history[-10:]


@pytest.mark.unit
def test_failed_action_yields_action_success_false():
    """Failed tool runs must produce ``action_success=False`` for the marker to fire."""
    md = _build_assistant_metadata(
        intent={"intent": "documents.upload"},
        action_result={"success": False, "message": "403 Forbidden"},
    )
    assert md["action_success"] is False
    assert md["intent"] == "documents.upload"


@pytest.mark.unit
def test_missing_action_yields_action_success_none():
    """General-conversation turns (no tool run) produce ``None``, not ``False``.

    This distinction matters because the agent prompt builder only marks
    ``is False`` — ``None`` must stay unmarked.
    """
    md = _build_assistant_metadata(
        intent={"intent": "general.conversation"},
        action_result=None,
    )
    assert md["action_success"] is None
    assert md["intent"] == "general.conversation"


@pytest.mark.unit
def test_missing_intent_yields_intent_none():
    """Defensive: handler-level fallback sets intent at L541, but the helper must
    tolerate ``None`` as input rather than raising AttributeError."""
    md = _build_assistant_metadata(intent=None, action_result={"success": True})
    assert md["intent"] is None
    assert md["action_success"] is True


@pytest.mark.unit
def test_successful_action_yields_action_success_true():
    md = _build_assistant_metadata(
        intent={"intent": "documents.search"},
        action_result={"success": True},
    )
    assert md["action_success"] is True


@pytest.mark.unit
def test_metadata_survives_10_message_trim():
    """The 10-message trim must not drop metadata from retained entries."""
    history: list[dict] = []
    # Simulate 8 exchanges (16 messages) — exceeds the 10-message budget.
    for i in range(8):
        success = i != 3  # one failed turn
        _append_turn(
            history,
            user_text=f"Turn {i} user",
            response_text=f"Turn {i} response",
            intent={"intent": "documents.upload"},
            action_result={"success": success},
        )

    assert len(history) == 10
    assistants = [m for m in history if m["role"] == "assistant"]
    assert len(assistants) == 5
    for msg in assistants:
        assert "metadata" in msg
        assert "action_success" in msg["metadata"]
    failed_turns = [m for m in assistants if m["metadata"]["action_success"] is False]
    assert len(failed_turns) >= 1, "failed turn metadata must survive the trim"
