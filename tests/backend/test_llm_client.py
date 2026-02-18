"""
Tests for utils/llm_client.py — LLM Client Protocol + Factory.

Tests:
- Protocol structural typing (positive + negative)
- Factory: client creation, URL-based caching, cache clearing
- Agent client: URL priority resolution (role → fallback → default)
"""
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure 'ollama' module is available even when the package isn't installed.
# create_llm_client() does `import ollama` internally, so we provide a stub.
if "ollama" not in sys.modules:
    _ollama_stub = MagicMock()
    _ollama_stub.AsyncClient = MagicMock()
    sys.modules["ollama"] = _ollama_stub

from utils.llm_client import (
    LLMClient,
    clear_client_cache,
    create_llm_client,
    extract_response_content,
    get_agent_client,
    get_classification_chat_kwargs,
    get_default_client,
    get_embed_client,
    is_thinking_model,
)


@pytest.fixture(autouse=True)
def _clean_cache():
    """Ensure a clean client cache for every test."""
    clear_client_cache()
    yield
    clear_client_cache()


# ============================================================================
# Protocol Tests
# ============================================================================

class TestLLMClientProtocol:
    """Tests for the LLMClient runtime-checkable protocol."""

    @pytest.mark.unit
    def test_mock_with_chat_and_embeddings_satisfies_protocol(self):
        """An object with chat() and embeddings() async methods satisfies LLMClient."""
        mock = MagicMock()
        mock.chat = AsyncMock()
        mock.embeddings = AsyncMock()
        assert isinstance(mock, LLMClient)

    @pytest.mark.unit
    def test_object_without_methods_does_not_satisfy_protocol(self):
        """A plain object without chat/embeddings does NOT satisfy LLMClient."""
        assert not isinstance(object(), LLMClient)

    @pytest.mark.unit
    def test_object_with_only_chat_does_not_satisfy_protocol(self):
        """An object with only chat() is not enough."""
        mock = MagicMock(spec=["chat"])
        mock.chat = AsyncMock()
        assert not isinstance(mock, LLMClient)


# ============================================================================
# Factory Tests
# ============================================================================

class TestCreateLLMClient:
    """Tests for create_llm_client() factory function."""

    @pytest.mark.unit
    @patch("ollama.AsyncClient")
    def test_creates_client_for_url(self, mock_cls):
        """create_llm_client creates an ollama.AsyncClient with the given host."""
        sentinel = MagicMock()
        mock_cls.return_value = sentinel

        result = create_llm_client("http://localhost:11434")

        # Client is created with host + explicit httpx.Timeout kwargs
        args, kwargs = mock_cls.call_args
        assert kwargs.get("host") == "http://localhost:11434"
        assert "timeout" in kwargs
        assert result is sentinel

    @pytest.mark.unit
    @patch("ollama.AsyncClient")
    def test_caches_by_url(self, mock_cls):
        """Same URL returns the same client instance (cached)."""
        sentinel = MagicMock()
        mock_cls.return_value = sentinel

        first = create_llm_client("http://host:11434")
        second = create_llm_client("http://host:11434")

        assert first is second
        assert mock_cls.call_count == 1

    @pytest.mark.unit
    @patch("ollama.AsyncClient")
    def test_normalizes_trailing_slash(self, mock_cls):
        """URLs with/without trailing slash map to the same cache entry."""
        sentinel = MagicMock()
        mock_cls.return_value = sentinel

        first = create_llm_client("http://host:11434/")
        second = create_llm_client("http://host:11434")

        assert first is second
        assert mock_cls.call_count == 1

    @pytest.mark.unit
    @patch("ollama.AsyncClient")
    def test_different_urls_get_different_clients(self, mock_cls):
        """Different URLs create separate client instances."""
        mock_cls.side_effect = [MagicMock(), MagicMock()]

        a = create_llm_client("http://host-a:11434")
        b = create_llm_client("http://host-b:11434")

        assert a is not b
        assert mock_cls.call_count == 2


