"""
Tests for `api.websocket.chat_handler._extract_agent_sources`.

Provenance "source chips": after a turn, the chat handler collects the KB
documents the answer drew on from this turn's `knowledge_search` tool results,
deduped by document_id, and attaches them to the assistant message
(`message_metadata.sources`) + the `done` stream frame.

`_extract_agent_sources` is the whole story while `CHAT_DOCUMENT_LINKS_ENABLED`
is off. With the flag on, `_turn_sources` adds Paperless documents, the direct
RAG path and open targets (`test_chat_document_links.py`).
"""

import json

import pytest

from api.websocket.chat_handler import _extract_agent_sources, _turn_sources
from utils.config import settings


def _paperless_search(*doc_ids):
    """A `mcp.paperless.search_documents` result as the handler receives it:
    MCP content blocks whose text is the tool's JSON."""
    payload = {"summary": {}, "results": [{"id": i, "title": f"Doc {i}"} for i in doc_ids]}
    return ("mcp.paperless.search_documents", [{"type": "text", "text": json.dumps(payload)}])


def _ks(sources):
    """The knowledge_search `step.data` as chat_handler actually receives it.

    chat_handler appends `(step.tool, step.data)` and agent_service sets
    `AgentStep.data = result["data"]` — i.e. the tool's INNER result dict. So
    `sources` sits at the top level here, NOT nested under another "data" key.
    (Encoding the wrong outer-envelope shape here once let a dead-on-arrival
    extractor bug pass review — the fixture MUST mirror the runtime payload.)
    """
    return {"results_count": len(sources), "sources": sources}


@pytest.mark.unit
def test_collects_and_dedupes_knowledge_search_sources():
    tool_results = [
        ("internal.knowledge_search", _ks([
            {"document_id": 7, "filename": "a.pdf", "title": "A", "tier": 2},
            {"document_id": 9, "filename": "b.pdf", "title": "B", "tier": 0},
        ])),
        # a second knowledge_search in the same turn repeating doc 7 → deduped
        ("internal.knowledge_search", _ks([
            {"document_id": 7, "filename": "a.pdf", "title": "A", "tier": 2},
            {"document_id": 12, "filename": "c.pdf", "title": "C", "tier": 4},
        ])),
    ]
    out = _extract_agent_sources(tool_results)
    assert [s["document_id"] for s in out] == [7, 9, 12]


@pytest.mark.unit
def test_ignores_non_knowledge_search_tools():
    tool_results = [
        ("internal.play_in_room", {"success": True, "data": {"sources": [{"document_id": 1}]}}),
        ("mcp.paperless.search_documents", {"data": {"sources": [{"document_id": 2}]}}),
        _paperless_search(2, 3),
    ]
    assert _extract_agent_sources(tool_results) == []


@pytest.mark.unit
async def test_flag_off_a_paperless_search_is_still_no_source(monkeypatch):
    """The intent before chat document links, kept for the dark default."""
    monkeypatch.setattr(settings, "chat_document_links_enabled", False, raising=False)
    monkeypatch.setattr(settings, "paperless_public_url", "https://paperless.example.org",
                        raising=False)
    assert await _turn_sources([_paperless_search(2, 3)], [], 1) == []


@pytest.mark.unit
async def test_flag_on_a_paperless_search_is_a_source(monkeypatch):
    """The intent CHANGED on purpose: a document the agent found in Paperless
    is one the chat must let the user open. Only the real result shape counts
    -- a `sources` key some tool invented is still nothing, and neither is a
    tool that is not one of the two Paperless reads."""
    monkeypatch.setattr(settings, "chat_document_links_enabled", True, raising=False)
    monkeypatch.setattr(settings, "paperless_public_url", "https://paperless.example.org",
                        raising=False)
    tool_results = [
        ("internal.play_in_room", {"success": True, "data": {"sources": [{"document_id": 1}]}}),
        ("mcp.paperless.search_documents", {"data": {"sources": [{"document_id": 2}]}}),
        _paperless_search(2, 3),
    ]

    sources = await _turn_sources(tool_results, [], 1)

    assert sources == [
        {"kind": "paperless", "paperless_document_id": 2, "title": "Doc 2",
         "paperless_url": "https://paperless.example.org/documents/2/details"},
        {"kind": "paperless", "paperless_document_id": 3, "title": "Doc 3",
         "paperless_url": "https://paperless.example.org/documents/3/details"},
    ]


@pytest.mark.unit
def test_empty_and_malformed_yield_no_sources():
    assert _extract_agent_sources([]) == []
    # knowledge_search with no sources key, or a missing document_id
    assert _extract_agent_sources([("internal.knowledge_search", {"data": {}})]) == []
    assert _extract_agent_sources([("internal.knowledge_search", _ks([{"filename": "x.pdf"}]))]) == []
    assert _extract_agent_sources([("internal.knowledge_search", None)]) == []
