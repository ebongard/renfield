"""
Tests for Hybrid Search (Dense + BM25 via RRF) and Context Window Retrieval.

RRF and Context Window logic is tested as unit tests.
BM25 and Hybrid integration require PostgreSQL (tsvector), so they use mocks.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.rag_service import RAGService
from utils.config import Settings

# =============================================================================
# Helper: Create mock search results
# =============================================================================

def make_result(chunk_id: int, doc_id: int = 1, chunk_index: int = 0,
                similarity: float = 0.9, content: str = "test content"):
    return {
        "chunk": {
            "id": chunk_id,
            "content": content,
            "chunk_index": chunk_index,
            "page_number": 1,
            "section_title": None,
            "chunk_type": "paragraph",
        },
        "document": {
            "id": doc_id,
            "filename": f"doc_{doc_id}.pdf",
            "title": f"Document {doc_id}"
        },
        "similarity": similarity
    }


# =============================================================================
# RRF Unit Tests
# =============================================================================

class TestReciprocalRankFusion:
    """Tests for the static _reciprocal_rank_fusion method."""

    @pytest.mark.unit
    def test_rrf_basic_fusion(self):
        """Two retrievers return overlapping results, RRF merges them."""
        dense = [make_result(1, similarity=0.95), make_result(2, similarity=0.8)]
        bm25 = [make_result(2, similarity=0.1), make_result(3, similarity=0.05)]

        with patch("services.rag_service.settings") as mock_settings:
            mock_settings.rag_hybrid_rrf_k = 60
            mock_settings.rag_hybrid_dense_weight = 0.7
            mock_settings.rag_hybrid_bm25_weight = 0.3

            results = RAGService._reciprocal_rank_fusion(dense, bm25, top_k=5)

        assert len(results) == 3
        ids = [r["chunk"]["id"] for r in results]
        # Chunk 2 appears in both → highest fused score
        assert ids[0] == 2

    @pytest.mark.unit
    def test_rrf_empty_dense(self):
        """BM25-only results when dense returns nothing."""
        dense = []
        bm25 = [make_result(1), make_result(2)]

        with patch("services.rag_service.settings") as mock_settings:
            mock_settings.rag_hybrid_rrf_k = 60
            mock_settings.rag_hybrid_dense_weight = 0.7
            mock_settings.rag_hybrid_bm25_weight = 0.3

            results = RAGService._reciprocal_rank_fusion(dense, bm25, top_k=5)

        assert len(results) == 2

    @pytest.mark.unit
    def test_rrf_empty_bm25(self):
        """Dense-only results when BM25 returns nothing."""
        dense = [make_result(1), make_result(2)]
        bm25 = []

        with patch("services.rag_service.settings") as mock_settings:
            mock_settings.rag_hybrid_rrf_k = 60
            mock_settings.rag_hybrid_dense_weight = 0.7
            mock_settings.rag_hybrid_bm25_weight = 0.3

            results = RAGService._reciprocal_rank_fusion(dense, bm25, top_k=5)

        assert len(results) == 2

    @pytest.mark.unit
    def test_rrf_both_empty(self):
        """Returns empty when both retrievers return nothing."""
        with patch("services.rag_service.settings") as mock_settings:
            mock_settings.rag_hybrid_rrf_k = 60
            mock_settings.rag_hybrid_dense_weight = 0.7
            mock_settings.rag_hybrid_bm25_weight = 0.3

            results = RAGService._reciprocal_rank_fusion([], [], top_k=5)

        assert results == []

    @pytest.mark.unit
    def test_rrf_top_k_limit(self):
        """RRF respects top_k limit."""
        dense = [make_result(i) for i in range(10)]
        bm25 = [make_result(i + 10) for i in range(10)]

        with patch("services.rag_service.settings") as mock_settings:
            mock_settings.rag_hybrid_rrf_k = 60
            mock_settings.rag_hybrid_dense_weight = 0.7
            mock_settings.rag_hybrid_bm25_weight = 0.3

            results = RAGService._reciprocal_rank_fusion(dense, bm25, top_k=5)

        assert len(results) == 5

    @pytest.mark.unit
    def test_rrf_dedup(self):
        """Same chunk in both retrievers appears only once."""
        dense = [make_result(1), make_result(2)]
        bm25 = [make_result(1), make_result(3)]

        with patch("services.rag_service.settings") as mock_settings:
            mock_settings.rag_hybrid_rrf_k = 60
            mock_settings.rag_hybrid_dense_weight = 0.7
            mock_settings.rag_hybrid_bm25_weight = 0.3

            results = RAGService._reciprocal_rank_fusion(dense, bm25, top_k=10)

        ids = [r["chunk"]["id"] for r in results]
        assert len(ids) == len(set(ids))  # No duplicates
        assert 1 in ids and 2 in ids and 3 in ids

    @pytest.mark.unit
    def test_rrf_weight_influence(self):
        """Higher dense weight means dense-only chunks rank above bm25-only."""
        # Chunk 1 only in dense (rank 0), Chunk 2 only in bm25 (rank 0)
        dense = [make_result(1)]
        bm25 = [make_result(2)]

        with patch("services.rag_service.settings") as mock_settings:
            mock_settings.rag_hybrid_rrf_k = 60
            mock_settings.rag_hybrid_dense_weight = 0.9
            mock_settings.rag_hybrid_bm25_weight = 0.1

            results = RAGService._reciprocal_rank_fusion(dense, bm25, top_k=5)

        # Dense has higher weight, so chunk 1 should rank first
        assert results[0]["chunk"]["id"] == 1

    @pytest.mark.unit
    def test_rrf_scores_are_positive(self):
        """All RRF scores should be positive floats."""
        dense = [make_result(1), make_result(2)]
        bm25 = [make_result(3), make_result(1)]

        with patch("services.rag_service.settings") as mock_settings:
            mock_settings.rag_hybrid_rrf_k = 60
            mock_settings.rag_hybrid_dense_weight = 0.7
            mock_settings.rag_hybrid_bm25_weight = 0.3

            results = RAGService._reciprocal_rank_fusion(dense, bm25, top_k=5)

        for r in results:
            assert r["similarity"] > 0


# =============================================================================
# Context Window Tests
# =============================================================================

class TestContextWindow:
    """Tests for _expand_context_window method."""

    @pytest.fixture
    def rag_service(self):
        db = AsyncMock()
        return RAGService(db)

    def _make_adj_row(self, chunk_id, content, chunk_index, page_number=1, document_id=1):
        return SimpleNamespace(
            id=chunk_id,
            content=content,
            chunk_index=chunk_index,
            page_number=page_number,
            section_title=None,
            chunk_type="paragraph",
            document_id=document_id,
        )

    @pytest.mark.unit
    async def test_context_window_zero(self, rag_service):
        """With window_size=0, returns same chunk (no real expansion)."""
        results = [make_result(1, doc_id=1, chunk_index=5, content="center")]

        mock_result = MagicMock()
        mock_result.fetchall.return_value = [
            self._make_adj_row(1, "center", 5),
        ]
        rag_service.db.execute = AsyncMock(return_value=mock_result)

        expanded = await rag_service._expand_context_window(results, window_size=0)
        assert len(expanded) == 1
        assert expanded[0]["chunk"]["content"] == "center"

    @pytest.mark.unit
    async def test_context_window_expansion(self, rag_service):
        """Expands a single hit with adjacent chunks."""
        results = [make_result(10, doc_id=1, chunk_index=5, content="center")]

        # Mock DB to return adjacent chunks (4, 5, 6)
        mock_result = MagicMock()
        mock_result.fetchall.return_value = [
            self._make_adj_row(9, "before", 4),
            self._make_adj_row(10, "center", 5),
            self._make_adj_row(11, "after", 6),
        ]
        rag_service.db.execute = AsyncMock(return_value=mock_result)

        expanded = await rag_service._expand_context_window(results, window_size=1)

        assert len(expanded) == 1
        content = expanded[0]["chunk"]["content"]
        assert "before" in content
        assert "center" in content
        assert "after" in content

    @pytest.mark.unit
    async def test_context_window_dedup_adjacent_hits(self, rag_service):
        """Adjacent hits don't produce duplicate entries."""
        results = [
            make_result(10, doc_id=1, chunk_index=5, content="chunk5"),
            make_result(11, doc_id=1, chunk_index=6, content="chunk6"),
        ]

        # Single batch query returns all adjacent chunks for both results
        mock_result = MagicMock()
        mock_result.fetchall.return_value = [
            self._make_adj_row(9, "chunk4", 4),
            self._make_adj_row(10, "chunk5", 5),
            self._make_adj_row(11, "chunk6", 6),
            self._make_adj_row(12, "chunk7", 7),
        ]

        rag_service.db.execute = AsyncMock(return_value=mock_result)

        expanded = await rag_service._expand_context_window(results, window_size=1)

        # Chunk 11 was already seen in first expansion, so only 1 result
        assert len(expanded) == 1

    @pytest.mark.unit
    async def test_context_window_start_of_document(self, rag_service):
        """Chunk at index 0 doesn't try to fetch negative indices."""
        results = [make_result(1, doc_id=1, chunk_index=0, content="first")]

        mock_result = MagicMock()
        mock_result.fetchall.return_value = [
            self._make_adj_row(1, "first", 0),
            self._make_adj_row(2, "second", 1),
        ]
        rag_service.db.execute = AsyncMock(return_value=mock_result)

        expanded = await rag_service._expand_context_window(results, window_size=1)

        assert len(expanded) == 1
        # min_index should be max(0, 0-1) = 0 — batch query uses min_0 param
        call_args = rag_service.db.execute.call_args
        assert call_args[0][1]["min_0"] == 0

    @pytest.mark.unit
    async def test_context_window_empty_results(self, rag_service):
        """Empty results return empty."""
        expanded = await rag_service._expand_context_window([], window_size=1)
        assert expanded == []

    @pytest.mark.unit
    async def test_context_window_different_documents(self, rag_service):
        """Chunks from different documents expand independently."""
        results = [
            make_result(10, doc_id=1, chunk_index=3, content="doc1_chunk3"),
            make_result(20, doc_id=2, chunk_index=5, content="doc2_chunk5"),
        ]

        # Single batch query returns all rows grouped by document_id
        mock_result = MagicMock()
        mock_result.fetchall.return_value = [
            self._make_adj_row(9, "doc1_chunk2", 2, document_id=1),
            self._make_adj_row(10, "doc1_chunk3", 3, document_id=1),
            self._make_adj_row(11, "doc1_chunk4", 4, document_id=1),
            self._make_adj_row(19, "doc2_chunk4", 4, document_id=2),
            self._make_adj_row(20, "doc2_chunk5", 5, document_id=2),
            self._make_adj_row(21, "doc2_chunk6", 6, document_id=2),
        ]

        rag_service.db.execute = AsyncMock(return_value=mock_result)

        expanded = await rag_service._expand_context_window(results, window_size=1)

        assert len(expanded) == 2
        assert "doc1_chunk2" in expanded[0]["chunk"]["content"]
        assert "doc2_chunk4" in expanded[1]["chunk"]["content"]


