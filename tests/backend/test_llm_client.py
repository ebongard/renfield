"""
Tests for utils/llm_client.py — LLM Client Protocol + Factory.

Tests:
- Protocol structural typing (positive + negative)
- Factory: client creation, URL-based caching, cache clearing
- Agent client: URL priority resolution (role → fallback → default)
"""
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Stub 'ollama' / 'openai' only if genuinely absent. create_llm_client()
# does `import ollama` and OpenAICompatibleClient instantiates
# openai.AsyncOpenAI — but in the real test container both ARE installed,
# and unconditionally stubbing them poisons every later test that imports
# them. Stub-if-missing keeps this file runnable standalone too.
if "ollama" not in sys.modules:
    try:
        import ollama  # noqa: F401
    except Exception:  # noqa: BLE001
        _ollama_stub = MagicMock()
        _ollama_stub.AsyncClient = MagicMock()
        sys.modules["ollama"] = _ollama_stub

if "openai" not in sys.modules:
    try:
        import openai  # noqa: F401
    except Exception:  # noqa: BLE001
        _openai_stub = MagicMock()
        _openai_stub.AsyncOpenAI = MagicMock()
        sys.modules["openai"] = _openai_stub

from utils.llm_client import (
    LLMClient,
    OpenAICompatibleClient,
    clear_client_cache,
    create_llm_client,
    extract_response_content,
    get_agent_client,
    get_classification_chat_kwargs,
    get_default_client,
    get_embed_client,
    get_openai_compat_client,
    get_openai_compat_embed_client,
    is_thinking_model,
    use_openai_for_tier,
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
        mock_settings.llm_openai_base_url = None
        mock_settings.llm_openai_embed_base_url = None
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

        mock_settings.llm_openai_base_url = None
        mock_settings.llm_openai_embed_base_url = None
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

        mock_settings.llm_openai_base_url = None
        mock_settings.llm_openai_embed_base_url = None
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
        mock_settings.llm_openai_base_url = None
        mock_settings.llm_openai_embed_base_url = None
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
        mock_settings.llm_openai_base_url = None
        mock_settings.llm_openai_embed_base_url = None
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

        mock_settings.llm_openai_base_url = None
        mock_settings.llm_openai_embed_base_url = None
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
        mock_settings.llm_openai_base_url = None
        mock_settings.llm_openai_embed_base_url = None
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
        mock_settings.llm_openai_base_url = None
        mock_settings.llm_openai_embed_base_url = None
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
        mock_settings.llm_openai_base_url = None
        mock_settings.llm_openai_embed_base_url = None
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
        mock_settings.llm_openai_base_url = None
        mock_settings.llm_openai_embed_base_url = None
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
        mock_settings.llm_openai_base_url = None
        mock_settings.llm_openai_embed_base_url = None
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


# ============================================================================
# OpenAICompatibleClient — adapter against an OpenAI-shaped REST endpoint
# (llama-server, vLLM, …) that surfaces Ollama-shaped response objects so
# call-sites that read `.message.content` / `.message.tool_calls` /
# `.message.thinking` keep working unchanged.
# ============================================================================


def _stub_openai_choice(content="", tool_calls=None, reasoning_content=None):
    """Build a non-stream openai.ChatCompletion-shaped fake."""
    msg = SimpleNamespace(
        content=content,
        tool_calls=tool_calls,
        reasoning_content=reasoning_content,
        role="assistant",
    )
    return SimpleNamespace(message=msg, finish_reason="stop", index=0)


def _stub_openai_response(content="", tool_calls=None, reasoning_content=None):
    """Top-level non-stream openai response with a single choice."""
    choice = _stub_openai_choice(content, tool_calls, reasoning_content)
    return SimpleNamespace(choices=[choice], usage=None, model="qwen3.6")


async def _async_iter(items):
    """Helper: turn a list into an async iterator (mocks an openai stream)."""
    for item in items:
        yield item


def _make_openai_compat(monkeypatch, *, stub_factory=None) -> OpenAICompatibleClient:
    """Construct an OpenAICompatibleClient with `openai.AsyncOpenAI` patched
    to a controllable fake. Returns the adapter; the underlying fake is
    accessible via adapter._client."""
    fake_client = MagicMock()
    fake_client.chat = MagicMock()
    fake_client.chat.completions = MagicMock()
    fake_client.chat.completions.create = AsyncMock()
    fake_client.embeddings = MagicMock()
    fake_client.embeddings.create = AsyncMock()
    fake_client.models = MagicMock()
    fake_client.models.list = AsyncMock()

    if stub_factory:
        stub_factory(fake_client)

    fake_async_openai = MagicMock(return_value=fake_client)
    monkeypatch.setattr("openai.AsyncOpenAI", fake_async_openai)

    adapter = OpenAICompatibleClient(
        base_url="http://llama:8080/v1",
        api_key="test-key",
        default_model="qwen3.6",
    )
    return adapter


class TestOpenAICompatibleClientChat:
    """Non-streaming chat() call shape."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_returns_ollama_shaped_response(self, monkeypatch):
        adapter = _make_openai_compat(monkeypatch)
        adapter._client.chat.completions.create.return_value = _stub_openai_response(
            content="Hi there"
        )

        response = await adapter.chat(
            model="qwen3.6",
            messages=[{"role": "user", "content": "hi"}],
        )

        assert response.message.content == "Hi there"
        assert response.message.role == "assistant"
        assert response.message.tool_calls is None
        assert response.message.thinking is None
        assert response.model == "qwen3.6"
        assert response.done is True

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_passes_through_tool_calls(self, monkeypatch):
        adapter = _make_openai_compat(monkeypatch)
        tc = SimpleNamespace(
            function=SimpleNamespace(name="home_assistant_query", arguments='{"area":"wohnzimmer"}'),
            id="call_1",
            type="function",
        )
        adapter._client.chat.completions.create.return_value = _stub_openai_response(
            content="", tool_calls=[tc]
        )

        response = await adapter.chat(
            messages=[{"role": "user", "content": "Welche Lampen?"}],
        )

        assert response.message.content == ""
        assert response.message.tool_calls is not None
        assert response.message.tool_calls[0].function.name == "home_assistant_query"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_reasoning_content_surfaces_as_thinking(self, monkeypatch):
        """When llama-server returns reasoning_content (thinking models), the
        adapter exposes it as response.message.thinking — same field name as
        ollama-python uses, so extract_response_content() works unchanged."""
        adapter = _make_openai_compat(monkeypatch)
        adapter._client.chat.completions.create.return_value = _stub_openai_response(
            content="", reasoning_content="Step 1, step 2…"
        )

        response = await adapter.chat(messages=[{"role": "user", "content": "?"}])
        assert response.message.thinking == "Step 1, step 2…"
        assert response.message.content == ""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_default_model_used_when_caller_omits(self, monkeypatch):
        adapter = _make_openai_compat(monkeypatch)
        adapter._client.chat.completions.create.return_value = _stub_openai_response()

        await adapter.chat(messages=[{"role": "user", "content": "x"}])

        kwargs = adapter._client.chat.completions.create.call_args.kwargs
        assert kwargs["model"] == "qwen3.6"  # default_model from constructor

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_strips_images_from_user_message(self, monkeypatch):
        """Vision payloads (`images=[…]`) on user messages aren't supported
        by the agent llama-server (single-model) and would otherwise be sent
        as an unknown field. The adapter strips them silently."""
        adapter = _make_openai_compat(monkeypatch)
        adapter._client.chat.completions.create.return_value = _stub_openai_response()

        await adapter.chat(
            messages=[
                {"role": "user", "content": "describe", "images": ["base64-blob"]},
            ],
        )

        sent = adapter._client.chat.completions.create.call_args.kwargs["messages"]
        assert sent[0] == {"role": "user", "content": "describe"}
        assert "images" not in sent[0]


class TestOpenAICompatibleClientStreaming:
    """Streaming chat() returns Ollama-shaped chunks via async generator."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_stream_yields_ollama_shaped_chunks(self, monkeypatch):
        chunk1 = SimpleNamespace(
            choices=[SimpleNamespace(
                delta=SimpleNamespace(content="Hello", tool_calls=None),
                finish_reason=None,
                index=0,
            )]
        )
        chunk2 = SimpleNamespace(
            choices=[SimpleNamespace(
                delta=SimpleNamespace(content=" world", tool_calls=None),
                finish_reason="stop",
                index=0,
            )]
        )

        adapter = _make_openai_compat(monkeypatch)
        adapter._client.chat.completions.create.return_value = _async_iter([chunk1, chunk2])

        # Mirrors the production call pattern used in services/ollama_service.py:
        # `async for chunk in await client.chat(stream=True)`.
        gen = await adapter.chat(messages=[{"role": "user", "content": "x"}], stream=True)
        chunks = []
        async for c in gen:
            chunks.append(c)

        assert [c.message.content for c in chunks] == ["Hello", " world"]
        assert all(c.message.role == "assistant" for c in chunks)

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_stream_skips_chunks_with_no_choices(self, monkeypatch):
        empty = SimpleNamespace(choices=[])
        good = SimpleNamespace(choices=[SimpleNamespace(
            delta=SimpleNamespace(content="ok", tool_calls=None),
            finish_reason="stop", index=0,
        )])
        adapter = _make_openai_compat(monkeypatch)
        adapter._client.chat.completions.create.return_value = _async_iter([empty, good])

        gen = await adapter.chat(messages=[{"role": "user", "content": "x"}], stream=True)
        chunks = [c async for c in gen]
        assert len(chunks) == 1
        assert chunks[0].message.content == "ok"


class TestOpenAICompatibleClientOptionsMapping:
    """Ollama `options` dict + kwargs are translated to OpenAI parameters."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_ollama_options_translate_to_openai(self, monkeypatch):
        adapter = _make_openai_compat(monkeypatch)
        adapter._client.chat.completions.create.return_value = _stub_openai_response()

        await adapter.chat(
            messages=[{"role": "user", "content": "x"}],
            options={
                "temperature": 0.6,
                "top_p": 0.95,
                "num_predict": 64,
                "seed": 42,
                "stop": ["</done>"],
                "mirostat": 1,  # ollama-only — should be silently dropped
            },
        )

        kwargs = adapter._client.chat.completions.create.call_args.kwargs
        assert kwargs["temperature"] == 0.6
        assert kwargs["top_p"] == 0.95
        assert kwargs["max_tokens"] == 64
        assert kwargs["seed"] == 42
        assert kwargs["stop"] == ["</done>"]
        assert "mirostat" not in kwargs

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_format_json_translates_to_response_format(self, monkeypatch):
        adapter = _make_openai_compat(monkeypatch)
        adapter._client.chat.completions.create.return_value = _stub_openai_response()

        await adapter.chat(
            messages=[{"role": "user", "content": "x"}],
            format="json",
        )

        kwargs = adapter._client.chat.completions.create.call_args.kwargs
        assert kwargs["response_format"] == {"type": "json_object"}

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_think_false_disables_thinking_via_chat_template_kwargs(self, monkeypatch):
        """`think=False` (the Ollama-side flag) translates to llama-server's
        chat-template extension. Without this, Qwen3 models leave content
        empty and put the JSON answer in reasoning_content — the silent
        memory-extraction bug from the migration."""
        adapter = _make_openai_compat(monkeypatch)
        adapter._client.chat.completions.create.return_value = _stub_openai_response()

        await adapter.chat(
            messages=[{"role": "user", "content": "x"}],
            think=False,
        )

        kwargs = adapter._client.chat.completions.create.call_args.kwargs
        assert kwargs["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_think_omitted_does_not_send_extra_body(self, monkeypatch):
        adapter = _make_openai_compat(monkeypatch)
        adapter._client.chat.completions.create.return_value = _stub_openai_response()

        await adapter.chat(messages=[{"role": "user", "content": "x"}])

        kwargs = adapter._client.chat.completions.create.call_args.kwargs
        # Either absent, or explicitly None — both are fine; what matters is
        # we don't pin enable_thinking to False when the caller didn't ask.
        assert kwargs.get("extra_body") is None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_tools_pass_through(self, monkeypatch):
        adapter = _make_openai_compat(monkeypatch)
        adapter._client.chat.completions.create.return_value = _stub_openai_response()
        tool_schema = {"type": "function", "function": {"name": "x", "parameters": {}}}

        await adapter.chat(
            messages=[{"role": "user", "content": "x"}],
            tools=[tool_schema],
            tool_choice="auto",
        )

        kwargs = adapter._client.chat.completions.create.call_args.kwargs
        assert kwargs["tools"] == [tool_schema]
        assert kwargs["tool_choice"] == "auto"


class TestOpenAICompatibleClientEmbeddings:
    """embeddings() returns Ollama-shaped {.embedding, .model}."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_returns_ollama_shaped_embedding(self, monkeypatch):
        adapter = _make_openai_compat(monkeypatch)
        adapter._client.embeddings.create.return_value = SimpleNamespace(
            data=[SimpleNamespace(embedding=[0.1, 0.2, 0.3], index=0)],
            model="qwen3-embedding",
        )

        result = await adapter.embeddings(model="qwen3-embedding", prompt="hello")
        assert result.embedding == [0.1, 0.2, 0.3]
        assert result.model == "qwen3-embedding"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_empty_data_returns_empty_embedding(self, monkeypatch):
        adapter = _make_openai_compat(monkeypatch)
        adapter._client.embeddings.create.return_value = SimpleNamespace(data=[])

        result = await adapter.embeddings(prompt="hello")
        assert result.embedding == []

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_uses_default_model_when_caller_omits(self, monkeypatch):
        adapter = _make_openai_compat(monkeypatch)
        adapter._client.embeddings.create.return_value = SimpleNamespace(
            data=[SimpleNamespace(embedding=[0.0])],
        )

        await adapter.embeddings(prompt="hello")
        kwargs = adapter._client.embeddings.create.call_args.kwargs
        assert kwargs["model"] == "qwen3.6"


class TestOpenAICompatFactories:
    """Module-level factories for the OpenAI-compatible adapter."""

    @pytest.mark.unit
    def test_returns_none_when_unconfigured(self, monkeypatch):
        monkeypatch.setattr("utils.llm_client.settings.llm_openai_base_url", None)
        assert get_openai_compat_client() is None
        assert get_openai_compat_embed_client() is None

    @pytest.mark.unit
    def test_chat_and_embed_clients_are_separately_cached(self, monkeypatch):
        """Chat and embed adapters point at different llama-server pods, so
        they must NOT share the same cache slot."""
        monkeypatch.setattr("utils.llm_client.settings.llm_openai_base_url", "http://chat:8080/v1")
        monkeypatch.setattr("utils.llm_client.settings.llm_openai_embed_base_url", "http://embed:8080/v1")
        monkeypatch.setattr("utils.llm_client.settings.llm_openai_model", "qwen3.6")
        monkeypatch.setattr("utils.llm_client.settings.llm_openai_embed_model", "qwen3-embedding")
        monkeypatch.setattr("utils.llm_client.settings.llm_openai_api_key", None)

        chat = get_openai_compat_client()
        embed = get_openai_compat_embed_client()
        assert chat is not None
        assert embed is not None
        assert chat is not embed
        assert chat._base_url == "http://chat:8080/v1"
        assert embed._base_url == "http://embed:8080/v1"

    @pytest.mark.unit
    def test_chat_client_is_cached_on_repeated_calls(self, monkeypatch):
        monkeypatch.setattr("utils.llm_client.settings.llm_openai_base_url", "http://chat:8080/v1")
        monkeypatch.setattr("utils.llm_client.settings.llm_openai_model", "qwen3.6")
        monkeypatch.setattr("utils.llm_client.settings.llm_openai_api_key", None)

        first = get_openai_compat_client()
        second = get_openai_compat_client()
        assert first is second


class TestUseOpenAIForTier:
    """Per-tier routing to the OpenAI-compatible endpoint."""

    @pytest.mark.unit
    def test_returns_false_when_endpoint_unset(self, monkeypatch):
        monkeypatch.setattr("utils.llm_client.settings.llm_openai_base_url", None)
        for tier in ("agent", "chat", "embed", "intent", "kg", "memory"):
            assert use_openai_for_tier(tier) is False

    @pytest.mark.unit
    def test_default_routes_all_tiers_when_endpoint_set(self, monkeypatch):
        """If no per-tier override is present and the endpoint is configured,
        every tier defaults to using the llama-server. This is what makes
        `OLLAMA_VISION_MODEL=""` enough to disable vision without a flag."""
        monkeypatch.setattr("utils.llm_client.settings.llm_openai_base_url", "http://llama:8080/v1")
        for attr in ("llm_openai_for_agent", "llm_openai_for_chat",
                     "llm_openai_for_rag", "llm_openai_for_intent",
                     "llm_openai_for_kg", "llm_openai_for_memory"):
            monkeypatch.setattr(f"utils.llm_client.settings.{attr}", None)

        for tier in ("agent", "chat", "rag", "intent", "kg", "memory"):
            assert use_openai_for_tier(tier) is True

    @pytest.mark.unit
    def test_per_tier_override_keeps_one_tier_on_ollama(self, monkeypatch):
        """Setting LLM_OPENAI_FOR_INTENT=false routes only intent back to Ollama."""
        monkeypatch.setattr("utils.llm_client.settings.llm_openai_base_url", "http://llama:8080/v1")
        monkeypatch.setattr("utils.llm_client.settings.llm_openai_for_agent", None)
        monkeypatch.setattr("utils.llm_client.settings.llm_openai_for_intent", False)

        assert use_openai_for_tier("agent") is True
        assert use_openai_for_tier("intent") is False

    @pytest.mark.unit
    def test_explicit_agent_false_disables_default_for_unspecified_tiers(self, monkeypatch):
        """When agent is explicitly off and a tier has no own override, it
        follows the agent setting (off)."""
        monkeypatch.setattr("utils.llm_client.settings.llm_openai_base_url", "http://llama:8080/v1")
        monkeypatch.setattr("utils.llm_client.settings.llm_openai_for_agent", False)
        monkeypatch.setattr("utils.llm_client.settings.llm_openai_for_chat", None)

        assert use_openai_for_tier("agent") is False
        assert use_openai_for_tier("chat") is False


class TestGetDefaultClientRoutesToOpenAI:
    """get_default_client() / get_agent_client() prefer OpenAI when configured."""

    @pytest.mark.unit
    def test_default_client_returns_openai_adapter_when_configured(self, monkeypatch):
        monkeypatch.setattr("utils.llm_client.settings.llm_openai_base_url", "http://llama:8080/v1")
        monkeypatch.setattr("utils.llm_client.settings.llm_openai_model", "qwen3.6")
        monkeypatch.setattr("utils.llm_client.settings.llm_openai_for_chat", None)
        monkeypatch.setattr("utils.llm_client.settings.llm_openai_for_agent", None)
        monkeypatch.setattr("utils.llm_client.settings.llm_openai_api_key", None)

        client = get_default_client()
        assert isinstance(client, OpenAICompatibleClient)

    @pytest.mark.unit
    def test_default_client_falls_through_to_ollama_when_disabled(self, monkeypatch):
        monkeypatch.setattr("utils.llm_client.settings.llm_openai_base_url", None)
        monkeypatch.setattr("utils.llm_client.settings.ollama_url", "http://ollama:11434")
        monkeypatch.setattr("utils.llm_client.settings.ollama_fallback_url", "")

        client = get_default_client()
        assert not isinstance(client, OpenAICompatibleClient)

    @pytest.mark.unit
    def test_embed_client_uses_openai_embed_endpoint_not_chat(self, monkeypatch):
        """Critical post-migration invariant: the embed factory points at the
        embed pod, NOT at get_default_client which may have been swapped to
        the chat-only llama-server. Mixing the two routed chat traffic to
        the CPU-only embed pod and added 2+ minutes per turn (#527)."""
        monkeypatch.setattr("utils.llm_client.settings.llm_openai_base_url", "http://chat:8080/v1")
        monkeypatch.setattr("utils.llm_client.settings.llm_openai_embed_base_url", "http://embed:8080/v1")
        monkeypatch.setattr("utils.llm_client.settings.llm_openai_model", "qwen3.6")
        monkeypatch.setattr("utils.llm_client.settings.llm_openai_embed_model", "qwen3-embedding")
        monkeypatch.setattr("utils.llm_client.settings.llm_openai_api_key", None)

        embed = get_embed_client()
        assert isinstance(embed, OpenAICompatibleClient)
        assert embed._base_url == "http://embed:8080/v1"
        assert embed._default_model == "qwen3-embedding"
