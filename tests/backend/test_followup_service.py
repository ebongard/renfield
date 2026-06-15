"""
Tests for `services.followup_service` — follow-up suggestion chips.

The parser is the load-bearing part (local models emit JSON *or* loose lines);
`generate_followups` must be best-effort — `[]` on any failure, never raising.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.followup_service import _parse_followups, generate_followups


# ---- _parse_followups (pure) -------------------------------------------------

@pytest.mark.unit
def test_parses_json_array():
    raw = '["Wie viel kostet das?", "Wann ist es fällig?"]'
    assert _parse_followups(raw, 3) == ["Wie viel kostet das?", "Wann ist es fällig?"]


@pytest.mark.unit
def test_parses_json_wrapped_in_prose_or_fence():
    raw = 'Here you go:\n```json\n["A?", "B?"]\n```'
    assert _parse_followups(raw, 3) == ["A?", "B?"]


@pytest.mark.unit
def test_line_fallback_strips_bullets_and_numbering():
    raw = "- Frage eins?\n2. Frage zwei?\n• Frage drei?"
    assert _parse_followups(raw, 3) == ["Frage eins?", "Frage zwei?", "Frage drei?"]


@pytest.mark.unit
def test_trims_to_count_dedupes_and_drops_overlong():
    raw = '["A?", "a?", "B?", "' + ("x" * 100) + '", "C?", "D?"]'
    # "a?" dedupes "A?" (casefold); the 100-char entry is dropped; capped at 2
    assert _parse_followups(raw, 2) == ["A?", "B?"]


@pytest.mark.unit
def test_empty_or_garbage_yields_empty():
    assert _parse_followups("", 3) == []
    assert _parse_followups("   ", 3) == []
    assert _parse_followups("{}", 3) == []  # object, not array, no lines


# ---- generate_followups (best-effort) ----------------------------------------

@pytest.mark.unit
async def test_generate_returns_parsed_chips():
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value={"message": {"content": '["X?", "Y?"]'}})
    with patch("utils.llm_client.get_default_client", return_value=mock_client), \
         patch("utils.llm_client.extract_response_content", return_value='["X?", "Y?"]'):
        out = await generate_followups("q", "an answer long enough", lang="de", model="m", count=3)
    assert out == ["X?", "Y?"]


@pytest.mark.unit
async def test_generate_empty_answer_short_circuits():
    # No client call when there's no answer to follow up on.
    with patch("utils.llm_client.get_default_client", side_effect=AssertionError("should not be called")):
        assert await generate_followups("q", "   ", model="m") == []


@pytest.mark.unit
async def test_generate_is_best_effort_on_failure():
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=RuntimeError("ollama down"))
    with patch("utils.llm_client.get_default_client", return_value=mock_client), \
         patch("utils.llm_client.extract_response_content", return_value=""):
        assert await generate_followups("q", "an answer", model="m") == []
