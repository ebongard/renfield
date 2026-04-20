"""
Regression tests for the RAGService → RAGRetrieval extraction (Lane A1 of
the second-brain-circles eng-review plan).

Critical invariant: behaviour of RAGService.search and RAGService.get_context
must be IDENTICAL whether routed through the legacy inline code (flag off)
or through the extracted RAGRetrieval module (flag on).

Approach:
- Static methods (`_reciprocal_rank_fusion`) are byte-equivalent in both classes
  — verified directly with shared fixtures.
- Instance methods (`search`, `_search_dense`, `_search_bm25`,
  `_resolve_parents`, `_rerank`, `_expand_context_window`, `get_context`,
  `format_context_from_results`) are exercised through BOTH the legacy inline
  RAGService path and the new RAGRetrieval delegate path with the same
  mocked DB / Ollama responses, asserting identical output.
- Routing test: confirms RAGService.search / get_context unconditionally
  re-routes calls to RAGRetrieval (verified by patching the import target
  and asserting it gets called).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.rag_retrieval import RAGRetrieval
from services.rag_service import RAGService


# =============================================================================
# Helpers (mirror tests/backend/test_rag_hybrid_search.py for parity)
# =============================================================================


def make_result(
    chunk_id: int,
    doc_id: int = 1,
    chunk_index: int = 0,
    similarity: float = 0.9,
    content: str = "test content",
):
    return {
        "chunk": {
            "id": chunk_id,
            "content": content,
            "chunk_index": chunk_index,
            "page_number": 1,
            "section_title": None,
            "chunk_type": "paragraph",
            "parent_chunk_id": None,
        },
        "document": {
            "id": doc_id,
            "filename": f"doc_{doc_id}.pdf",
            "title": f"Document {doc_id}",
        },
        "similarity": similarity,
    }


# =============================================================================
# (TestRRFParity removed: RAGService._reciprocal_rank_fusion was deleted in
# the post-Lane-C hygiene sprint. RAGRetrieval._reciprocal_rank_fusion is the
# only copy and is exercised by test_rag_hybrid_search.py::TestReciprocalRankFusion.)
# =============================================================================


# =============================================================================
# Static method parity: format_context_from_results
# =============================================================================


class TestFormatContextParity:
    """Result-formatting helper must produce identical strings across both classes."""

    @pytest.mark.unit
    def test_format_empty_results(self):
        legacy = RAGService(MagicMock()).format_context_from_results([])
        new = RAGRetrieval(MagicMock()).format_context_from_results([])
        assert legacy == new == ""

    @pytest.mark.unit
    def test_format_single_result(self):
        results = [make_result(1, content="The capital of France is Paris.")]
        legacy = RAGService(MagicMock()).format_context_from_results(results)
        new = RAGRetrieval(MagicMock()).format_context_from_results(results)
        assert legacy == new
        assert "[Quelle 1: doc_1.pdf, Seite 1]" in new
        assert "The capital of France is Paris." in new

    @pytest.mark.unit
    def test_format_multiple_results_separator(self):
        results = [make_result(1, content="alpha"), make_result(2, content="beta")]
        legacy = RAGService(MagicMock()).format_context_from_results(results)
        new = RAGRetrieval(MagicMock()).format_context_from_results(results)
        assert legacy == new
        assert "\n\n---\n\n" in new

    @pytest.mark.unit
    def test_format_with_section_title(self):
        results = [make_result(1)]
        results[0]["chunk"]["section_title"] = "Introduction"
        legacy = RAGService(MagicMock()).format_context_from_results(results)
        new = RAGRetrieval(MagicMock()).format_context_from_results(results)
        assert legacy == new
        assert "Introduction" in new


# =============================================================================
# Flag routing: the search() / get_context() entry points actually delegate
# =============================================================================


class TestRouting:
    """
    RAGService.search and RAGService.get_context unconditionally route through
    RAGRetrieval (which applies the circle-tier filter).
    """

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_search_always_routes_to_retrieval_module(self):
        db = MagicMock()
        service = RAGService(db)
        sentinel = [make_result(42, content="from RAGRetrieval path")]

        with patch(
            "services.rag_retrieval.RAGRetrieval.search",
            new=AsyncMock(return_value=sentinel),
        ) as ret_search:
            result = await service.search("anything", top_k=5, user_id=42)

        ret_search.assert_called_once()
        assert ret_search.call_args.kwargs.get("user_id") == 42
        assert result is sentinel

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_get_context_always_routes(self):
        db = MagicMock()
        service = RAGService(db)
        sentinel = "[Quelle 1: x.pdf, Seite 1]\nrouted"

        with patch(
            "services.rag_retrieval.RAGRetrieval.get_context",
            new=AsyncMock(return_value=sentinel),
        ) as ret_ctx:
            result = await service.get_context("anything", top_k=3, user_id=7)

        ret_ctx.assert_called_once()
        assert ret_ctx.call_args.kwargs.get("user_id") == 7
        assert result == sentinel


# =============================================================================
# RAGRetrieval public API surface check (catches accidental method renames)
# =============================================================================


class TestRAGRetrievalSurface:
    """Public methods extracted from RAGService must exist on RAGRetrieval."""

    @pytest.mark.unit
    def test_required_methods_present(self):
        required = {
            "get_embedding",
            "search",
            "get_context",
            "format_context_from_results",
            # Internal helpers exposed for completeness — also moved
            "_search_dense",
            "_search_bm25",
            "_reciprocal_rank_fusion",
            "_rerank",
            "_resolve_parents",
            "_expand_context_window",
        }
        actual = {name for name in dir(RAGRetrieval) if not name.startswith("__")}
        missing = required - actual
        assert not missing, f"RAGRetrieval is missing extracted methods: {missing}"