# =============================================================================
# Hybrid Search Integration Tests (with mocked DB)
# =============================================================================

class TestHybridSearchIntegration:
    """Tests for the search() method with hybrid enabled/disabled."""

    @pytest.fixture
    def rag_service(self):
        db = AsyncMock()
        return RAGService(db)

    @pytest.mark.unit
    async def test_search_hybrid_enabled(self, rag_service):
        """When hybrid is enabled, both dense and BM25 are called."""
        mock_embedding = [0.1] * 768

        with patch.object(rag_service, "get_embedding", return_value=mock_embedding), \
             patch.object(rag_service, "_search_dense", return_value=[make_result(1)]) as mock_dense, \
             patch.object(rag_service, "_search_bm25", return_value=[make_result(2)]) as mock_bm25, \
             patch("services.rag_service.settings") as mock_settings:

            mock_settings.rag_hybrid_enabled = True
            mock_settings.rag_top_k = 5
            mock_settings.rag_similarity_threshold = 0.3
            mock_settings.rag_context_window = 0
            mock_settings.rag_context_window_max = 3
            mock_settings.rag_hybrid_rrf_k = 60
            mock_settings.rag_hybrid_dense_weight = 0.7
            mock_settings.rag_hybrid_bm25_weight = 0.3

            results = await rag_service.search("test query")

        mock_dense.assert_called_once()
        mock_bm25.assert_called_once()
        assert len(results) > 0

    @pytest.mark.unit
    async def test_search_hybrid_disabled(self, rag_service):
        """When hybrid is disabled, only dense search is called."""
        mock_embedding = [0.1] * 768

        with patch.object(rag_service, "get_embedding", return_value=mock_embedding), \
             patch.object(rag_service, "_search_dense", return_value=[make_result(1)]) as mock_dense, \
             patch.object(rag_service, "_search_bm25") as mock_bm25, \
             patch("services.rag_service.settings") as mock_settings:

            mock_settings.rag_hybrid_enabled = False
            mock_settings.rag_top_k = 5
            mock_settings.rag_similarity_threshold = 0.3
            mock_settings.rag_context_window = 0
            mock_settings.rag_context_window_max = 3

            await rag_service.search("test query")

        mock_dense.assert_called_once()
        mock_bm25.assert_not_called()

    @pytest.mark.unit
    async def test_search_with_context_window(self, rag_service):
        """Context window is applied when > 0."""
        mock_embedding = [0.1] * 768

        with patch.object(rag_service, "get_embedding", return_value=mock_embedding), \
             patch.object(rag_service, "_search_dense", return_value=[make_result(1)]), \
             patch.object(rag_service, "_expand_context_window", return_value=[make_result(1)]) as mock_expand, \
             patch("services.rag_service.settings") as mock_settings:

            mock_settings.rag_hybrid_enabled = False
            mock_settings.rag_top_k = 5
            mock_settings.rag_similarity_threshold = 0.3
            mock_settings.rag_context_window = 1
            mock_settings.rag_context_window_max = 3

            await rag_service.search("test query")

        mock_expand.assert_called_once()

    @pytest.mark.unit
    async def test_search_context_window_clamped(self, rag_service):
        """Context window is clamped to max."""
        mock_embedding = [0.1] * 768

        with patch.object(rag_service, "get_embedding", return_value=mock_embedding), \
             patch.object(rag_service, "_search_dense", return_value=[make_result(1)]), \
             patch.object(rag_service, "_expand_context_window", return_value=[make_result(1)]) as mock_expand, \
             patch("services.rag_service.settings") as mock_settings:

            mock_settings.rag_hybrid_enabled = False
            mock_settings.rag_top_k = 5
            mock_settings.rag_similarity_threshold = 0.3
            mock_settings.rag_context_window = 10  # Exceeds max
            mock_settings.rag_context_window_max = 3

            await rag_service.search("test query")

        # Should be called with clamped value (min(10, 3) = 3)
        mock_expand.assert_called_once()
        call_args = mock_expand.call_args
        assert call_args[0][1] == 3  # window_size argument

    @pytest.mark.unit
    async def test_search_embedding_error_returns_empty(self, rag_service):
        """Embedding error returns empty results gracefully."""
        with patch.object(rag_service, "get_embedding", side_effect=Exception("Connection refused")), \
             patch("services.rag_service.settings") as mock_settings:

            mock_settings.rag_top_k = 5
            mock_settings.rag_similarity_threshold = 0.3

            results = await rag_service.search("test query")

        assert results == []


