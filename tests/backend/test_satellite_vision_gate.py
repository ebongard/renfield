"""
A satellite's camera snapshot is used only for a question that wants to see.

A camera satellite photographs the room on every wake word, and the handler
answered every turn that carried an image with the vision model. A dark room
(2026-11-06, Esszimmer, 17:12) turned an unrelated utterance into "I can't see
the image, it is completely black". With SATELLITE_VISION_QUERY_GATE_ENABLED
the intent model first decides whether the utterance wants to see something;
anything but a clear yes answers without the image.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_glue.api.websocket import satellite_handler
from ha_glue.api.websocket.satellite_handler import should_use_vision
from services.ollama_service import OllamaService

IMAGE = "aGVsbG8="


def _ollama(verdict):
    return SimpleNamespace(is_visual_query=AsyncMock(return_value=verdict))


@pytest.fixture
def vision_on(monkeypatch):
    monkeypatch.setattr(satellite_handler.settings, "ollama_vision_model", "vision-model")


def _gate(monkeypatch, enabled: bool):
    monkeypatch.setattr(satellite_handler.ha_glue_settings, "satellite_vision_query_gate_enabled", enabled)


# --- the decision in the voice turn -----------------------------------------


@pytest.mark.unit
async def test_gate_off_keeps_the_legacy_behaviour(monkeypatch, vision_on):
    """Flag off: any image goes to the vision model, the classifier is never asked."""
    _gate(monkeypatch, False)
    ollama = _ollama(False)

    assert await should_use_vision(IMAGE, "Wie spät ist es?", "de", ollama) is True
    ollama.is_visual_query.assert_not_awaited()


@pytest.mark.unit
async def test_a_visual_question_uses_the_snapshot(monkeypatch, vision_on):
    _gate(monkeypatch, True)
    ollama = _ollama(True)

    assert await should_use_vision(IMAGE, "Was siehst du?", "de", ollama) is True
    ollama.is_visual_query.assert_awaited_once_with("Was siehst du?", lang="de")


@pytest.mark.unit
async def test_an_unrelated_question_is_answered_without_the_snapshot(monkeypatch, vision_on):
    _gate(monkeypatch, True)

    assert await should_use_vision(IMAGE, "Wie spät ist es?", "de", _ollama(False)) is False


@pytest.mark.unit
async def test_no_verdict_means_no_snapshot(monkeypatch, vision_on):
    """A failed check must not fall back to the vision model."""
    _gate(monkeypatch, True)

    assert await should_use_vision(IMAGE, "Wie spät ist es?", "de", _ollama(None)) is False


@pytest.mark.unit
@pytest.mark.parametrize("image, vision_model", [(None, "vision-model"), ("", "vision-model"), (IMAGE, "")])
async def test_without_image_or_vision_model_nothing_is_asked(monkeypatch, image, vision_model):
    _gate(monkeypatch, True)
    monkeypatch.setattr(satellite_handler.settings, "ollama_vision_model", vision_model)
    ollama = _ollama(True)

    assert await should_use_vision(image, "Was siehst du?", "de", ollama) is False
    ollama.is_visual_query.assert_not_awaited()


# --- the classifier ----------------------------------------------------------


def _service():
    return object.__new__(OllamaService)


def _reply(content: str):
    return SimpleNamespace(message=SimpleNamespace(content=content, thinking=None))


async def _classify(content=None, *, raises=None, allow=True, text="Was siehst du?", lang="de"):
    client = MagicMock()
    client.chat = AsyncMock(side_effect=raises) if raises else AsyncMock(return_value=_reply(content))
    breaker = AsyncMock()
    breaker.allow_request.return_value = allow
    with patch("services.ollama_service.llm_circuit_breaker", breaker), \
         patch("utils.llm_client.get_intent_client", return_value=client):
        verdict = await _service().is_visual_query(text, lang=lang)
    return verdict, client


@pytest.mark.unit
@pytest.mark.parametrize("content, expected", [
    ("JA", True), ("Ja.", True), ("YES", True), ("<think>hm</think>\nJA", True),
    ("NEIN", False), ("Nein, das braucht kein Bild.", False), ("NO", False),
    ("vielleicht", None), ("", None),
])
async def test_the_answer_is_read_from_its_first_word(content, expected):
    verdict, _ = await _classify(content)
    assert verdict is expected


@pytest.mark.unit
async def test_the_prompt_carries_the_utterance_in_the_turns_language():
    _, client = await _classify("NEIN", text="What time is it?", lang="en")

    prompt = client.chat.await_args.kwargs["messages"][0]["content"]
    assert '"What time is it?"' in prompt
    assert "Answer with exactly one word: YES or NO." in prompt


@pytest.mark.unit
async def test_a_failing_call_gives_no_verdict():
    verdict, _ = await _classify(raises=RuntimeError("endpoint down"))
    assert verdict is None


@pytest.mark.unit
async def test_an_open_breaker_or_empty_utterance_skips_the_call():
    verdict, client = await _classify("JA", allow=False)
    assert verdict is None
    client.chat.assert_not_awaited()

    verdict, client = await _classify("JA", text="   ")
    assert verdict is None
    client.chat.assert_not_awaited()