class TestClearClientCache:
    """Tests for clear_client_cache()."""

    @pytest.mark.unit
    @patch("ollama.AsyncClient")
    def test_clear_cache_forces_new_client(self, mock_cls):
        """After clearing the cache, the same URL creates a new client."""
        mock_cls.side_effect = [MagicMock(), MagicMock()]

        first = create_llm_client("http://host:11434")
        clear_client_cache()
        second = create_llm_client("http://host:11434")

        assert first is not second
        assert mock_cls.call_count == 2


# ============================================================================
# get_default_client Tests
# ============================================================================

class TestGetDefaultClient:
    """Tests for get_default_client()."""

    @pytest.mark.unit
    @patch("ollama.AsyncClient")
    @patch("utils.llm_client.settings")
    def test_uses_settings_ollama_url(self, mock_settings, mock_cls):
        """get_default_client() creates a client for settings.ollama_url."""
        mock_settings.ollama_url = "http://my-ollama:11434"
        mock_settings.ollama_fallback_url = ""  # no fallback
        mock_settings.ollama_connect_timeout = 10.0
        mock_settings.ollama_read_timeout = 300.0
        sentinel = MagicMock()
        mock_cls.return_value = sentinel

        result = get_default_client()

        args, kwargs = mock_cls.call_args
        assert kwargs.get("host") == "http://my-ollama:11434"
        assert result is sentinel

    @pytest.mark.unit
    @patch("ollama.AsyncClient")
    @patch("utils.llm_client.settings")
    def test_returns_fallback_wrapper_when_fallback_url_configured(self, mock_settings, mock_cls):
        """When OLLAMA_FALLBACK_URL is set, get_default_client returns a _FallbackLLMClient."""
        from utils.llm_client import _FallbackLLMClient

        mock_settings.ollama_url = "http://cuda.local:11434"
        mock_settings.ollama_fallback_url = "http://host.docker.internal:11434"
        mock_settings.ollama_connect_timeout = 10.0
        mock_settings.ollama_read_timeout = 300.0
        mock_cls.return_value = MagicMock()

        result = get_default_client()

        assert isinstance(result, _FallbackLLMClient)
        # Two clients created: primary + fallback
        assert mock_cls.call_count == 2
        hosts = [call.kwargs["host"] for call in mock_cls.call_args_list]
        assert "http://cuda.local:11434" in hosts
        assert "http://host.docker.internal:11434" in hosts

    @pytest.mark.unit
    @patch("ollama.AsyncClient")
    @patch("utils.llm_client.settings")
    def test_no_fallback_wrapper_when_same_url(self, mock_settings, mock_cls):
        """No _FallbackLLMClient when fallback URL equals primary URL."""
        from utils.llm_client import _FallbackLLMClient

        mock_settings.ollama_url = "http://cuda.local:11434"
        mock_settings.ollama_fallback_url = "http://cuda.local:11434"
        mock_settings.ollama_connect_timeout = 10.0
        mock_settings.ollama_read_timeout = 300.0
        mock_cls.return_value = MagicMock()

        result = get_default_client()

        assert not isinstance(result, _FallbackLLMClient)


# ============================================================================
# get_embed_client Tests
# ============================================================================