# =============================================================================
# FTS Reindex Tests
# =============================================================================

class TestFTSReindex:
    """Tests for reindex_fts method."""

    @pytest.fixture
    def rag_service(self):
        db = AsyncMock()
        return RAGService(db)

    @pytest.mark.unit
    async def test_reindex_fts_returns_count(self, rag_service):
        """reindex_fts returns the count of updated chunks."""
        mock_result = MagicMock()
        mock_result.rowcount = 42
        rag_service.db.execute = AsyncMock(return_value=mock_result)

        with patch("services.rag_service.settings") as mock_settings:
            mock_settings.rag_hybrid_fts_config = "simple"

            result = await rag_service.reindex_fts()

        assert result["updated_count"] == 42
        assert result["fts_config"] == "simple"

    @pytest.mark.unit
    async def test_reindex_fts_uses_config(self, rag_service):
        """reindex_fts uses the configured FTS config."""
        mock_result = MagicMock()
        mock_result.rowcount = 0
        rag_service.db.execute = AsyncMock(return_value=mock_result)

        with patch("services.rag_service.settings") as mock_settings:
            mock_settings.rag_hybrid_fts_config = "german"

            result = await rag_service.reindex_fts()

        assert result["fts_config"] == "german"
        # Verify SQL was called with german config
        call_args = rag_service.db.execute.call_args
        assert call_args[0][1]["fts_config"] == "german"


