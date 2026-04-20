"""
Regression tests for the KnowledgeGraphService -> KGRetrieval extraction
(Lane A2 of the second-brain-circles eng-review plan).

Critical invariant: behaviour of KnowledgeGraphService.get_relevant_context
must be IDENTICAL whether routed through the legacy inline code (flag off)
or through the extracted KGRetrieval module (flag on).

Approach (mirrors test_rag_retrieval_extract.py):
- _extract_query_entities parity: mock the LLM client; assert both classes
  parse identical raw responses into the same list of entity names.
- get_relevant_context: unconditionally routes to KGRetrieval
  actually re-routes calls to KGRetrieval (and does NOT route when off).
- KGRetrieval public-surface check: catches accidental method renames.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.kg_retrieval import KGRetrieval
from services.knowledge_graph_service import KnowledgeGraphService


def _ollama_chat_response(content_text):
    """Build a mock object that resembles an ollama-python chat response."""
    response = MagicMock()
    response.message = MagicMock()
    response.message.content = content_text
    response.content = content_text
    return response


class TestExtractQueryEntitiesParity:
    """Both classes must parse the same raw LLM output into the same entity list."""

    @pytest.fixture
    def patched_prompt_manager(self):
        with patch("services.prompt_manager.prompt_manager") as pm:
            pm.get.return_value = "stub-prompt"
            pm.get_config.return_value = {}
            yield pm

    @pytest.fixture
    def patched_llm_client(self):
        with patch("utils.llm_client.extract_response_content") as extract, \
             patch("utils.llm_client.get_classification_chat_kwargs", return_value={}):
            yield extract

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_parses_clean_json_array(self, patched_prompt_manager, patched_llm_client):
        patched_llm_client.return_value = '["Anna", "Norway", "Eduard"]'
        chat_resp = _ollama_chat_response('["Anna", "Norway", "Eduard"]')

        for cls in (KnowledgeGraphService, KGRetrieval):
            instance = cls(MagicMock())
            instance._ollama_client = MagicMock()
            instance._ollama_client.chat = AsyncMock(return_value=chat_resp)
            result = await instance._extract_query_entities("any query", lang="en")
            assert result == ["Anna", "Norway", "Eduard"], f"{cls.__name__} returned {result}"

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_parses_array_inside_markdown_fence(self, patched_prompt_manager, patched_llm_client):
        raw = '```json\n["Mom", "recipe"]\n```'
        patched_llm_client.return_value = raw
        chat_resp = _ollama_chat_response(raw)

        for cls in (KnowledgeGraphService, KGRetrieval):
            instance = cls(MagicMock())
            instance._ollama_client = MagicMock()
            instance._ollama_client.chat = AsyncMock(return_value=chat_resp)
            result = await instance._extract_query_entities("any query", lang="en")
            assert result == ["Mom", "recipe"]

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_returns_empty_on_parse_failure(self, patched_prompt_manager, patched_llm_client):
        patched_llm_client.return_value = "not json at all"
        chat_resp = _ollama_chat_response("not json at all")

        for cls in (KnowledgeGraphService, KGRetrieval):
            instance = cls(MagicMock())
            instance._ollama_client = MagicMock()
            instance._ollama_client.chat = AsyncMock(return_value=chat_resp)
            result = await instance._extract_query_entities("any query", lang="en")
            assert result == []

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_returns_empty_on_llm_exception(self, patched_prompt_manager, patched_llm_client):
        for cls in (KnowledgeGraphService, KGRetrieval):
            instance = cls(MagicMock())
            instance._ollama_client = MagicMock()
            instance._ollama_client.chat = AsyncMock(side_effect=RuntimeError("ollama down"))
            result = await instance._extract_query_entities("any query", lang="en")
            assert result == []

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_filters_non_string_items(self, patched_prompt_manager, patched_llm_client):
        raw = '["Anna", 42, null, "  ", "Bob"]'
        patched_llm_client.return_value = raw
        chat_resp = _ollama_chat_response(raw)

        for cls in (KnowledgeGraphService, KGRetrieval):
            instance = cls(MagicMock())
            instance._ollama_client = MagicMock()
            instance._ollama_client.chat = AsyncMock(return_value=chat_resp)
            result = await instance._extract_query_entities("any query", lang="en")
            assert result == ["Anna", "Bob"]


class TestRouting:
    """
    Lane C: KnowledgeGraphService.get_relevant_context unconditionally
    delegates to KGRetrieval. The legacy CIRCLES_USE_NEW_KG flag is
    retained on the settings model for back-compat but is now a no-op.
    """

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_always_routes_to_kg_retrieval(self):
        db = MagicMock()
        service = KnowledgeGraphService(db)
        sentinel = "WISSENSGRAPH:\n- Anna lives_in Hamburg"

        with patch(
            "services.kg_retrieval.KGRetrieval.get_relevant_context",
            new=AsyncMock(return_value=sentinel),
        ) as ret_get:
            result = await service.get_relevant_context(
                "Where does Anna live?", user_id=42, user_role="family", lang="de"
            )

        ret_get.assert_called_once()
        call_kwargs = ret_get.call_args
        assert call_kwargs.kwargs.get("user_id") == 42
        assert call_kwargs.kwargs.get("user_role") == "family"
        assert call_kwargs.kwargs.get("lang") == "de"
        assert result == sentinel



class TestKGRetrievalSurface:
    @pytest.mark.unit
    def test_required_methods_present(self):
        required = {
            "get_relevant_context",
            "_extract_query_entities",
            "_get_embedding",
            "_get_ollama_client",
        }
        actual = {name for name in dir(KGRetrieval) if not name.startswith("__")}
        missing = required - actual
        assert not missing, f"KGRetrieval is missing extracted methods: {missing}"
