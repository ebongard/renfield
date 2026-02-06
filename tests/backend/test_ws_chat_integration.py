"""Tests for WebSocket chat handler integration.

Tests ConversationSessionState, _stream_rag_response, is_followup_question,
and helper functions from chat_handler and shared modules.

NOTE: Imports from api.websocket trigger services.database which needs asyncpg.
We mock asyncpg in sys.modules before importing to allow local test execution.
"""
import sys
from unittest.mock import MagicMock

# Pre-mock modules that are not installed locally but are imported transitively
# by the api.websocket package (asyncpg, whisper, piper, speechbrain, etc.).
_missing_stubs = [
    "asyncpg", "whisper", "piper", "piper.voice", "speechbrain",
    "speechbrain.inference", "speechbrain.inference.speaker",
    "openwakeword", "openwakeword.model",
]
for _mod in _missing_stubs:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from time import time
from unittest.mock import AsyncMock, patch

import pytest

# ============================================================================
# ConversationSessionState Tests
# ============================================================================

@pytest.mark.backend
class TestConversationSessionState:
    """Tests for ConversationSessionState dataclass."""

    def _get_class(self):
        from api.websocket.shared import ConversationSessionState
        return ConversationSessionState

    def test_init_defaults(self):
        cls = self._get_class()
        state = cls()
        assert state.conversation_history == []
        assert state.history_loaded is False
        assert state.db_session_id is None
        assert state.last_rag_context is None
        assert state.last_query is None
        assert state.last_intent is None
        assert state.last_action_result is None
        assert state.last_entities == []

    def test_add_to_history(self):
        cls = self._get_class()
        state = cls()
        state.add_to_history("user", "Hello")
        state.add_to_history("assistant", "Hi there")
        assert len(state.conversation_history) == 2
        assert state.conversation_history[0] == {"role": "user", "content": "Hello"}
        assert state.conversation_history[1] == {"role": "assistant", "content": "Hi there"}

    def test_add_to_history_truncates_at_max(self):
        cls = self._get_class()
        state = cls()
        state.MAX_HISTORY_MESSAGES = 4
        for i in range(6):
            state.add_to_history("user", f"msg {i}")
        assert len(state.conversation_history) == 4
        # Should keep the most recent 4
        assert state.conversation_history[0]["content"] == "msg 2"
        assert state.conversation_history[-1]["content"] == "msg 5"

    def test_rag_context_valid_when_fresh(self):
        cls = self._get_class()
        state = cls()
        state.update_rag_context("some context", [{"id": 1}], "query", kb_id=1)
        assert state.is_rag_context_valid() is True

    def test_rag_context_invalid_when_empty(self):
        cls = self._get_class()
        state = cls()
        assert state.is_rag_context_valid() is False

    def test_rag_context_invalid_when_expired(self):
        cls = self._get_class()
        state = cls()
        state.last_rag_context = "old context"
        state.last_rag_timestamp = time() - 600  # 10 minutes ago
        assert state.is_rag_context_valid() is False

    def test_update_rag_context(self):
        cls = self._get_class()
        state = cls()
        results = [{"id": 1, "score": 0.9}]
        state.update_rag_context("doc content", results, "my query", kb_id=42)
        assert state.last_rag_context == "doc content"
        assert state.last_rag_results == results
        assert state.last_query == "my query"
        assert state.knowledge_base_id == 42
        assert state.last_rag_timestamp > 0

    def test_update_action_context(self):
        cls = self._get_class()
        state = cls()
        intent = {"intent": "homeassistant.turn_on", "parameters": {"entity_id": "light.kitchen"}}
        result = {"success": True}
        state.update_action_context(intent, result)
        assert state.last_intent == intent
        assert state.last_action_result == result
        assert state.last_entities == ["light.kitchen"]

    def test_update_action_context_no_entity(self):
        cls = self._get_class()
        state = cls()
        intent = {"intent": "general.conversation", "parameters": {}}
        result = {"success": True}
        state.update_action_context(intent, result)
        assert state.last_entities == []

    def test_clear_rag(self):
        cls = self._get_class()
        state = cls()
        state.update_rag_context("ctx", [{"id": 1}], "q", kb_id=1)
        state.clear_rag()
        assert state.last_rag_context is None
        assert state.last_rag_results is None
        assert state.last_query is None
        assert state.last_rag_timestamp == 0
        assert state.knowledge_base_id is None

    def test_clear_all(self):
        cls = self._get_class()
        state = cls()
        state.add_to_history("user", "msg")
        state.history_loaded = True
        state.update_rag_context("ctx", [], "q")
        state.update_action_context({"intent": "x", "parameters": {"entity_id": "e"}}, {})
        state.clear_all()
        assert state.conversation_history == []
        assert state.history_loaded is False
        assert state.last_rag_context is None
        assert state.last_intent is None
        assert state.last_entities == []


