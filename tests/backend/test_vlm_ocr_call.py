"""The VLM re-OCR call (OllamaService.extract_text_from_image) is deterministic.

A transcription has one right answer, so the call pins temperature 0 instead of
inheriting the server default (the 5090 llama-server runs --temp 0.6). Both
adapters honour it: Ollama reads ``options`` natively, the OpenAI-compat adapter
maps ``options.temperature`` to ``temperature``.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.ollama_service import OllamaService
from utils import llm_client
from utils.circuit_breaker import llm_circuit_breaker
from utils.config import settings


@pytest.fixture(autouse=True)
def _reset_breaker():
    llm_circuit_breaker.reset()
    yield
    llm_circuit_breaker.reset()


def _vision_client(content: str = "Rechnung Nr. 42"):
    return SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(message=SimpleNamespace(content=content))))


@pytest.mark.unit
async def test_ocr_call_pins_temperature_zero(monkeypatch):
    monkeypatch.setattr(settings, "ollama_vision_model", "qwen3.6")
    client = _vision_client()
    monkeypatch.setattr(llm_client, "get_vision_client", lambda: (client, "qwen3.6"))
    svc = OllamaService.__new__(OllamaService)

    text = await svc.extract_text_from_image("aGVsbG8=")

    assert text == "Rechnung Nr. 42"
    options = client.chat.await_args.kwargs["options"]
    assert options["temperature"] == 0
    assert options["num_ctx"] == settings.ollama_num_ctx


@pytest.mark.unit
def test_openai_adapter_forwards_the_zero_temperature():
    oa = llm_client.OpenAICompatibleClient._options_to_openai({"num_ctx": 8192, "temperature": 0}, {})
    assert oa["temperature"] == 0


@pytest.mark.unit
async def test_no_vision_model_means_no_call(monkeypatch):
    monkeypatch.setattr(settings, "ollama_vision_model", "")
    client = _vision_client()
    monkeypatch.setattr(llm_client, "get_vision_client", lambda: (client, ""))
    svc = OllamaService.__new__(OllamaService)

    assert await svc.extract_text_from_image("aGVsbG8=") is None
    client.chat.assert_not_awaited()