class TestGetEmbedClient:
    """Tests for get_embed_client() — separate embedding Ollama instance."""

    @pytest.mark.unit
    @patch("ollama.AsyncClient")
    @patch("utils.llm_client.settings")
    def test_uses_embed_url_when_configured(self, mock_settings, mock_cls):
        """get_embed_client() creates a client for settings.ollama_embed_url."""
        mock_settings.ollama_embed_url = "http://embed-host:11434"
        mock_settings.ollama_fallback_url = ""
        mock_settings.ollama_connect_timeout = 10.0
        mock_settings.ollama_read_timeout = 300.0
        sentinel = MagicMock()
        mock_cls.return_value = sentinel

        result = get_embed_client()

        args, kwargs = mock_cls.call_args
        assert kwargs.get("host") == "http://embed-host:11434"
        assert result is sentinel

    @pytest.mark.unit
    @patch("ollama.AsyncClient")
    @patch("utils.llm_client.settings")
    def test_falls_back_to_default_when_no_embed_url(self, mock_settings, mock_cls):
        """get_embed_client() uses default client when ollama_embed_url is None."""
        mock_settings.ollama_embed_url = None
        mock_settings.ollama_url = "http://default:11434"
        mock_settings.ollama_fallback_url = ""
        mock_settings.ollama_connect_timeout = 10.0
        mock_settings.ollama_read_timeout = 300.0
        sentinel = MagicMock()
        mock_cls.return_value = sentinel

        result = get_embed_client()

        args, kwargs = mock_cls.call_args
        assert kwargs.get("host") == "http://default:11434"
        assert result is sentinel

    @pytest.mark.unit
    @patch("ollama.AsyncClient")
    @patch("utils.llm_client.settings")
    def test_embed_client_gets_fallback_wrapper(self, mock_settings, mock_cls):
        """When OLLAMA_FALLBACK_URL is set, embed client also gets fallback wrapper."""
        from utils.llm_client import _FallbackLLMClient

        mock_settings.ollama_embed_url = "http://embed-host:11434"
        mock_settings.ollama_fallback_url = "http://fallback:11434"
        mock_settings.ollama_connect_timeout = 10.0
        mock_settings.ollama_read_timeout = 300.0
        mock_cls.return_value = MagicMock()

        result = get_embed_client()

        assert isinstance(result, _FallbackLLMClient)

    @pytest.mark.unit
    @patch("ollama.AsyncClient")
    @patch("utils.llm_client.settings")
    def test_empty_string_embed_url_uses_default(self, mock_settings, mock_cls):
        """Empty string ollama_embed_url falls through to default."""
        mock_settings.ollama_embed_url = ""
        mock_settings.ollama_url = "http://default:11434"
        mock_settings.ollama_fallback_url = ""
        mock_settings.ollama_connect_timeout = 10.0
        mock_settings.ollama_read_timeout = 300.0
        sentinel = MagicMock()
        mock_cls.return_value = sentinel

        result = get_embed_client()

        args, kwargs = mock_cls.call_args
        assert kwargs.get("host") == "http://default:11434"


# ============================================================================
# get_agent_client Tests
# ============================================================================

class TestGetAgentClient:
    """Tests for get_agent_client() URL resolution."""

    @pytest.mark.unit
    @patch("ollama.AsyncClient")
    @patch("utils.llm_client.settings")
    def test_role_url_has_highest_priority(self, mock_settings, mock_cls):
        """role_url wins over fallback_url and default."""
        mock_settings.ollama_url = "http://default:11434"
        mock_settings.ollama_fallback_url = ""  # no fallback
        mock_settings.ollama_connect_timeout = 10.0
        mock_settings.ollama_read_timeout = 300.0
        sentinel = MagicMock()
        mock_cls.return_value = sentinel

        client, resolved = get_agent_client(
            role_url="http://role:11434",
            fallback_url="http://fallback:11434",
        )

        assert resolved == "http://role:11434"
        assert client is sentinel

    @pytest.mark.unit
    @patch("ollama.AsyncClient")
    @patch("utils.llm_client.settings")
    def test_fallback_url_used_when_no_role_url(self, mock_settings, mock_cls):
        """fallback_url is used when role_url is None."""
        mock_settings.ollama_url = "http://default:11434"
        mock_settings.ollama_fallback_url = ""  # no fallback
        mock_settings.ollama_connect_timeout = 10.0
        mock_settings.ollama_read_timeout = 300.0
        sentinel = MagicMock()
        mock_cls.return_value = sentinel

        client, resolved = get_agent_client(
            role_url=None,
            fallback_url="http://fallback:11434",
        )

        assert resolved == "http://fallback:11434"
        assert client is sentinel

    @pytest.mark.unit
    @patch("ollama.AsyncClient")
    @patch("utils.llm_client.settings")
    def test_default_url_used_when_no_overrides(self, mock_settings, mock_cls):
        """settings.ollama_url is used when both role_url and fallback_url are None."""
        mock_settings.ollama_url = "http://default:11434"
        mock_settings.ollama_fallback_url = ""  # no fallback
        mock_settings.ollama_connect_timeout = 10.0
        mock_settings.ollama_read_timeout = 300.0
        sentinel = MagicMock()
        mock_cls.return_value = sentinel

        client, resolved = get_agent_client(role_url=None, fallback_url=None)

        assert resolved == "http://default:11434"
        assert client is sentinel

    @pytest.mark.unit
    @patch("ollama.AsyncClient")
    @patch("utils.llm_client.settings")
    def test_empty_string_fallback_treated_as_falsy(self, mock_settings, mock_cls):
        """Empty string fallback_url falls through to default."""
        mock_settings.ollama_url = "http://default:11434"
        mock_settings.ollama_fallback_url = ""  # no fallback
        mock_settings.ollama_connect_timeout = 10.0
        mock_settings.ollama_read_timeout = 300.0
        sentinel = MagicMock()
        mock_cls.return_value = sentinel

        client, resolved = get_agent_client(role_url=None, fallback_url="")

        assert resolved == "http://default:11434"
        assert client is sentinel