# ============================================================================
# is_followup_question Tests
# ============================================================================

@pytest.mark.backend
class TestIsFollowupQuestion:
    """Tests for is_followup_question helper."""

    def _get_func(self):
        from api.websocket.shared import is_followup_question
        return is_followup_question

    def test_short_query_is_followup(self):
        assert self._get_func()("was ist das") is True

    def test_very_short_query_is_followup(self):
        assert self._get_func()("ja") is True

    def test_demonstrative_pronoun_is_followup(self):
        assert self._get_func()("Kannst du das nochmal erklären bitte") is True

    def test_continuation_word_is_followup(self):
        assert self._get_func()("Zeig mir noch mehr davon") is True

    def test_conjunction_start_is_followup(self):
        assert self._get_func()("und was passiert dann mit dem Licht") is True

    def test_prepositional_pronoun_is_followup(self):
        assert self._get_func()("Erzähl mir mehr darüber bitte jetzt") is True

    def test_detail_request_is_followup(self):
        assert self._get_func()("Kannst du genauer beschreiben wie das funktioniert") is True

    def test_independent_long_query_not_followup(self):
        # No pronouns, no continuation words, long enough
        assert self._get_func()("Wie wird morgen Nachmittag in Berlin Wetter sein") is False

    def test_topic_overlap_is_followup(self):
        fn = self._get_func()
        prev = "Wie wird das Wetter morgen"
        assert fn("Wie wird das Wetter morgen Nachmittag", prev) is True

    def test_completely_different_topic_not_followup(self):
        fn = self._get_func()
        prev = "Schalte das Licht ein"
        assert fn("Erstelle eine Einkaufsliste fuer morgen Abend bitte", prev) is False


# ============================================================================
# _parse_mcp_raw_data Tests
# ============================================================================

@pytest.mark.backend
class TestParseMcpRawData:
    """Tests for _parse_mcp_raw_data helper."""

    def _get_func(self):
        from api.websocket.chat_handler import _parse_mcp_raw_data
        return _parse_mcp_raw_data

    def test_valid_mcp_format(self):
        fn = self._get_func()
        data = [{"type": "text", "text": '{"results": [1, 2, 3]}'}]
        result = fn(data)
        assert result == {"results": [1, 2, 3]}

    def test_none_input(self):
        assert self._get_func()(None) is None

    def test_empty_list(self):
        assert self._get_func()([]) is None

    def test_non_mcp_format(self):
        data = [{"foo": "bar"}]
        assert self._get_func()(data) is None

    def test_non_json_text_long(self):
        fn = self._get_func()
        data = [{"type": "text", "text": "a" * 100}]
        result = fn(data)
        assert result is not None
        assert "text_summary" in result

    def test_non_json_text_short(self):
        fn = self._get_func()
        data = [{"type": "text", "text": "short"}]
        result = fn(data)
        assert result is None


# ============================================================================
# _build_agent_action_result Tests
# ============================================================================

