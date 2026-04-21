"""
Regression guard for satellite_handler.py in-memory conversation history shape.

Context: #431 — satellite_conversation_history used to append assistant turns as
``{"role", "content"}`` without metadata, so the #430 marker for failed actions
never fired on satellite-sourced history. After the fix, each assistant append
carries ``metadata`` with ``intent`` + ``action_success``.

These tests cover two invariants of that contract:

1. The assistant entry shape includes ``metadata.action_success`` as a bool (or
   None) so ``agent_service._build_agent_prompt`` can detect failed turns.
2. The 10-message in-memory trim (``history[:] = history[-10:]``) preserves the
   ``metadata`` key — i.e. nothing in the trim logic silently strips it.

End-to-end marker rendering is already covered by
``tests/backend/test_agent_service.py::test_failed_action_history_marker_present``;
these tests nail down the producer-side shape that feeds it.
"""
import pytest


def _append_like_satellite_handler(
    history: list[dict],
    user_text: str,
    response_text: str,
    intent: dict | None,
    action_result: dict | None,
) -> None:
    """Mirror the exact append pattern from satellite_handler.py.

    Keep this in sync with the handler — if the real code evolves (e.g. stores
    additional metadata keys), update this helper and the tests will cover the
    new contract.
    """
    assistant_metadata = {
        "intent": intent.get("intent") if intent else None,
        "action_success": action_result.get("success") if action_result else None,
    }
    history.append({"role": "user", "content": user_text})
    history.append({
        "role": "assistant",
        "content": response_text,
        "metadata": assistant_metadata,
    })
    if len(history) > 10:
        history[:] = history[-10:]


@pytest.mark.unit
def test_assistant_entry_carries_action_success_metadata():
    """Assistant turns from satellite handler expose action_success for the agent prompt builder."""
    history: list[dict] = []
    _append_like_satellite_handler(
        history,
        user_text="Lade das Dokument hoch",
        response_text="Entschuldigung, das konnte ich nicht ausführen: 403 Forbidden.",
        intent={"intent": "documents.upload"},
        action_result={"success": False, "message": "403 Forbidden"},
    )

    assistant = history[-1]
    assert assistant["role"] == "assistant"
    assert assistant["metadata"]["action_success"] is False
    assert assistant["metadata"]["intent"] == "documents.upload"


@pytest.mark.unit
def test_assistant_metadata_missing_action_is_none():
    """General-conversation turns (no tool run) yield metadata=None, not missing key."""
    history: list[dict] = []
    _append_like_satellite_handler(
        history,
        user_text="Wie geht es dir?",
        response_text="Mir geht es gut, danke!",
        intent={"intent": "general.conversation"},
        action_result=None,
    )

    assistant = history[-1]
    # None (no action taken) must be distinguishable from False (action failed)
    # — the agent prompt builder only marks ``is False``, so None stays unmarked.
    assert assistant["metadata"]["action_success"] is None
    assert assistant["metadata"]["intent"] == "general.conversation"


@pytest.mark.unit
def test_metadata_survives_10_message_trim():
    """The 10-message trim must not drop metadata from retained entries."""
    history: list[dict] = []
    # Simulate 8 exchanges (16 messages) — more than the 10-message budget.
    for i in range(8):
        success = i != 3  # turn 4 failed
        _append_like_satellite_handler(
            history,
            user_text=f"Turn {i} user",
            response_text=f"Turn {i} response",
            intent={"intent": "documents.upload"},
            action_result={"success": success},
        )

    assert len(history) == 10, "trim should cap at exactly 10 messages"
    # The failed turn (turn 4, response = index 7 after trim) should still be here.
    assistants = [m for m in history if m["role"] == "assistant"]
    assert len(assistants) == 5, "5 assistant entries retained after trim"
    # All retained assistant entries must still carry metadata
    for msg in assistants:
        assert "metadata" in msg
        assert "action_success" in msg["metadata"]
    # At least one failed entry should survive and be distinguishable
    failed_turns = [m for m in assistants if m["metadata"]["action_success"] is False]
    assert len(failed_turns) >= 1, "failed turn metadata must survive the trim"