# =============================================================================
# Config Tests
# =============================================================================

class TestHybridSearchConfig:
    """Tests for hybrid search configuration defaults."""

    @pytest.mark.unit
    def test_default_hybrid_enabled(self):
        """Hybrid search is enabled by default."""
        s = Settings(
            _env_file=None,
            database_url="sqlite:///:memory:",
            postgres_host="localhost"
        )
        assert s.rag_hybrid_enabled is True

    @pytest.mark.unit
    def test_default_weights(self):
        """Default weights sum and are valid."""
        s = Settings(
            _env_file=None,
            database_url="sqlite:///:memory:",
            postgres_host="localhost"
        )
        assert s.rag_hybrid_bm25_weight == 0.3
        assert s.rag_hybrid_dense_weight == 0.7
        assert s.rag_hybrid_bm25_weight + s.rag_hybrid_dense_weight == pytest.approx(1.0)

    @pytest.mark.unit
    def test_default_context_window(self):
        """Default context window is 1."""
        s = Settings(
            _env_file=None,
            database_url="sqlite:///:memory:",
            postgres_host="localhost"
        )
        assert s.rag_context_window == 1
        assert s.rag_context_window_max == 3

    @pytest.mark.unit
    def test_default_fts_config(self):
        """Default FTS config is 'simple'."""
        s = Settings(
            _env_file=None,
            database_url="sqlite:///:memory:",
            postgres_host="localhost"
        )
        assert s.rag_hybrid_fts_config == "simple"

    @pytest.mark.unit
    def test_default_rrf_k(self):
        """Default RRF k is 60."""
        s = Settings(
            _env_file=None,
            database_url="sqlite:///:memory:",
            postgres_host="localhost"
        )
        assert s.rag_hybrid_rrf_k == 60