@pytest.mark.backend
class TestBuildAgentActionResult:
    """Tests for _build_agent_action_result helper."""

    def _get_func(self):
        from api.websocket.chat_handler import _build_agent_action_result
        return _build_agent_action_result

    def test_empty_results(self):
        assert self._get_func()([]) is None

    def test_single_result(self):
        fn = self._get_func()
        tool_results = [("search_docs", {"results": [1, 2]})]
        result = fn(tool_results)
        assert result["success"] is True
        assert result["data"] == {"results": [1, 2]}
        assert result["_agent_intent"] == "search_docs"

    def test_prefers_search_over_send(self):
        fn = self._get_func()
        tool_results = [
            ("send_email", {"sent": True}),
            ("search_documents", {"results": ["doc1"]}),
        ]
        result = fn(tool_results)
        assert result["_agent_intent"] == "search_documents"

    def test_none_data_skipped(self):
        fn = self._get_func()
        tool_results = [("tool1", None), ("tool2", {"ok": True})]
        result = fn(tool_results)
        assert result["_agent_intent"] == "tool2"


# ============================================================================
# _build_action_summary Tests
# ============================================================================

@pytest.mark.backend
class TestBuildActionSummary:
    """Tests for _build_action_summary helper."""

    def _get_func(self):
        from api.websocket.chat_handler import _build_action_summary
        return _build_action_summary

    def test_no_data(self):
        fn = self._get_func()
        intent = {"intent": "test"}
        result = fn(intent, {"success": True, "data": None})
        assert result == ""

    def test_dict_with_results_list(self):
        fn = self._get_func()
        intent = {"intent": "mcp.search"}
        action_result = {
            "success": True,
            "data": {"results": [{"id": 1, "title": "Doc A"}]}
        }
        summary = fn(intent, action_result)
        assert "mcp.search" in summary
        assert "id=1" in summary
        assert "title=Doc A" in summary

    def test_simple_dict(self):
        fn = self._get_func()
        intent = {"intent": "mcp.weather"}
        action_result = {"success": True, "data": {"temp": 20, "city": "Berlin"}}
        summary = fn(intent, action_result)
        assert "mcp.weather" in summary

    def test_list_items(self):
        fn = self._get_func()
        intent = {"intent": "mcp.list"}
        items = [{"id": i, "name": f"item{i}"} for i in range(3)]
        summary = fn(intent, {"success": True, "data": items})
        assert "3 Ergebnisse" in summary
        assert "id=0" in summary

    def test_max_chars_truncation(self):
        fn = self._get_func()
        intent = {"intent": "test"}
        data = {"key": "x" * 5000}
        summary = fn(intent, {"success": True, "data": data}, max_chars=200)
        # max_chars limits the JSON portion; intent prefix is added separately
        assert len(summary) < 5000

    def test_none_intent(self):
        fn = self._get_func()
        summary = fn(None, {"success": True, "data": {"foo": 1}})
        assert summary != ""


# ============================================================================
# _stream_rag_response Tests
# ============================================================================

