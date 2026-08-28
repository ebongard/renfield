"""Unit tests for the Simba category/type classifier (content → taxonomy)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services import simba_classify

pytestmark = pytest.mark.unit

CATS = {"Belege": ["Ausgangsrechnung", "Eingangsrechnung"], "Posteingang": ["Schriftverkehr"]}


def _client():
    c = MagicMock()
    c.chat = AsyncMock(return_value="ignored")  # extract_response_content is patched
    return c


async def _classify(text, cats, llm_json):
    with patch("services.simba_classify.extract_response_content", return_value=llm_json), patch(
        "services.simba_classify.get_classification_chat_kwargs", return_value={}
    ), patch("services.simba_classify.settings") as ms:
        ms.ollama_chat_model = "m"
        ms.ollama_model = "m"
        return await simba_classify.classify_simba(text, cats, llm_client=_client())


@pytest.mark.asyncio
async def test_valid_pair():
    assert await _classify("Schreiben vom Finanzamt", CATS,
                           '{"category":"Posteingang","type":"Schriftverkehr"}') == ("Posteingang", "Schriftverkehr")


@pytest.mark.asyncio
async def test_case_insensitive_match():
    assert await _classify("x", CATS, '{"category":"belege","type":"ausgangsrechnung"}') == ("Belege", "Ausgangsrechnung")


@pytest.mark.asyncio
async def test_invalid_category_returns_none():
    assert await _classify("x", CATS, '{"category":"Unbekannt","type":"Foo"}') == (None, None)


@pytest.mark.asyncio
async def test_valid_category_but_invalid_type_keeps_category():
    # A plausible category with an unknown type still prefills the category.
    assert await _classify("x", CATS, '{"category":"Belege","type":"Nichtvorhanden"}') == ("Belege", None)


@pytest.mark.asyncio
async def test_fenced_json_is_parsed():
    assert await _classify("x", CATS, '```json\n{"category":"Belege","type":"Eingangsrechnung"}\n```') == (
        "Belege", "Eingangsrechnung",
    )


@pytest.mark.asyncio
async def test_garbage_output_returns_none():
    assert await _classify("x", CATS, 'not json at all') == (None, None)


@pytest.mark.asyncio
async def test_empty_text_returns_none_without_calling_llm():
    assert await simba_classify.classify_simba("", CATS) == (None, None)
    assert await simba_classify.classify_simba("text", {}) == (None, None)
