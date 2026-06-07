"""Unit tests for the document-title synthesizer (Wissen/Dokumente display name).

generate_document_title turns a document's Schicht A facts into a short human
title via the LLM. The LLM client is injected (no network); settings model is
monkeypatched so the function gets past its no-model guard.
"""
from types import SimpleNamespace

import pytest

from services.schicht_a_extractor import (
    ExtractedFact,
    _facts_to_block,
    generate_document_title,
)
from utils.config import settings

pytestmark = pytest.mark.asyncio


class _FakeClient:
    """Returns a fixed chat response shaped like ollama's (message.content)."""

    def __init__(self, content: str):
        self._content = content
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(message=SimpleNamespace(content=self._content, thinking=None))


@pytest.fixture(autouse=True)
def _model(monkeypatch):
    monkeypatch.setattr(settings, "schicht_a_extraction_model", "test-model", raising=False)
    monkeypatch.setattr(settings, "ollama_chat_model", "test-model", raising=False)


def _f(category, kind, value, **kw):
    return ExtractedFact(category=category, kind=kind, value=value, **kw)


def test_facts_to_block_includes_kind_value_date_amount():
    from decimal import Decimal
    from datetime import date
    facts = [
        _f("universal", "aussteller", "Allianz Versicherungs-AG"),
        _f("obligation", "abgabefrist", "innerhalb von sechs Wochen", obligation_date=date(2026, 5, 15)),
        _f("universal", "hauptforderung", "119,00", amount_value=Decimal("119.00"), amount_currency="EUR"),
    ]
    block = _facts_to_block(facts)
    assert "aussteller: Allianz Versicherungs-AG" in block
    assert "Frist 2026-05-15" in block
    assert "Betrag 119.00 EUR" in block


async def test_generates_title_from_facts():
    client = _FakeClient('{"title": "Mahnung BFS health finance (Nr. 70660643)"}')
    facts = [_f("identifier", "kontoinhaber", "BFS health finance GmbH"),
             _f("identifier", "mahnnummer", "70660643")]
    title = await generate_document_title(facts, lang="de", llm_client=client)
    assert title == "Mahnung BFS health finance (Nr. 70660643)"
    assert len(client.calls) == 1  # one LLM call


async def test_facts_actually_reach_the_user_prompt():
    # Regression for the prompt_manager-default bug: {facts} must be substituted,
    # not passed literally — else the LLM gets no facts.
    client = _FakeClient('{"title": "x"}')
    facts = [_f("identifier", "kontoinhaber", "BFS health finance GmbH"),
             _f("identifier", "mahnnummer", "70660643")]
    await generate_document_title(facts, lang="de", llm_client=client)
    user_msg = next(m["content"] for m in client.calls[0]["messages"] if m["role"] == "user")
    assert "BFS health finance GmbH" in user_msg
    assert "70660643" in user_msg
    assert "{facts}" not in user_msg  # placeholder was substituted, not left literal


async def test_empty_facts_returns_none_without_llm():
    client = _FakeClient('{"title": "should not be used"}')
    assert await generate_document_title([], llm_client=client) is None
    assert client.calls == []  # no LLM call for an empty doc


async def test_unparseable_response_returns_none():
    client = _FakeClient("not json at all")
    title = await generate_document_title([_f("universal", "aussteller", "X")], llm_client=client)
    assert title is None


async def test_title_is_trimmed_and_dequoted():
    client = _FakeClient('{"title": "  \\"Rechnung   Stadtwerke\\"  "}')
    title = await generate_document_title([_f("universal", "aussteller", "Stadtwerke")], llm_client=client)
    assert title == "Rechnung Stadtwerke"  # quotes stripped, whitespace collapsed


async def test_no_model_configured_returns_none(monkeypatch):
    monkeypatch.setattr(settings, "schicht_a_extraction_model", "", raising=False)
    monkeypatch.setattr(settings, "ollama_chat_model", "", raising=False)
    monkeypatch.setattr(settings, "ollama_model", "", raising=False)
    client = _FakeClient('{"title": "x"}')
    assert await generate_document_title([_f("universal", "aussteller", "X")], llm_client=client) is None
    assert client.calls == []


async def test_llm_exception_returns_none():
    class _Boom:
        async def chat(self, **kw):
            raise RuntimeError("model down")
    title = await generate_document_title([_f("universal", "aussteller", "X")], llm_client=_Boom())
    assert title is None


def test_doc_response_display_name_precedence():
    # display_name = generated_title → title → filename
    from api.routes.knowledge import _doc_to_response_kwargs
    from models.database import Document

    d = Document(filename="2026_05_23_10_55_18.pdf")
    assert _doc_to_response_kwargs(d)["display_name"] == "2026_05_23_10_55_18.pdf"
    d.title = "Microsoft Word - Doc1"
    assert _doc_to_response_kwargs(d)["display_name"] == "Microsoft Word - Doc1"
    d.generated_title = "Mahnung BFS health finance"
    assert _doc_to_response_kwargs(d)["display_name"] == "Mahnung BFS health finance"