@pytest.mark.backend
class TestStreamRagResponse:
    """Tests for _stream_rag_response function."""

    async def _make_async_gen(self, chunks):
        """Helper to create an async generator from a list of chunks."""
        for chunk in chunks:
            yield chunk

    @patch("api.websocket.chat_handler.settings")
    async def test_rag_disabled_falls_back_to_plain_chat(self, mock_settings):
        from api.websocket.chat_handler import _stream_rag_response
        from api.websocket.shared import ConversationSessionState

        mock_settings.rag_enabled = False

        ollama = AsyncMock()
        ollama.chat_stream = MagicMock(return_value=self._make_async_gen(["Hello ", "world"]))

        ws = AsyncMock()
        session_state = ConversationSessionState()

        result = await _stream_rag_response(
            "test query", None, ollama, session_state, ws
        )
        assert result == "Hello world"
        # Should have sent stream messages
        assert ws.send_json.call_count == 2

    @patch("api.websocket.chat_handler.settings")
    @patch("api.websocket.chat_handler.AsyncSessionLocal")
    async def test_rag_no_results_falls_back(self, mock_session_local, mock_settings):
        from api.websocket.chat_handler import _stream_rag_response
        from api.websocket.shared import ConversationSessionState

        mock_settings.rag_enabled = True

        # Mock RAG service returning no results
        mock_rag = AsyncMock()
        mock_rag.search.return_value = []

        mock_db = AsyncMock()
        mock_session_local.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_local.return_value.__aexit__ = AsyncMock(return_value=False)

        ollama = AsyncMock()
        ollama.chat_stream = MagicMock(return_value=self._make_async_gen(["fallback"]))

        ws = AsyncMock()
        session_state = ConversationSessionState()

        with patch("services.rag_service.RAGService", return_value=mock_rag):
            result = await _stream_rag_response(
                "test query", 1, ollama, session_state, ws
            )

        assert result == "fallback"
        # Should have sent rag_context with has_context=False
        rag_ctx_calls = [
            c for c in ws.send_json.call_args_list
            if c.args[0].get("type") == "rag_context"
        ]
        assert len(rag_ctx_calls) == 1
        assert rag_ctx_calls[0].args[0]["has_context"] is False

    @patch("api.websocket.chat_handler.settings")
    @patch("api.websocket.chat_handler.AsyncSessionLocal")
    async def test_rag_with_results_streams_rag_response(self, mock_session_local, mock_settings):
        from api.websocket.chat_handler import _stream_rag_response
        from api.websocket.shared import ConversationSessionState

        mock_settings.rag_enabled = True

        mock_rag = AsyncMock()
        mock_rag.search.return_value = [{"chunk": {"id": 1, "content": "doc text", "page_number": None, "section_title": None}, "document": {"filename": "test.pdf"}, "similarity": 0.9}]
        mock_rag.format_context_from_results = MagicMock(return_value="Document context here")

        mock_db = AsyncMock()
        mock_session_local.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_local.return_value.__aexit__ = AsyncMock(return_value=False)

        ollama = AsyncMock()
        ollama.chat_stream_with_rag = MagicMock(
            return_value=self._make_async_gen(["RAG ", "answer"])
        )

        ws = AsyncMock()
        session_state = ConversationSessionState()

        with patch("services.rag_service.RAGService", return_value=mock_rag):
            result = await _stream_rag_response(
                "test query", 1, ollama, session_state, ws
            )

        assert result == "RAG answer"
        # Should have sent rag_context with has_context=True
        rag_ctx_calls = [
            c for c in ws.send_json.call_args_list
            if c.args[0].get("type") == "rag_context"
        ]
        assert len(rag_ctx_calls) == 1
        assert rag_ctx_calls[0].args[0]["has_context"] is True
        # History should be updated
        assert len(session_state.conversation_history) == 2

    @patch("api.websocket.chat_handler.settings")
    async def test_rag_followup_uses_cached_context(self, mock_settings):
        from api.websocket.chat_handler import _stream_rag_response
        from api.websocket.shared import ConversationSessionState

        mock_settings.rag_enabled = True

        ollama = AsyncMock()
        ollama.chat_stream_with_rag = MagicMock(
            return_value=self._make_async_gen(["cached ", "answer"])
        )

        ws = AsyncMock()
        session_state = ConversationSessionState()
        # Pre-populate RAG cache
        session_state.update_rag_context("cached doc context", [{"id": 1}], "original query", kb_id=1)

        # Short follow-up query that will be detected as follow-up
        result = await _stream_rag_response(
            "und dann?", 1, ollama, session_state, ws
        )

        assert result == "cached answer"

    @patch("api.websocket.chat_handler.settings")
    async def test_rag_exception_falls_back_to_plain(self, mock_settings):
        from api.websocket.chat_handler import _stream_rag_response
        from api.websocket.shared import ConversationSessionState

        mock_settings.rag_enabled = True

        ollama = AsyncMock()
        ollama.chat_stream = MagicMock(return_value=self._make_async_gen(["error ", "fallback"]))

        ws = AsyncMock()
        session_state = ConversationSessionState()

        # Patch RAGService import to raise
        with patch("services.rag_service.RAGService", side_effect=Exception("RAG broken")), \
             patch("api.websocket.chat_handler.AsyncSessionLocal") as mock_sl:
                mock_db = AsyncMock()
                mock_sl.return_value.__aenter__ = AsyncMock(return_value=mock_db)
                mock_sl.return_value.__aexit__ = AsyncMock(return_value=False)

                result = await _stream_rag_response(
                    "test", 1, ollama, session_state, ws
                )

        assert result == "error fallback"
