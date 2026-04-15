"""
Tests for `services.knowledge_tool.knowledge_search`.

Moved from `test_internal_tools.py::TestKnowledgeSearch` in the
Phase 1 W4 internal-tools split. The RAG-based knowledge search tool
stays on the platform (pure DB + RAGService), while the rest of
`InternalToolService` (room resolution, HA media, DLNA, BLE presence,
radio) moved into `ha_glue/services/internal_tools.py`.
"""

import sys
from contextlib import asynccontextmanager
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.knowledge_tool import knowledge_search


# ============================================================================
# Helpers
# ============================================================================


def _stub_db_and_rag_modules():
    """Guarantee `services.database` and `services.rag_service` are importable.

    The platform modules are real but depend on asyncpg + pgvector which
    aren't installed in the minimal test env. We stub them so `patch()`
    targeting `services.database.AsyncSessionLocal` and
    `services.rag_service.RAGService` works under `create=True`.
    """
    added: list[str] = []
    for mod_name in ("services.database", "services.rag_service"):
        if mod_name not in sys.modules:
            sys.modules[mod_name] = ModuleType(mod_name)
            added.append(mod_name)
    return added


def _teardown_stubs(added: list[str]) -> None:
    for mod_name in added:
        sys.modules.pop(mod_name, None)


# ============================================================================
# Tests
# ============================================================================


class TestKnowledgeSearch:
    """Test `services.knowledge_tool.knowledge_search`."""

    @pytest.mark.unit
    async def test_returns_results(self):
        """Successful RAG search returns formatted context."""
        mock_rag = MagicMock()
        mock_rag.search = AsyncMock(return_value=[
            {
                "chunk": {"content": "Rechnung Am Stirkenbend 20 vom 15.03.2022"},
                "document": {"filename": "rechnung_2022_03.pdf"},
                "similarity": 0.85,
            },
            {
                "chunk": {"content": "Nebenkostenabrechnung 2022"},
                "document": {"filename": "nebenkosten_2022.pdf"},
                "similarity": 0.78,
            },
        ])

        mock_db = AsyncMock()

        @asynccontextmanager
        async def mock_session():
            yield mock_db

        stubs = _stub_db_and_rag_modules()
        try:
            with patch("services.database.AsyncSessionLocal", mock_session, create=True), \
                 patch("services.rag_service.RAGService", return_value=mock_rag, create=True):
                result = await knowledge_search({"query": "Rechnungen 2022 Am Stirkenbend"})
        finally:
            _teardown_stubs(stubs)

        assert result["success"] is True
        assert result["data"]["results_count"] == 2
        assert "rechnung_2022_03.pdf" in result["data"]["context"]
        assert "nebenkosten_2022.pdf" in result["data"]["context"]
        mock_rag.search.assert_called_once_with(query="Rechnungen 2022 Am Stirkenbend", top_k=None)

    @pytest.mark.unit
    async def test_custom_top_k(self):
        """Custom top_k is forwarded to RAG search."""
        mock_rag = MagicMock()
        mock_rag.search = AsyncMock(return_value=[
            {
                "chunk": {"content": "Test content"},
                "document": {"filename": "test.pdf"},
                "similarity": 0.9,
            },
        ])

        mock_db = AsyncMock()

        @asynccontextmanager
        async def mock_session():
            yield mock_db

        stubs = _stub_db_and_rag_modules()
        try:
            with patch("services.database.AsyncSessionLocal", mock_session, create=True), \
                 patch("services.rag_service.RAGService", return_value=mock_rag, create=True):
                result = await knowledge_search({"query": "test", "top_k": "30"})
        finally:
            _teardown_stubs(stubs)

        assert result["success"] is True
        mock_rag.search.assert_called_once_with(query="test", top_k=30)

    @pytest.mark.unit
    async def test_no_results(self):
        """Empty RAG results return empty_result flag."""
        mock_rag = MagicMock()
        mock_rag.search = AsyncMock(return_value=[])

        mock_db = AsyncMock()

        @asynccontextmanager
        async def mock_session():
            yield mock_db

        stubs = _stub_db_and_rag_modules()
        try:
            with patch("services.database.AsyncSessionLocal", mock_session, create=True), \
                 patch("services.rag_service.RAGService", return_value=mock_rag, create=True):
                result = await knowledge_search({"query": "nonexistent document"})
        finally:
            _teardown_stubs(stubs)

        assert result["success"] is True
        assert result.get("empty_result") is True
        assert result["data"]["results_count"] == 0

    @pytest.mark.unit
    async def test_missing_query(self):
        """Missing query returns error."""
        result = await knowledge_search({})
        assert result["success"] is False
        assert "required" in result["message"]

    @pytest.mark.unit
    async def test_empty_query(self):
        """Empty/whitespace-only query returns error."""
        result = await knowledge_search({"query": "  "})
        assert result["success"] is False
        assert "required" in result["message"]

    @pytest.mark.unit
    async def test_exception(self):
        """RAG service exception returns clean error."""
        mock_db = AsyncMock()

        @asynccontextmanager
        async def mock_session():
            yield mock_db

        mock_rag = MagicMock()
        mock_rag.search = AsyncMock(side_effect=RuntimeError("DB connection failed"))

        stubs = _stub_db_and_rag_modules()
        try:
            with patch("services.database.AsyncSessionLocal", mock_session, create=True), \
                 patch("services.rag_service.RAGService", return_value=mock_rag, create=True):
                result = await knowledge_search({"query": "test"})
        finally:
            _teardown_stubs(stubs)

        assert result["success"] is False
        assert "error" in result["message"].lower()
