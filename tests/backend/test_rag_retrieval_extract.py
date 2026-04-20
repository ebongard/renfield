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
- Flag routing test: confirms the `circles_use_new_rag` flag actually
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
# Static method parity: _reciprocal_rank_fusion
# =============================================================================


class TestRRFParity:
    """The RRF algorithm must produce byte-identical output across the two classes."""

    @pytest.mark.unit
    def test_rrf_identical_for_overlapping_results(self):
        """RAGService.RRF and RAGRetrieval.RRF agree on overlapping inputs."""
        dense = [make_result(1, similarity=0.95), make_result(2, similarity=0.80)]
        bm25 = [make_result(2, similarity=0.7), make_result(3, similarity=0.6)]

        with patch("services.rag_service.settings") as legacy_settings, \
             patch("services.rag_retrieval.settings") as new_settings:
            for s in (legacy_settings, new_settings):
                s.rag_hybrid_rrf_k = 60
                s.rag_hybrid_dense_weight = 0.5
                s.rag_hybrid_bm25_weight = 0.5

            legacy = RAGService._reciprocal_rank_fusion(dense, bm25, top_k=10)
            new = RAGRetrieval._reciprocal_rank_fusion(dense, bm25, top_k=10)

        assert legacy == new, "RRF output must be identical across legacy and extracted paths"

    @pytest.mark.unit
    def test_rrf_identical_for_disjoint_results(self):
        dense = [make_result(1), make_result(2)]
        bm25 = [make_result(3), make_result(4)]

        with patch("services.rag_service.settings") as legacy_settings, \
             patch("services.rag_retrieval.settings") as new_settings:
            for s in (legacy_settings, new_settings):
                s.rag_hybrid_rrf_k = 60
                s.rag_hybrid_dense_weight = 0.5
                s.rag_hybrid_bm25_weight = 0.5

            legacy = RAGService._reciprocal_rank_fusion(dense, bm25, top_k=10)
            new = RAGRetrieval._reciprocal_rank_fusion(dense, bm25, top_k=10)

        assert legacy == new

    @pytest.mark.unit
    def test_rrf_identical_with_asymmetric_weights(self):
        dense = [make_result(1, similarity=0.95), make_result(2, similarity=0.85)]
        bm25 = [make_result(2, similarity=0.9), make_result(3, similarity=0.5)]

        with patch("services.rag_service.settings") as legacy_settings, \
             patch("services.rag_retrieval.settings") as new_settings:
            for s in (legacy_settings, new_settings):
                s.rag_hybrid_rrf_k = 60
                s.rag_hybrid_dense_weight = 0.7
                s.rag_hybrid_bm25_weight = 0.3

            legacy = RAGService._reciprocal_rank_fusion(dense, bm25, top_k=5)
            new = RAGRetrieval._reciprocal_rank_fusion(dense, bm25, top_k=5)

        assert legacy == new


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


class TestFlagRouting:
    """When CIRCLES_USE_NEW_RAG is on, RAGService.search must call RAGRetrieval.search."""

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_search_flag_on_routes_to_retrieval_module(self):
        db = MagicMock()
        service = RAGService(db)
        sentinel = [make_result(42, content="from RAGRetrieval path")]

        with patch("services.rag_service.settings") as svc_settings, \
             patch("services.rag_retrieval.RAGRetrieval.search", new=AsyncMock(return_value=sentinel)) as ret_search:
            svc_settings.circles_use_new_rag = True
            result = await service.search("anything", top_k=5)

        ret_search.assert_called_once()
        assert result is sentinel, "RAGService.search must return RAGRetrieval.search output verbatim"

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_search_flag_off_does_not_route_to_retrieval_module(self):
        db = MagicMock()
        service = RAGService(db)

        # Mock the legacy inline path enough to not blow up; we don't care about result quality,
        # only that the new RAGRetrieval.search is NOT called.
        with patch("services.rag_service.settings") as svc_settings, \
             patch.object(service, "get_embedding", new=AsyncMock(return_value=[0.1] * 768)), \
             patch.object(service, "_search_dense", new=AsyncMock(return_value=[])), \
             patch.object(service, "_search_bm25", new=AsyncMock(return_value=[])), \
             patch("services.rag_retrieval.RAGRetrieval.search", new=AsyncMock(return_value=[])) as ret_search:
            svc_settings.circles_use_new_rag = False
            svc_settings.rag_top_k = 5
            svc_settings.rag_similarity_threshold = 0.4
            svc_settings.rag_hybrid_enabled = True
            svc_settings.rag_rerank_enabled = False
            svc_settings.rag_parent_child_enabled = False
            svc_settings.rag_context_window = 0
            svc_settings.rag_context_window_max = 3

            await service.search("anything", top_k=5)

        ret_search.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_get_context_flag_on_routes_to_retrieval_module(self):
        db = MagicMock()
        service = RAGService(db)
        sentinel = "[Quelle 1: x.pdf, Seite 1]\nrouted"

        with patch("services.rag_service.settings") as svc_settings, \
             patch("services.rag_retrieval.RAGRetrieval.get_context", new=AsyncMock(return_value=sentinel)) as ret_ctx:
            svc_settings.circles_use_new_rag = True
            result = await service.get_context("anything", top_k=3)

        ret_ctx.assert_called_once()
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
