"""
Tests for advanced RAG features: BM25 fallback, contextual retrieval,
parent-child chunking, reranking, and evaluation pipeline.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.rag_service import RAGService
from utils.config import settings


@pytest.fixture
def mock_db():
    """Mock async database session."""
    db = AsyncMock()
    db.add = MagicMock()
    db.add_all = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.flush = AsyncMock()
    db.execute = AsyncMock()
    return db


@pytest.fixture
def rag_service(mock_db):
    return RAGService(mock_db)


# ===========================================================================
# 1. BM25 Fallback when embedding fails
# ===========================================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_search_falls_back_to_bm25_on_embedding_failure(rag_service, mock_db):
    """When embedding model is unreachable, search should fall back to BM25-only."""
    rag_service.get_embedding = AsyncMock(side_effect=ConnectionError("Ollama down"))
    rag_service._search_bm25 = AsyncMock(return_value=[
        {"chunk": {"id": 1, "content": "test", "chunk_index": 0, "page_number": 1,
                   "section_title": None, "chunk_type": "paragraph", "parent_chunk_id": None},
         "document": {"id": 1, "filename": "test.pdf", "title": "Test"},
         "similarity": 0.5}
    ])
    rag_service._resolve_parents = AsyncMock(side_effect=lambda x: x)

    results = await rag_service.search("test query")

    assert len(results) == 1
    rag_service._search_bm25.assert_called_once()


# ===========================================================================
# 2. Embedding timeout
# ===========================================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_embedding_timeout_raises(rag_service):
    """get_embedding should raise TimeoutError when Ollama is too slow."""
    async def slow_embed(*args, **kwargs):
        await asyncio.sleep(10)

    mock_client = AsyncMock()
    mock_client.embeddings = slow_embed
    rag_service._get_ollama_client = AsyncMock(return_value=mock_client)

    with patch.object(settings, "rag_embedding_timeout", 0.01):
        with pytest.raises(asyncio.TimeoutError):
            await rag_service.get_embedding("test")


# ===========================================================================
# 3. Contextual Retrieval prefix generation
# ===========================================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_contextualize_chunks_adds_prefix(rag_service):
    """_contextualize_chunks should prepend LLM-generated prefix to text_for_embedding."""
    rag_service._generate_context_prefix = AsyncMock(return_value="Aus einer DEVK-Rechnung, Kfz-Versicherung.")

    with patch.object(settings, "rag_contextual_retrieval", True):
        chunks = [{"text": "Der Beitrag betraegt 234,50 EUR.", "chunk_index": 0, "metadata": {}}]
        result = await rag_service._contextualize_chunks(chunks, "DEVK Rechnung")

        assert "text_for_embedding" in result[0]
        assert "DEVK" in result[0]["text_for_embedding"]
        assert result[0]["text"] == "Der Beitrag betraegt 234,50 EUR."  # Original unchanged


@pytest.mark.unit
@pytest.mark.asyncio
async def test_contextualize_disabled_returns_unchanged(rag_service):
    """When rag_contextual_retrieval is False, chunks pass through unchanged."""
    with patch.object(settings, "rag_contextual_retrieval", False):
        chunks = [{"text": "test", "chunk_index": 0, "metadata": {}}]
        result = await rag_service._contextualize_chunks(chunks, "summary")
        assert "text_for_embedding" not in result[0]


# ===========================================================================
# 4. Parent-Child chunk creation
# ===========================================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parent_child_creates_parent_and_children(rag_service, mock_db):
    """_ingest_parent_child should create parent chunks without embeddings and children with embeddings."""
    rag_service.get_embedding = AsyncMock(return_value=[0.1] * 768)

    # Simulate 4 child-sized chunks that form 1 parent
    chunks = [
        {"text": f"Child chunk {i}", "chunk_index": i, "metadata": {"headings": [], "page_number": 1, "chunk_type": "paragraph"}}
        for i in range(4)
    ]

    with patch.object(settings, "rag_parent_chunk_size", 1024), \
         patch.object(settings, "rag_child_chunk_size", 256):
        sem = asyncio.Semaphore(5)
        # Mock flush to set parent.id
        call_count = 0

        async def mock_flush():
            nonlocal call_count
            call_count += 1

        mock_db.flush = mock_flush

        result = await rag_service._ingest_parent_child(1, chunks, sem)

    # Should have 1 parent + 4 children = 5 objects
    parents = [r for r in result if r.chunk_type == "parent"]
    children = [r for r in result if r.chunk_type != "parent"]

    assert len(parents) == 1
    assert len(children) == 4
    assert parents[0].embedding is None  # Parent has no embedding
    assert all(c.embedding is not None for c in children)


# ===========================================================================
# 5. Parent resolution in search
# ===========================================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_parents_replaces_child_content(rag_service, mock_db):
    """_resolve_parents should replace child content with parent content and deduplicate."""
    # Two children from same parent
    results = [
        {"chunk": {"id": 10, "content": "child 1", "parent_chunk_id": 100,
                   "chunk_index": 0, "page_number": 1, "section_title": None, "chunk_type": "paragraph"},
         "document": {"id": 1, "filename": "test.pdf", "title": "Test"}, "similarity": 0.9},
        {"chunk": {"id": 11, "content": "child 2", "parent_chunk_id": 100,
                   "chunk_index": 1, "page_number": 1, "section_title": None, "chunk_type": "paragraph"},
         "document": {"id": 1, "filename": "test.pdf", "title": "Test"}, "similarity": 0.8},
    ]

    # Mock parent fetch
    mock_row = MagicMock()
    mock_row.id = 100
    mock_row.content = "Full parent context with both children"
    mock_row.page_number = 1
    mock_row.section_title = "Section A"
    mock_result = MagicMock()
    mock_result.fetchall.return_value = [mock_row]
    mock_db.execute = AsyncMock(return_value=mock_result)

    resolved = await rag_service._resolve_parents(results)

    # Should deduplicate: only 1 result (highest scoring child's parent)
    assert len(resolved) == 1
    assert resolved[0]["chunk"]["content"] == "Full parent context with both children"
    assert resolved[0]["chunk"]["chunk_type"] == "parent"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_parents_passthrough_no_parents(rag_service):
    """_resolve_parents returns results unchanged when no parent_chunk_id is set."""
    results = [
        {"chunk": {"id": 1, "content": "flat chunk", "parent_chunk_id": None,
                   "chunk_index": 0, "page_number": 1, "section_title": None, "chunk_type": "paragraph"},
         "document": {"id": 1, "filename": "test.pdf", "title": "Test"}, "similarity": 0.9},
    ]
    resolved = await rag_service._resolve_parents(results)
    assert len(resolved) == 1
    assert resolved[0]["chunk"]["content"] == "flat chunk"


# ===========================================================================
# 6. Reranker happy path
# ===========================================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rerank_reorders_and_reduces(rag_service):
    """_rerank should reorder results by reranker score and reduce to rag_rerank_top_k."""
    mock_embeddings = {
        "query": [1.0, 0.0, 0.0],
        "good chunk": [0.9, 0.1, 0.0],  # High similarity to query
        "bad chunk": [0.0, 0.0, 1.0],   # Low similarity
        "ok chunk": [0.5, 0.5, 0.0],    # Medium similarity
    }

    async def mock_embeddings_fn(model, prompt):
        key = prompt[:20].strip()
        for k, v in mock_embeddings.items():
            if k in prompt[:50]:
                resp = MagicMock()
                resp.embedding = v
                return resp
        resp = MagicMock()
        resp.embedding = [0.0, 0.0, 0.0]
        return resp

    mock_client = AsyncMock()
    mock_client.embeddings = mock_embeddings_fn
    rag_service._get_ollama_client = AsyncMock(return_value=mock_client)

    results = [
        {"chunk": {"id": 1, "content": "bad chunk text"}, "document": {"id": 1}, "similarity": 0.9},
        {"chunk": {"id": 2, "content": "good chunk text"}, "document": {"id": 2}, "similarity": 0.5},
        {"chunk": {"id": 3, "content": "ok chunk text"}, "document": {"id": 3}, "similarity": 0.7},
    ]

    with patch.object(settings, "rag_rerank_enabled", True), \
         patch.object(settings, "rag_rerank_top_k", 2), \
         patch.object(settings, "rag_rerank_model", "test-reranker"):
        reranked = await rag_service._rerank("query about things", results)

    assert len(reranked) == 2  # Reduced to top_k=2


# ===========================================================================
# 7. Reranker fallback on failure
# ===========================================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rerank_fallback_on_error(rag_service):
    """_rerank should return un-reranked results when reranker model fails."""
    mock_client = AsyncMock()
    mock_client.embeddings = AsyncMock(side_effect=ConnectionError("model not found"))
    rag_service._get_ollama_client = AsyncMock(return_value=mock_client)

    results = [
        {"chunk": {"id": i, "content": f"chunk {i}"}, "document": {"id": i}, "similarity": 0.5}
        for i in range(10)
    ]

    with patch.object(settings, "rag_rerank_enabled", True), \
         patch.object(settings, "rag_rerank_top_k", 3):
        reranked = await rag_service._rerank("query", results)

    assert len(reranked) == 3  # Falls back to simple truncation


# ===========================================================================
# 8. Config toggles (features can be disabled independently)
# ===========================================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rerank_disabled_returns_truncated(rag_service):
    """When rag_rerank_enabled is False, _rerank just truncates to top_k."""
    results = [{"chunk": {"id": i}, "similarity": 1.0 - i * 0.1} for i in range(10)]

    with patch.object(settings, "rag_rerank_enabled", False), \
         patch.object(settings, "rag_rerank_top_k", 3):
        reranked = await rag_service._rerank("query", results)

    assert len(reranked) == 3
    assert reranked[0]["chunk"]["id"] == 0  # Original order preserved


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parent_child_disabled_uses_flat(rag_service, mock_db):
    """When rag_parent_child_enabled is False, ingest uses flat chunking."""
    rag_service.get_embedding = AsyncMock(return_value=[0.1] * 768)

    chunks = [{"text": "test", "chunk_index": 0, "metadata": {"headings": [], "chunk_type": "paragraph"}}]

    with patch.object(settings, "rag_parent_child_enabled", False):
        result = await rag_service._ingest_flat(1, chunks, asyncio.Semaphore(5))

    assert len(result) == 1
    assert result[0].parent_chunk_id is None


# ===========================================================================
# 9. RAG Eval Service scoring
# ===========================================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_eval_score_parses_number():
    """RAGEvalService._score should extract a numeric score from LLM response."""
    from services.rag_eval_service import RAGEvalService

    mock_db = AsyncMock()
    svc = RAGEvalService(mock_db)

    mock_response = MagicMock()
    mock_response.response = "8"

    with patch("services.rag_eval_service.get_default_client") as mock_get:
        mock_client = AsyncMock()
        mock_client.generate = AsyncMock(return_value=mock_response)
        mock_get.return_value = mock_client

        score = await svc._score("relevance", query="test", context="test context")

    assert score == 8.0


# ===========================================================================
# 10. Eval test case loading
# ===========================================================================


@pytest.mark.unit
def test_eval_load_test_cases_from_yaml(tmp_path):
    """load_test_cases should parse YAML test cases file."""
    from services.rag_eval_service import RAGEvalService

    yaml_content = """
test_cases:
  - query: "What is X?"
    expected_source: "doc.pdf"
    expected_answer_contains: ["X"]
  - query: "Who is Y?"
    expected_source: null
    expected_answer_contains: []
"""
    p = tmp_path / "test_cases.yaml"
    p.write_text(yaml_content)

    cases = RAGEvalService.load_test_cases(str(p))
    assert len(cases) == 2
    assert cases[0]["query"] == "What is X?"
    assert cases[0]["expected_source"] == "doc.pdf"