# ============================================================================
# Fallback Client Tests
# ============================================================================


class TestFallbackLLMClient:
    """Tests for _FallbackLLMClient transparent retry behavior."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_uses_primary_on_success(self):
        """chat() returns primary result when primary succeeds."""
        from utils.llm_client import _FallbackLLMClient

        primary = AsyncMock()
        fallback = AsyncMock()
        primary.chat.return_value = "primary_result"

        client = _FallbackLLMClient(primary, fallback, "http://fallback:11434")
        result = await client.chat(model="test", messages=[])

        assert result == "primary_result"
        fallback.chat.assert_not_called()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_retries_fallback_on_connect_error(self):
        """chat() retries on fallback when primary raises ConnectError."""
        import httpx

        from utils.llm_client import _FallbackLLMClient

        primary = AsyncMock()
        fallback = AsyncMock()
        primary.chat.side_effect = httpx.ConnectError("refused")
        fallback.chat.return_value = "fallback_result"

        client = _FallbackLLMClient(primary, fallback, "http://fallback:11434")
        result = await client.chat(model="test", messages=[])

        assert result == "fallback_result"
        fallback.chat.assert_called_once()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_retries_fallback_on_connect_timeout(self):
        """chat() retries on fallback when primary raises ConnectTimeout."""
        import httpx

        from utils.llm_client import _FallbackLLMClient

        primary = AsyncMock()
        fallback = AsyncMock()
        primary.chat.side_effect = httpx.ConnectTimeout("timed out")
        fallback.chat.return_value = "fallback_after_timeout"

        client = _FallbackLLMClient(primary, fallback, "http://fallback:11434")
        result = await client.chat(model="test", messages=[])

        assert result == "fallback_after_timeout"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_embeddings_also_falls_back(self):
        """embeddings() also uses fallback on connect error."""
        import httpx

        from utils.llm_client import _FallbackLLMClient

        primary = AsyncMock()
        fallback = AsyncMock()
        primary.embeddings.side_effect = httpx.ConnectError("refused")
        fallback.embeddings.return_value = "embed_result"

        client = _FallbackLLMClient(primary, fallback, "http://fallback:11434")
        result = await client.embeddings(model="test", prompt="hello")

        assert result == "embed_result"


# ============================================================================
# Thinking Model Detection Tests (Option C)
# ============================================================================

class TestIsThinkingModel:
    """Tests for is_thinking_model() detection."""

    @pytest.mark.unit
    def test_qwen3_base_is_thinking(self):
        """qwen3 without version tag is a thinking model."""
        assert is_thinking_model("qwen3") is True

    @pytest.mark.unit
    def test_qwen3_with_version_is_thinking(self):
        """qwen3:14b with version tag is a thinking model."""
        assert is_thinking_model("qwen3:14b") is True

    @pytest.mark.unit
    def test_qwen3_case_insensitive(self):
        """Detection is case-insensitive."""
        assert is_thinking_model("Qwen3:8b") is True
        assert is_thinking_model("QWEN3:latest") is True

    @pytest.mark.unit
    def test_qwq_is_thinking(self):
        """qwq model is a thinking model."""
        assert is_thinking_model("qwq:32b") is True

    @pytest.mark.unit
    def test_deepseek_r1_is_thinking(self):
        """deepseek-r1 is a thinking model."""
        assert is_thinking_model("deepseek-r1:latest") is True

    @pytest.mark.unit
    def test_deepseek_r1_distill_is_thinking(self):
        """deepseek-r1-distill variants are thinking models."""
        assert is_thinking_model("deepseek-r1-distill-qwen:14b") is True
        assert is_thinking_model("deepseek-r1-distill-llama:8b") is True

    @pytest.mark.unit
    def test_marco_o1_is_thinking(self):
        """marco-o1 is a thinking model."""
        assert is_thinking_model("marco-o1:7b") is True

    @pytest.mark.unit
    def test_llama_is_not_thinking(self):
        """llama models are not thinking models."""
        assert is_thinking_model("llama3.2:3b") is False
        assert is_thinking_model("llama3.1:8b") is False

    @pytest.mark.unit
    def test_mistral_is_not_thinking(self):
        """mistral is not a thinking model."""
        assert is_thinking_model("mistral:7b") is False

    @pytest.mark.unit
    def test_nomic_embed_is_not_thinking(self):
        """Embedding models are not thinking models."""
        assert is_thinking_model("nomic-embed-text") is False


# ============================================================================
# Classification Chat Kwargs Tests (Option A)
# ============================================================================

class TestGetClassificationChatKwargs:
    """Tests for get_classification_chat_kwargs() helper."""

    @pytest.mark.unit
    def test_thinking_model_gets_think_false(self):
        """Thinking models get think=False."""
        kwargs = get_classification_chat_kwargs("qwen3:14b")
        assert kwargs == {"think": False}

    @pytest.mark.unit
    def test_non_thinking_model_gets_empty_kwargs(self):
        """Non-thinking models get empty kwargs."""
        kwargs = get_classification_chat_kwargs("llama3.2:3b")
        assert kwargs == {}

    @pytest.mark.unit
    def test_deepseek_r1_gets_think_false(self):
        """DeepSeek R1 gets think=False."""
        kwargs = get_classification_chat_kwargs("deepseek-r1:70b")
        assert kwargs == {"think": False}


# ============================================================================
# Response Content Extraction Tests (Option B)
# ============================================================================

class TestExtractResponseContent:
    """Tests for extract_response_content() failsafe."""

    @pytest.mark.unit
    def test_extracts_normal_content(self):
        """Normal response content is extracted."""
        response = MagicMock()
        response.message.content = "Hello, world!"
        assert extract_response_content(response) == "Hello, world!"

    @pytest.mark.unit
    def test_handles_empty_content(self):
        """Empty content returns empty string."""
        response = MagicMock()
        response.message.content = ""
        response.message.thinking = None
        assert extract_response_content(response) == ""

    @pytest.mark.unit
    def test_handles_none_content(self):
        """None content returns empty string."""
        response = MagicMock()
        response.message.content = None
        response.message.thinking = None
        assert extract_response_content(response) == ""

    @pytest.mark.unit
    def test_logs_warning_for_empty_content_with_thinking(self):
        """Warning is logged when content is empty but thinking is present."""
        response = MagicMock()
        response.message.content = ""
        response.message.thinking = "I am reasoning about this..."

        # Should return empty string (not use thinking as content)
        result = extract_response_content(response)
        assert result == ""

    @pytest.mark.unit
    def test_does_not_use_thinking_as_content(self):
        """Thinking content is NOT used as the response."""
        response = MagicMock()
        response.message.content = ""
        response.message.thinking = "Secret reasoning"

        result = extract_response_content(response)
        assert result == ""
        assert "Secret reasoning" not in result
