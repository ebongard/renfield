"""Tests for OllamaService streaming methods.

Tests chat_stream() and chat_stream_with_rag() with mocked ollama client.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.ollama_service import OllamaService

# ============================================================================
# Helpers
# ============================================================================

def _make_chunk(content: str):
    """Create a mock ollama chat chunk with Pydantic-style attributes."""
    chunk = MagicMock()
    chunk.message = MagicMock()
    chunk.message.content = content
    return chunk


def _make_empty_chunk():
    """Create a chunk with no content (e.g. final chunk)."""
    chunk = MagicMock()
    chunk.message = MagicMock()
    chunk.message.content = ""
    return chunk


def _make_none_message_chunk():
    """Create a chunk with message=None."""
    chunk = MagicMock()
    chunk.message = None
    return chunk


async def _async_iter(items):
    """Helper to create an async iterable from a list."""
    for item in items:
        yield item


# ============================================================================
# chat_stream Tests
# ============================================================================

@pytest.mark.backend
class TestChatStream:
    """Tests for OllamaService.chat_stream()."""

    @patch("services.ollama_service.get_default_client")
    @patch("services.ollama_service.settings")
    @patch("services.ollama_service.llm_circuit_breaker", new_callable=AsyncMock)
    async def test_basic_streaming(self, mock_cb, mock_settings, mock_get_client):
        mock_cb.allow_request.return_value = True
        mock_settings.ollama_model = "test-model"
        mock_settings.ollama_chat_model = "test-model"
        mock_settings.ollama_rag_model = "test-model"
        mock_settings.ollama_embed_model = "test-embed"
        mock_settings.ollama_intent_model = "test-model"
        mock_settings.ollama_num_ctx = 4096
        mock_settings.default_language = "de"

        client = AsyncMock()
        chunks = [_make_chunk("Hello "), _make_chunk("world")]
        client.chat.return_value = _async_iter(chunks)
        mock_get_client.return_value = client

        service = OllamaService()
        collected = []
        async for chunk in service.chat_stream("Test message"):
            collected.append(chunk)

        assert collected == ["Hello ", "world"]
        mock_cb.record_success.assert_called_once()

    @patch("services.ollama_service.get_default_client")
    @patch("services.ollama_service.settings")
    @patch("services.ollama_service.llm_circuit_breaker", new_callable=AsyncMock)
    async def test_streaming_skips_empty_chunks(self, mock_cb, mock_settings, mock_get_client):
        mock_cb.allow_request.return_value = True
        mock_settings.ollama_model = "test-model"
        mock_settings.ollama_chat_model = "test-model"
        mock_settings.ollama_rag_model = "test-model"
        mock_settings.ollama_embed_model = "test-embed"
        mock_settings.ollama_intent_model = "test-model"
        mock_settings.ollama_num_ctx = 4096
        mock_settings.default_language = "de"

        client = AsyncMock()
        chunks = [_make_chunk("data"), _make_empty_chunk(), _make_none_message_chunk()]
        client.chat.return_value = _async_iter(chunks)
        mock_get_client.return_value = client

        service = OllamaService()
        collected = []
        async for chunk in service.chat_stream("msg"):
            collected.append(chunk)

        assert collected == ["data"]

    @patch("services.ollama_service.get_default_client")
    @patch("services.ollama_service.settings")
    @patch("services.ollama_service.llm_circuit_breaker", new_callable=AsyncMock)
    async def test_streaming_with_history(self, mock_cb, mock_settings, mock_get_client):
        mock_cb.allow_request.return_value = True
        mock_settings.ollama_model = "test-model"
        mock_settings.ollama_chat_model = "test-model"
        mock_settings.ollama_rag_model = "test-model"
        mock_settings.ollama_embed_model = "test-embed"
        mock_settings.ollama_intent_model = "test-model"
        mock_settings.ollama_num_ctx = 4096
        mock_settings.default_language = "de"

        client = AsyncMock()
        chunks = [_make_chunk("response")]
        client.chat.return_value = _async_iter(chunks)
        mock_get_client.return_value = client

        service = OllamaService()
        history = [
            {"role": "user", "content": "prev msg"},
            {"role": "assistant", "content": "prev response"},
        ]
        collected = []
        async for chunk in service.chat_stream("new msg", history=history):
            collected.append(chunk)

        assert collected == ["response"]
        # Verify messages passed to client include history
        call_kwargs = client.chat.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
        # system + 2 history + user = 4
        assert len(messages) == 4

    @patch("services.ollama_service.get_default_client")
    @patch("services.ollama_service.settings")
    @patch("services.ollama_service.llm_circuit_breaker", new_callable=AsyncMock)
    async def test_circuit_breaker_open(self, mock_cb, mock_settings, mock_get_client):
        mock_cb.allow_request.return_value = False
        mock_settings.ollama_model = "test-model"
        mock_settings.ollama_chat_model = "test-model"
        mock_settings.ollama_rag_model = "test-model"
        mock_settings.ollama_embed_model = "test-embed"
        mock_settings.ollama_intent_model = "test-model"
        mock_settings.ollama_num_ctx = 4096
        mock_settings.default_language = "de"
        mock_get_client.return_value = AsyncMock()

        service = OllamaService()
        collected = []
        async for chunk in service.chat_stream("msg"):
            collected.append(chunk)

        # Should yield a single error message
        assert len(collected) == 1
        assert collected[0]  # Non-empty fallback

    @patch("services.ollama_service.get_default_client")
    @patch("services.ollama_service.settings")
    @patch("services.ollama_service.llm_circuit_breaker", new_callable=AsyncMock)
    async def test_streaming_error_handling(self, mock_cb, mock_settings, mock_get_client):
        mock_cb.allow_request.return_value = True
        mock_settings.ollama_model = "test-model"
        mock_settings.ollama_chat_model = "test-model"
        mock_settings.ollama_rag_model = "test-model"
        mock_settings.ollama_embed_model = "test-embed"
        mock_settings.ollama_intent_model = "test-model"
        mock_settings.ollama_num_ctx = 4096
        mock_settings.default_language = "de"

        client = AsyncMock()
        client.chat.side_effect = Exception("Connection failed")
        mock_get_client.return_value = client

        service = OllamaService()
        collected = []
        async for chunk in service.chat_stream("msg"):
            collected.append(chunk)

        assert len(collected) == 1
        mock_cb.record_failure.assert_called_once()

    @patch("services.ollama_service.get_default_client")
    @patch("services.ollama_service.settings")
    @patch("services.ollama_service.llm_circuit_breaker", new_callable=AsyncMock)
    async def test_streaming_with_memory_context(self, mock_cb, mock_settings, mock_get_client):
        mock_cb.allow_request.return_value = True
        mock_settings.ollama_model = "test-model"
        mock_settings.ollama_chat_model = "test-model"
        mock_settings.ollama_rag_model = "test-model"
        mock_settings.ollama_embed_model = "test-embed"
        mock_settings.ollama_intent_model = "test-model"
        mock_settings.ollama_num_ctx = 4096
        mock_settings.default_language = "de"

        client = AsyncMock()
        chunks = [_make_chunk("ok")]
        client.chat.return_value = _async_iter(chunks)
        mock_get_client.return_value = client

        service = OllamaService()
        collected = []
        async for chunk in service.chat_stream("msg", memory_context="User likes coffee"):
            collected.append(chunk)

        assert collected == ["ok"]
        # System prompt should include memory context
        call_kwargs = client.chat.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
        system_content = messages[0]["content"]
        assert "User likes coffee" in system_content


# ============================================================================
# chat_stream_with_rag Tests
# ============================================================================

@pytest.mark.backend
class TestChatStreamWithRag:
    """Tests for OllamaService.chat_stream_with_rag()."""

    @patch("services.ollama_service.get_default_client")
    @patch("services.ollama_service.settings")
    async def test_rag_streaming(self, mock_settings, mock_get_client):
        mock_settings.ollama_model = "test-model"
        mock_settings.ollama_chat_model = "chat-model"
        mock_settings.ollama_rag_model = "rag-model"
        mock_settings.ollama_embed_model = "test-embed"
        mock_settings.ollama_intent_model = "test-model"
        mock_settings.ollama_num_ctx = 4096
        mock_settings.default_language = "de"

        client = AsyncMock()
        chunks = [_make_chunk("Based on "), _make_chunk("the docs")]
        client.chat.return_value = _async_iter(chunks)
        mock_get_client.return_value = client

        service = OllamaService()
        collected = []
        async for chunk in service.chat_stream_with_rag("What is X?", rag_context="X is a thing"):
            collected.append(chunk)

        assert collected == ["Based on ", "the docs"]
        # Should use rag_model when context is provided
        call_kwargs = client.chat.call_args
        model_used = call_kwargs.kwargs.get("model") or call_kwargs[1].get("model")
        assert model_used == "rag-model"

    @patch("services.ollama_service.get_default_client")
    @patch("services.ollama_service.settings")
    async def test_rag_streaming_no_context_uses_chat_model(self, mock_settings, mock_get_client):
        mock_settings.ollama_model = "test-model"
        mock_settings.ollama_chat_model = "chat-model"
        mock_settings.ollama_rag_model = "rag-model"
        mock_settings.ollama_embed_model = "test-embed"
        mock_settings.ollama_intent_model = "test-model"
        mock_settings.ollama_num_ctx = 4096
        mock_settings.default_language = "de"

        client = AsyncMock()
        chunks = [_make_chunk("plain")]
        client.chat.return_value = _async_iter(chunks)
        mock_get_client.return_value = client

        service = OllamaService()
        collected = []
        async for chunk in service.chat_stream_with_rag("What?", rag_context=None):
            collected.append(chunk)

        assert collected == ["plain"]
        call_kwargs = client.chat.call_args
        model_used = call_kwargs.kwargs.get("model") or call_kwargs[1].get("model")
        assert model_used == "chat-model"

    @patch("services.ollama_service.get_default_client")
    @patch("services.ollama_service.settings")
    async def test_rag_streaming_includes_context_in_prompt(self, mock_settings, mock_get_client):
        mock_settings.ollama_model = "test-model"
        mock_settings.ollama_chat_model = "chat-model"
        mock_settings.ollama_rag_model = "rag-model"
        mock_settings.ollama_embed_model = "test-embed"
        mock_settings.ollama_intent_model = "test-model"
        mock_settings.ollama_num_ctx = 4096
        mock_settings.default_language = "de"

        client = AsyncMock()
        chunks = [_make_chunk("answer")]
        client.chat.return_value = _async_iter(chunks)
        mock_get_client.return_value = client

        service = OllamaService()
        async for _ in service.chat_stream_with_rag("Q?", rag_context="The document says XYZ"):
            pass

        call_kwargs = client.chat.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
        system_content = messages[0]["content"]
        assert "The document says XYZ" in system_content

    @patch("services.ollama_service.get_default_client")
    @patch("services.ollama_service.settings")
    async def test_rag_streaming_with_history(self, mock_settings, mock_get_client):
        mock_settings.ollama_model = "test-model"
        mock_settings.ollama_chat_model = "chat-model"
        mock_settings.ollama_rag_model = "rag-model"
        mock_settings.ollama_embed_model = "test-embed"
        mock_settings.ollama_intent_model = "test-model"
        mock_settings.ollama_num_ctx = 4096
        mock_settings.default_language = "de"

        client = AsyncMock()
        chunks = [_make_chunk("ok")]
        client.chat.return_value = _async_iter(chunks)
        mock_get_client.return_value = client

        service = OllamaService()
        history = [{"role": "user", "content": "prev"}]
        async for _ in service.chat_stream_with_rag("Q?", rag_context="ctx", history=history):
            pass

        call_kwargs = client.chat.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
        # system + 1 history + user = 3
        assert len(messages) == 3

    @patch("services.ollama_service.get_default_client")
    @patch("services.ollama_service.settings")
    async def test_rag_streaming_error_yields_fallback(self, mock_settings, mock_get_client):
        mock_settings.ollama_model = "test-model"
        mock_settings.ollama_chat_model = "chat-model"
        mock_settings.ollama_rag_model = "rag-model"
        mock_settings.ollama_embed_model = "test-embed"
        mock_settings.ollama_intent_model = "test-model"
        mock_settings.ollama_num_ctx = 4096
        mock_settings.default_language = "de"

        client = AsyncMock()
        client.chat.side_effect = Exception("LLM down")
        mock_get_client.return_value = client

        service = OllamaService()
        collected = []
        async for chunk in service.chat_stream_with_rag("Q?", rag_context="ctx"):
            collected.append(chunk)

        assert len(collected) == 1
        # Should contain error info

    @patch("services.ollama_service.get_default_client")
    @patch("services.ollama_service.settings")
    async def test_rag_streaming_with_memory_context(self, mock_settings, mock_get_client):
        mock_settings.ollama_model = "test-model"
        mock_settings.ollama_chat_model = "chat-model"
        mock_settings.ollama_rag_model = "rag-model"
        mock_settings.ollama_embed_model = "test-embed"
        mock_settings.ollama_intent_model = "test-model"
        mock_settings.ollama_num_ctx = 4096
        mock_settings.default_language = "de"

        client = AsyncMock()
        chunks = [_make_chunk("ok")]
        client.chat.return_value = _async_iter(chunks)
        mock_get_client.return_value = client

        service = OllamaService()
        async for _ in service.chat_stream_with_rag(
            "Q?", rag_context="doc ctx", memory_context="User prefers German"
        ):
            pass

        call_kwargs = client.chat.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
        system_content = messages[0]["content"]
        assert "User prefers German" in system_content
        assert "doc ctx" in system_content
