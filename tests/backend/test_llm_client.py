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
    get_agent_client,
    get_default_client,
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

        mock_cls.assert_called_once_with(host="http://localhost:11434")
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
        sentinel = MagicMock()
        mock_cls.return_value = sentinel

        result = get_default_client()

        mock_cls.assert_called_once_with(host="http://my-ollama:11434")
        assert result is sentinel


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
        sentinel = MagicMock()
        mock_cls.return_value = sentinel

        client, resolved = get_agent_client(
            role_url="http://role:11434",
            fallback_url="http://fallback:11434",
        )

        assert resolved == "http://role:11434"
        mock_cls.assert_called_once_with(host="http://role:11434")
        assert client is sentinel

    @pytest.mark.unit
    @patch("ollama.AsyncClient")
    @patch("utils.llm_client.settings")
    def test_fallback_url_used_when_no_role_url(self, mock_settings, mock_cls):
        """fallback_url is used when role_url is None."""
        mock_settings.ollama_url = "http://default:11434"
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
        sentinel = MagicMock()
        mock_cls.return_value = sentinel

        client, resolved = get_agent_client(role_url=None, fallback_url="")

        assert resolved == "http://default:11434"
        assert client is sentinel
