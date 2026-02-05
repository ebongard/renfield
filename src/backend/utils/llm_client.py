"""
LLM Client Factory — Centralized creation and caching of LLM clients.

Provides a Protocol that ollama.AsyncClient satisfies via structural typing,
plus factory functions with URL-based caching to eliminate duplicate client
instantiations across services.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from utils.config import settings


@runtime_checkable
class LLMClient(Protocol):
    """Structural protocol for LLM clients (chat + embeddings).

    ollama.AsyncClient satisfies this without any adapter.
    Ollama-specific methods (list, pull) stay on the concrete client.
    """

    async def chat(
        self,
        model: str = "",
        messages: list[dict[str, Any]] | None = None,
        *,
        stream: bool = False,
        options: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any: ...

    async def embeddings(
        self,
        model: str = "",
        prompt: str = "",
        *,
        options: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any: ...


# ---------------------------------------------------------------------------
# Client cache (keyed by normalized URL)
# ---------------------------------------------------------------------------
_client_cache: dict[str, LLMClient] = {}


def _normalize_url(url: str) -> str:
    """Strip trailing slashes for consistent cache keys."""
    return url.rstrip("/")


def create_llm_client(host: str) -> LLMClient:
    """Create or reuse an LLM client for *host*.

    Uses a module-level cache so that every call with the same URL returns
    the same ``ollama.AsyncClient`` instance.
    """
    import ollama

    key = _normalize_url(host)
    if key not in _client_cache:
        _client_cache[key] = ollama.AsyncClient(host=host)
    return _client_cache[key]


def get_default_client() -> LLMClient:
    """Return the client for ``settings.ollama_url``."""
    return create_llm_client(settings.ollama_url)


def get_agent_client(
    role_url: str | None = None,
    fallback_url: str | None = None,
) -> tuple[LLMClient, str]:
    """Resolve agent URL with priority: *role_url* → *fallback_url* → default.

    Returns ``(client, resolved_url)`` so callers can log which URL won.
    """
    resolved = role_url or fallback_url or settings.ollama_url
    return create_llm_client(resolved), resolved


def clear_client_cache() -> None:
    """Clear the client cache (useful in tests)."""
    _client_cache.clear()
