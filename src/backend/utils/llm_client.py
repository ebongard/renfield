"""
LLM Client Factory — Centralized creation and caching of LLM clients.

Provides a Protocol that ollama.AsyncClient satisfies via structural typing,
plus factory functions with URL-based caching to eliminate duplicate client
instantiations across services.

Also handles thinking-mode models (e.g., Qwen3) which require special handling
for classification tasks where we need deterministic output without reasoning.

Timeout & Fallback:
    OLLAMA_CONNECT_TIMEOUT — TCP connect timeout in seconds (default: 10).
      Fast-fails when the primary Ollama host is offline so background tasks
      (e.g. KG extraction) don't hang indefinitely.
    OLLAMA_FALLBACK_URL — If set and the primary Ollama raises a connection
      error, the same request is transparently retried on the fallback URL.
      Useful when cuda.local (GPU) is the primary but may be offline.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from loguru import logger

from utils.config import settings

# ---------------------------------------------------------------------------
# Thinking-capable models (Option C: Model-specific configuration)
# ---------------------------------------------------------------------------
# Models that support thinking mode and may return {"content": "...", "thinking": "..."}
# ollama-python 0.6.1 has a bug where content is empty when thinking is present
THINKING_MODELS: frozenset[str] = frozenset({
    "qwen3",
    "qwq",
    "deepseek-r1",
    "deepseek-r1-distill",  # Distilled versions (qwen/llama based)
    "marco-o1",  # Alibaba's reasoning model
    "skywork-o1",  # Kunlun's reasoning model
})


def is_thinking_model(model: str) -> bool:
    """Check if a model supports thinking mode.

    Matches model family prefixes (e.g., "qwen3:14b" matches "qwen3").
    """
    model_lower = model.lower()
    return any(model_lower.startswith(prefix) for prefix in THINKING_MODELS)


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
    the same ``ollama.AsyncClient`` instance.  All clients are created with
    explicit connect / read timeouts so a downed Ollama host fails fast
    (``OLLAMA_CONNECT_TIMEOUT``) instead of hanging forever.
    """
    import httpx
    import ollama

    key = _normalize_url(host)
    if key not in _client_cache:
        timeout = httpx.Timeout(
            connect=settings.ollama_connect_timeout,
            read=settings.ollama_read_timeout,
            write=30.0,
            pool=None,
        )
        _client_cache[key] = ollama.AsyncClient(host=host, timeout=timeout)
    return _client_cache[key]


# ---------------------------------------------------------------------------
# Transparent fallback client
# ---------------------------------------------------------------------------


class _FallbackLLMClient:
    """Wraps a primary LLM client with transparent fallback on connect errors.

    On the first ``chat()`` or ``embeddings()`` call, the primary is tried.
    If a connection-level error is raised (host down / unreachable), the same
    call is retried on the fallback client and a warning is emitted.
    Subsequent calls always try the primary first so recovery is automatic
    when the GPU host comes back online.
    """

    def __init__(self, primary: LLMClient, fallback: LLMClient, fallback_url: str) -> None:
        self._primary = primary
        self._fallback = fallback
        self._fallback_url = fallback_url

    async def _call(self, method: str, /, *args: Any, **kwargs: Any) -> Any:
        import httpx

        try:
            return await getattr(self._primary, method)(*args, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            logger.warning(
                f"Primary Ollama unreachable ({exc!r}), "
                f"retrying on fallback {self._fallback_url}"
            )
            return await getattr(self._fallback, method)(*args, **kwargs)

    async def chat(self, *args: Any, **kwargs: Any) -> Any:  # noqa: D102
        return await self._call("chat", *args, **kwargs)

    async def embeddings(self, *args: Any, **kwargs: Any) -> Any:  # noqa: D102
        return await self._call("embeddings", *args, **kwargs)


def _make_client_with_fallback(primary_url: str) -> LLMClient:
    """Return a client for *primary_url*, wrapped with fallback if configured."""
    primary = create_llm_client(primary_url)
    if settings.ollama_fallback_url and _normalize_url(settings.ollama_fallback_url) != _normalize_url(primary_url):
        fallback = create_llm_client(settings.ollama_fallback_url)
        return _FallbackLLMClient(primary, fallback, settings.ollama_fallback_url)  # type: ignore[return-value]
    return primary


def get_default_client() -> LLMClient:
    """Return the client for ``settings.ollama_url`` (with fallback if configured)."""
    return _make_client_with_fallback(settings.ollama_url)


def get_embed_client() -> LLMClient:
    """Return the client for embedding calls.

    Uses ``settings.ollama_embed_url`` when configured (separate Ollama instance
    dedicated to embeddings), otherwise falls back to ``get_default_client()``.
    Both paths include transparent fallback via ``OLLAMA_FALLBACK_URL``.
    """
    if settings.ollama_embed_url:
        return _make_client_with_fallback(settings.ollama_embed_url)
    return get_default_client()


def get_agent_client(
    role_url: str | None = None,
    fallback_url: str | None = None,
) -> tuple[LLMClient, str]:
    """Resolve agent URL with priority: *role_url* → *fallback_url* → default.

    Returns ``(client, resolved_url)`` so callers can log which URL won.
    The resolved client includes transparent fallback if ``OLLAMA_FALLBACK_URL``
    is configured.
    """
    resolved = role_url or fallback_url or settings.ollama_url
    return _make_client_with_fallback(resolved), resolved


def clear_client_cache() -> None:
    """Clear the client cache (useful in tests)."""
    _client_cache.clear()


# ---------------------------------------------------------------------------
# Thinking Mode Handling (Options A + B)
# ---------------------------------------------------------------------------


def get_classification_chat_kwargs(model: str) -> dict[str, Any]:
    """Get kwargs for classification tasks (router, intent extraction).

    Option A: Disables thinking mode for thinking-capable models to ensure
    deterministic, fast responses without reasoning overhead.

    Args:
        model: The model name (e.g., "qwen3:14b")

    Returns:
        dict with `think=False` if model supports thinking, else empty dict
    """
    if is_thinking_model(model):
        logger.debug(f"Disabling thinking mode for classification model: {model}")
        return {"think": False}
    return {}


def extract_response_content(response: Any) -> str:
    """Extract content from an LLM response with failsafe for thinking mode.

    Option B: Handles the ollama-python 0.6.1 bug where content is empty
    when thinking mode is active. Falls back to thinking content if present.

    Args:
        response: The response object from client.chat()

    Returns:
        The response content string (or empty string if none found)
    """
    content = response.message.content or ""

    # Failsafe: If content is empty but thinking is present, log a warning
    # and return empty string (caller should handle this gracefully)
    if not content:
        thinking = getattr(response.message, "thinking", None)
        if thinking:
            logger.warning(
                f"LLM response has empty content but thinking present "
                f"(length: {len(thinking)}). This may indicate think=False "
                f"was not passed for a thinking model."
            )
            # Don't use thinking as content - it's not the answer
            # Instead, return empty so caller falls back to default behavior

    return content
