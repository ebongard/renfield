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
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Imported so `patch("services.document_fact_retrieval.DocumentFactRetrieval")`
# can resolve the submodule (knowledge_tool imports it lazily inside the
# flag-gated fact path, so it isn't otherwise loaded at test-collection time).
import services.document_fact_retrieval  # noqa: F401
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
        mock_rag.search.assert_called_once_with(
            query="Rechnungen 2022 Am Stirkenbend", top_k=None, user_id=None
        )

    @pytest.mark.unit
    async def test_chunk_cap_follows_setting(self, monkeypatch):
        """The per-chunk char cap in the context block is configurable
        (knowledge_context_chunk_chars) so a large-context deployment can pass
        retrieved chunks to the agent uncut."""
        long_content = "A" * 900
        mock_rag = MagicMock()
        mock_rag.search = AsyncMock(return_value=[
            {
                "chunk": {"content": long_content},
                "document": {"filename": "doc.pdf"},
                "similarity": 0.9,
            },
        ])

        mock_db = AsyncMock()

        @asynccontextmanager
        async def mock_session():
            yield mock_db

        monkeypatch.setattr(
            "services.knowledge_tool.settings.knowledge_context_chunk_chars", 100
        )
        stubs = _stub_db_and_rag_modules()
        try:
            with patch("services.database.AsyncSessionLocal", mock_session, create=True), \
                 patch("services.rag_service.RAGService", return_value=mock_rag, create=True):
                result = await knowledge_search({"query": "test"})
        finally:
            _teardown_stubs(stubs)

        context = result["data"]["context"]
        assert "A" * 100 in context
        assert "A" * 101 not in context

    @pytest.mark.unit
    async def test_returns_structured_sources(self):
        """Results carry a deduped, document-keyed `sources` list for the chat
        provenance-chips UI (filename, title, tier)."""
        mock_rag = MagicMock()
        mock_rag.search = AsyncMock(return_value=[
            {
                "chunk": {"content": "chunk A1"},
                "document": {"id": 7, "filename": "rechnung.pdf", "title": "Rechnung März", "circle_tier": 2},
            },
            {  # second chunk of the SAME document → must dedupe to one source
                "chunk": {"content": "chunk A2"},
                "document": {"id": 7, "filename": "rechnung.pdf", "title": "Rechnung März", "circle_tier": 2},
            },
            {
                "chunk": {"content": "chunk B1"},
                "document": {"id": 9, "filename": "vertrag.pdf", "title": "Vertrag", "circle_tier": 0},
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
                result = await knowledge_search({"query": "x"})
        finally:
            _teardown_stubs(stubs)

        sources = result["data"]["sources"]
        assert [s["document_id"] for s in sources] == [7, 9]  # deduped, order-preserved
        assert sources[0] == {
            "document_id": 7, "filename": "rechnung.pdf", "title": "Rechnung März", "tier": 2,
        }
        assert sources[1]["tier"] == 0

    @pytest.mark.unit
    async def test_no_results_has_no_sources(self):
        """Empty search → no `sources` key (UI renders nothing)."""
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
                result = await knowledge_search({"query": "nope"})
        finally:
            _teardown_stubs(stubs)

        assert "sources" not in result["data"]

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
        mock_rag.search.assert_called_once_with(query="test", top_k=30, user_id=None)

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


# ============================================================================
# Schicht A fact wiring (internal.knowledge_search folds document_facts in)
# ============================================================================


def _doc_title_row(id, generated_title, title, filename, circle_tier):
    return SimpleNamespace(
        id=id,
        generated_title=generated_title,
        title=title,
        filename=filename,
        circle_tier=circle_tier,
    )


def _mock_db_with_title_rows(rows):
    """A mock DB whose ``execute(...).all()`` returns fact-source title rows.

    The RAG search is mocked separately (``mock_rag.search``); the only
    ``db.execute`` call in ``knowledge_search`` is the fact source-title lookup.
    """
    mock_db = AsyncMock()
    result_obj = MagicMock()
    result_obj.all.return_value = rows
    mock_db.execute = AsyncMock(return_value=result_obj)
    return mock_db


class TestKnowledgeSearchFacts:
    """`internal.knowledge_search` folds circle-filtered Schicht A facts into the
    context + provenance chips when `schicht_a_extraction_enabled` is on."""

    @pytest.mark.unit
    async def test_facts_injected_and_fact_only_doc_gets_chip(self):
        """A fact from a document with NO chunk hit is injected into the FAKTEN
        block AND gets its own source chip."""
        mock_rag = MagicMock()
        mock_rag.search = AsyncMock(return_value=[
            {"chunk": {"content": "chunk of doc 7"},
             "document": {"id": 7, "filename": "vertrag.pdf", "title": "Vertrag", "circle_tier": 0}},
        ])
        facts = [
            {"document_id": 42, "category": "identifier", "kind": "steuernummer",
             "value": "114/5876/5293", "normalized_value": "11458765293",
             "obligation_date": None, "amount_value": None, "amount_currency": None},
        ]
        mock_fact_retrieval = MagicMock()
        mock_fact_retrieval.search = AsyncMock(return_value=facts)

        mock_db = _mock_db_with_title_rows([
            _doc_title_row(42, "Steuerbescheid 2023", None, "bescheid.pdf", 0),
        ])

        @asynccontextmanager
        async def mock_session():
            yield mock_db

        stubs = _stub_db_and_rag_modules()
        try:
            with patch("services.database.AsyncSessionLocal", mock_session, create=True), \
                 patch("services.rag_service.RAGService", return_value=mock_rag, create=True), \
                 patch("services.knowledge_tool.settings.schicht_a_extraction_enabled", True), \
                 patch("services.document_fact_retrieval.DocumentFactRetrieval",
                       return_value=mock_fact_retrieval, create=True):
                result = await knowledge_search({"query": "steuernummer"})
        finally:
            _teardown_stubs(stubs)

        assert result["success"] is True
        data = result["data"]
        assert data["facts_count"] == 1
        assert data["results_count"] == 1
        assert "FAKTEN" in data["context"]
        assert "steuernummer: 114/5876/5293" in data["context"]
        assert "Steuerbescheid 2023" in data["context"]  # fact source title
        assert "PASSAGEN" in data["context"]  # chunk passages still present
        # Both the chunk source (7) and the fact-only source (42) get a chip.
        assert [s["document_id"] for s in data["sources"]] == [7, 42]
        assert data["sources"][1]["title"] == "Steuerbescheid 2023"
        assert data["facts"][0]["kind"] == "steuernummer"
        mock_fact_retrieval.search.assert_called_once()

    @pytest.mark.unit
    async def test_facts_without_any_chunks_still_succeed(self):
        """Facts present but zero chunk hits → success (not empty_result)."""
        mock_rag = MagicMock()
        mock_rag.search = AsyncMock(return_value=[])
        facts = [
            {"document_id": 5, "category": "obligation", "kind": "zahlungsfrist",
             "value": "Stromabschlag", "normalized_value": None,
             "obligation_date": "2026-03-15", "amount_value": 89.5, "amount_currency": "EUR"},
        ]
        mock_fact_retrieval = MagicMock()
        mock_fact_retrieval.search = AsyncMock(return_value=facts)

        mock_db = _mock_db_with_title_rows([
            _doc_title_row(5, None, "Stadtwerke Rechnung", "sw.pdf", 2),
        ])

        @asynccontextmanager
        async def mock_session():
            yield mock_db

        stubs = _stub_db_and_rag_modules()
        try:
            with patch("services.database.AsyncSessionLocal", mock_session, create=True), \
                 patch("services.rag_service.RAGService", return_value=mock_rag, create=True), \
                 patch("services.knowledge_tool.settings.schicht_a_extraction_enabled", True), \
                 patch("services.document_fact_retrieval.DocumentFactRetrieval",
                       return_value=mock_fact_retrieval, create=True):
                result = await knowledge_search({"query": "wann muss ich zahlen"})
        finally:
            _teardown_stubs(stubs)

        assert result["success"] is True
        assert result.get("empty_result") is not True
        data = result["data"]
        assert data["results_count"] == 0
        assert data["facts_count"] == 1
        assert data["context"].startswith("FAKTEN")
        assert "PASSAGEN" not in data["context"]
        assert "Frist 2026-03-15" in data["context"]
        assert "Betrag 89.5 EUR" in data["context"]
        assert [s["document_id"] for s in data["sources"]] == [5]

    @pytest.mark.unit
    async def test_fact_doc_overlapping_chunk_is_not_double_chipped(self):
        """A fact whose document already produced a chunk chip does not add a
        second chip (deduped by document_id)."""
        mock_rag = MagicMock()
        mock_rag.search = AsyncMock(return_value=[
            {"chunk": {"content": "chunk of doc 7"},
             "document": {"id": 7, "filename": "bescheid.pdf", "title": "Bescheid", "circle_tier": 0}},
        ])
        facts = [
            {"document_id": 7, "category": "identifier", "kind": "iban",
             "value": "DE12...", "normalized_value": "DE12",
             "obligation_date": None, "amount_value": None, "amount_currency": None},
        ]
        mock_fact_retrieval = MagicMock()
        mock_fact_retrieval.search = AsyncMock(return_value=facts)
        mock_db = _mock_db_with_title_rows([
            _doc_title_row(7, None, "Bescheid", "bescheid.pdf", 0),
        ])

        @asynccontextmanager
        async def mock_session():
            yield mock_db

        stubs = _stub_db_and_rag_modules()
        try:
            with patch("services.database.AsyncSessionLocal", mock_session, create=True), \
                 patch("services.rag_service.RAGService", return_value=mock_rag, create=True), \
                 patch("services.knowledge_tool.settings.schicht_a_extraction_enabled", True), \
                 patch("services.document_fact_retrieval.DocumentFactRetrieval",
                       return_value=mock_fact_retrieval, create=True):
                result = await knowledge_search({"query": "iban"})
        finally:
            _teardown_stubs(stubs)

        data = result["data"]
        assert [s["document_id"] for s in data["sources"]] == [7]  # not [7, 7]
        assert "iban: DE12..." in data["context"]

    @pytest.mark.unit
    async def test_fact_from_nonvisible_document_leaks_no_title_or_chip(self):
        """A tier-overridden fact whose PARENT document the asker can't see (the
        document is circle-filtered out of the title lookup) must NOT leak the
        document's title/filename: generic 'Dokument {id}' Quelle, and NO chip."""
        mock_rag = MagicMock()
        mock_rag.search = AsyncMock(return_value=[
            {"chunk": {"content": "chunk of doc 7"},
             "document": {"id": 7, "filename": "vertrag.pdf", "title": "Vertrag", "circle_tier": 0}},
        ])
        facts = [
            # visible doc 7 (also a chunk hit)
            {"document_id": 7, "category": "identifier", "kind": "iban",
             "value": "DE12", "normalized_value": "DE12",
             "obligation_date": None, "amount_value": None, "amount_currency": None},
            # public-override fact on PRIVATE doc 99 — asker sees the fact, not the doc
            {"document_id": 99, "category": "universal", "kind": "issuer",
             "value": "Finanzamt", "normalized_value": None,
             "obligation_date": None, "amount_value": None, "amount_currency": None},
        ]
        mock_fact_retrieval = MagicMock()
        mock_fact_retrieval.search = AsyncMock(return_value=facts)
        # The circle-filtered doc query returns ONLY doc 7 — doc 99 (private) is
        # excluded, so its title/filename never enters doc_meta.
        mock_db = _mock_db_with_title_rows([
            _doc_title_row(7, None, "Vertrag", "vertrag.pdf", 0),
        ])

        @asynccontextmanager
        async def mock_session():
            yield mock_db

        stubs = _stub_db_and_rag_modules()
        try:
            with patch("services.database.AsyncSessionLocal", mock_session, create=True), \
                 patch("services.rag_service.RAGService", return_value=mock_rag, create=True), \
                 patch("services.knowledge_tool.settings.schicht_a_extraction_enabled", True), \
                 patch("services.document_fact_retrieval.DocumentFactRetrieval",
                       return_value=mock_fact_retrieval, create=True):
                result = await knowledge_search({"query": "issuer", "user_id": "500"})
        finally:
            _teardown_stubs(stubs)

        data = result["data"]
        # The private document's title/filename must NOT appear anywhere.
        assert "geheim" not in data["context"].lower()
        assert all("geheim" not in (s.get("title") or "").lower() for s in data["sources"])
        # The fact itself IS surfaced (it's circle-visible), under a generic Quelle.
        assert "issuer: Finanzamt" in data["context"]
        assert "Dokument 99" in data["context"]
        # Only the visible doc (7) gets a chip; the private doc (99) does not.
        assert [s["document_id"] for s in data["sources"]] == [7]

    @pytest.mark.unit
    async def test_fact_retrieval_failure_is_swallowed(self):
        """A fact-retrieval exception never fails the chunk-based answer."""
        mock_rag = MagicMock()
        mock_rag.search = AsyncMock(return_value=[
            {"chunk": {"content": "chunk"}, "document": {"id": 1, "filename": "a.pdf"}},
        ])
        mock_fact_retrieval = MagicMock()
        mock_fact_retrieval.search = AsyncMock(side_effect=RuntimeError("fts blew up"))
        mock_db = AsyncMock()

        @asynccontextmanager
        async def mock_session():
            yield mock_db

        stubs = _stub_db_and_rag_modules()
        try:
            with patch("services.database.AsyncSessionLocal", mock_session, create=True), \
                 patch("services.rag_service.RAGService", return_value=mock_rag, create=True), \
                 patch("services.knowledge_tool.settings.schicht_a_extraction_enabled", True), \
                 patch("services.document_fact_retrieval.DocumentFactRetrieval",
                       return_value=mock_fact_retrieval, create=True):
                result = await knowledge_search({"query": "x"})
        finally:
            _teardown_stubs(stubs)

        assert result["success"] is True
        assert result["data"]["facts_count"] == 0
        assert result["data"]["results_count"] == 1
