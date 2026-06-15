"""
Tests for `api.websocket.chat_handler._extract_agent_sources`.

Provenance "source chips": after a turn, the chat handler collects the KB
documents the answer drew on from this turn's `knowledge_search` tool results,
deduped by document_id, and attaches them to the assistant message
(`message_metadata.sources`) + the `done` stream frame.
"""

import pytest

from api.websocket.chat_handler import _extract_agent_sources


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
    ]
    assert _extract_agent_sources(tool_results) == []


@pytest.mark.unit
def test_empty_and_malformed_yield_no_sources():
    assert _extract_agent_sources([]) == []
    # knowledge_search with no sources key, or a missing document_id
    assert _extract_agent_sources([("internal.knowledge_search", {"data": {}})]) == []
    assert _extract_agent_sources([("internal.knowledge_search", _ks([{"filename": "x.pdf"}]))]) == []
    assert _extract_agent_sources([("internal.knowledge_search", None)]) == []
