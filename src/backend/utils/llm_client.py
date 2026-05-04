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

    async def list(self, *args: Any, **kwargs: Any) -> Any:  # noqa: D102
        return await self._call("list", *args, **kwargs)

    async def generate(self, *args: Any, **kwargs: Any) -> Any:  # noqa: D102
        return await self._call("generate", *args, **kwargs)


def _make_client_with_fallback(primary_url: str) -> LLMClient:
    """Return a client for *primary_url*, wrapped with fallback if configured."""
    primary = create_llm_client(primary_url)
    if settings.ollama_fallback_url and _normalize_url(settings.ollama_fallback_url) != _normalize_url(primary_url):
        fallback = create_llm_client(settings.ollama_fallback_url)
        return _FallbackLLMClient(primary, fallback, settings.ollama_fallback_url)  # type: ignore[return-value]
    return primary


def get_default_client() -> LLMClient:
    """Return the client for the default chat tier.

    When ``LLM_OPENAI_BASE_URL`` is set and the chat tier opts in (default: yes),
    routes through the OpenAI-compatible endpoint (llama-server). Otherwise
    falls back to the Ollama URL with transparent OLLAMA_FALLBACK_URL retry.
    """
    if use_openai_for_tier("chat"):
        client = get_openai_compat_client()
        if client is not None:
            return client  # type: ignore[return-value]
    return _make_client_with_fallback(settings.ollama_url)


def get_embed_client() -> LLMClient:
    """Return the client for embedding calls.

    Priority:
      1. ``LLM_OPENAI_EMBED_BASE_URL`` set → OpenAI-compatible embed client
         (a llama-server pod with --embedding hosting Qwen3-Embedding etc).
      2. ``OLLAMA_EMBED_URL`` set → dedicated Ollama embed instance.
      3. Fall back to ``settings.ollama_url`` (NOT get_default_client, which
         may itself have been swapped to a chat-only llama-server).
    """
    embed_oa = get_openai_compat_embed_client()
    if embed_oa is not None:
        return embed_oa  # type: ignore[return-value]
    if settings.ollama_embed_url:
        return _make_client_with_fallback(settings.ollama_embed_url)
    return _make_client_with_fallback(settings.ollama_url)


def get_agent_client(
    role_url: str | None = None,
    fallback_url: str | None = None,
) -> tuple[LLMClient, str]:
    """Resolve agent client with OpenAI-compatible priority:

      1. If ``LLM_OPENAI_BASE_URL`` is set and the agent tier opts in (default
         when configured): return the OpenAI-compatible client.
      2. Otherwise: ``role_url`` → ``fallback_url`` → ``settings.ollama_url``
         with transparent fallback wrapping.

    Returns ``(client, resolved_url)`` so callers can log which URL won.
    """
    if use_openai_for_tier("agent"):
        client = get_openai_compat_client()
        if client is not None:
            return client, settings.llm_openai_base_url or ""  # type: ignore[return-value]
    resolved = role_url or fallback_url or settings.ollama_url
    return _make_client_with_fallback(resolved), resolved


def clear_client_cache() -> None:
    """Clear the client cache (useful in tests)."""
    _client_cache.clear()


# ---------------------------------------------------------------------------
# OpenAI-compatible client adapter (for llama-server / vLLM / etc.)
# ---------------------------------------------------------------------------
#
# llama-server exposes an OpenAI-compatible REST API at /v1. To plug it in
# without touching every call-site, we wrap openai.AsyncOpenAI in an adapter
# that satisfies the LLMClient Protocol AND returns response objects with
# the same attribute shape as ollama.AsyncClient (`response.message.content`,
# `response.message.tool_calls`, etc). Renfield's response handling already
# tolerates dict-style and attribute-style access, so a SimpleNamespace
# wrapper is enough.


class _OllamaShapedMessage:
    """Mimics ollama.ChatResponse.message — attribute access for content,
    tool_calls, role, thinking. Renfield reads via getattr/dict mixed."""

    __slots__ = ("role", "content", "tool_calls", "thinking")

    def __init__(self, role: str, content: str, tool_calls: list[Any] | None, thinking: str | None) -> None:
        self.role = role
        self.content = content
        self.tool_calls = tool_calls
        self.thinking = thinking


class _OllamaShapedResponse:
    """Mimics ollama.ChatResponse with .message attribute."""

    __slots__ = ("message", "model", "done")

    def __init__(self, message: _OllamaShapedMessage, model: str) -> None:
        self.message = message
        self.model = model
        self.done = True


class OpenAICompatibleClient:
    """Adapter that satisfies the LLMClient Protocol against an OpenAI-style API.

    Translates Renfield's Ollama-shaped chat() invocation into an OpenAI
    chat.completions request, then wraps the response so existing call-sites
    that read ``response.message.content`` / ``.tool_calls`` / ``.thinking``
    keep working unchanged.

    Streaming is supported via async iterators that yield Ollama-shaped
    chunks (``chunk.message.content``).

    Embeddings: not supported by every llama-server build; raises
    NotImplementedError so the caller falls through to the Ollama embed path.
    """

    def __init__(self, base_url: str, api_key: str, default_model: str) -> None:
        import openai

        self._client = openai.AsyncOpenAI(base_url=base_url.rstrip("/"), api_key=api_key or "no-key")
        self._default_model = default_model
        self._base_url = base_url

    @staticmethod
    def _convert_messages(messages: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        """Pass Renfield's chat-message list through with minor normalization.

        Ollama and OpenAI use the same {role, content} shape; tool messages
        and tool_calls also match. Only difference: Ollama allows `images`
        on user messages — we strip those (vision goes to a separate tier).
        """
        if not messages:
            return []
        out: list[dict[str, Any]] = []
        for m in messages:
            mm = {k: v for k, v in m.items() if k != "images"}
            out.append(mm)
        return out

    @staticmethod
    def _options_to_openai(options: dict[str, Any] | None, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Map Ollama `options` (temperature/top_p/num_predict/...) to OpenAI kwargs.

        Only the fields Renfield actually sets are mapped — anything else is
        dropped silently (ollama accepts a wide pile of options that have no
        OpenAI counterpart, like `mirostat`).
        """
        oa: dict[str, Any] = {}
        opts = options or {}
        if "temperature" in opts:
            oa["temperature"] = opts["temperature"]
        if "top_p" in opts:
            oa["top_p"] = opts["top_p"]
        if "num_predict" in opts:
            oa["max_tokens"] = opts["num_predict"]
        if "seed" in opts:
            oa["seed"] = opts["seed"]
        if "stop" in opts:
            oa["stop"] = opts["stop"]
        if kwargs.get("format") == "json":
            oa["response_format"] = {"type": "json_object"}
        return oa

    @staticmethod
    def _think_extra_body(kwargs: dict[str, Any]) -> dict[str, Any]:
        """Translate Ollama's `think=False` flag into a llama-server
        chat-template kwarg so Qwen3-family thinking mode is suppressed."""
        if kwargs.get("think") is False:
            return {"chat_template_kwargs": {"enable_thinking": False}}
        return {}

    async def chat(
        self,
        model: str = "",
        messages: list[dict[str, Any]] | None = None,
        *,
        stream: bool = False,
        options: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        oa_messages = self._convert_messages(messages)
        oa_kwargs = self._options_to_openai(options, kwargs)
        extra_body = self._think_extra_body(kwargs)

        # Tools: Ollama accepts a `tools` list with the OpenAI function schema
        # already, so pass through. tool_choice maps directly.
        if "tools" in kwargs:
            oa_kwargs["tools"] = kwargs["tools"]
        if "tool_choice" in kwargs:
            oa_kwargs["tool_choice"] = kwargs["tool_choice"]

        request_model = model or self._default_model

        if stream:
            return self._chat_stream(request_model, oa_messages, oa_kwargs, extra_body)

        response = await self._client.chat.completions.create(
            model=request_model,
            messages=oa_messages,
            stream=False,
            extra_body=extra_body or None,
            **oa_kwargs,
        )
        return self._wrap_response(response, request_model)

    async def _chat_stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        oa_kwargs: dict[str, Any],
        extra_body: dict[str, Any],
    ) -> Any:
        stream = await self._client.chat.completions.create(
            model=model,
            messages=messages,
            stream=True,
            extra_body=extra_body or None,
            **oa_kwargs,
        )
        async for chunk in stream:
            choice = chunk.choices[0] if chunk.choices else None
            if not choice:
                continue
            delta = choice.delta
            content = (delta.content or "") if delta else ""
            tool_calls = getattr(delta, "tool_calls", None) if delta else None
            yield _OllamaShapedResponse(
                _OllamaShapedMessage(
                    role="assistant",
                    content=content,
                    tool_calls=tool_calls,
                    thinking=None,
                ),
                model,
            )

    @staticmethod
    def _wrap_response(response: Any, model: str) -> _OllamaShapedResponse:
        choice = response.choices[0] if response.choices else None
        msg = choice.message if choice else None
        content = (msg.content if msg else "") or ""
        tool_calls = getattr(msg, "tool_calls", None) if msg else None
        # llama-server with thinking-enabled exposes reasoning_content; harmless if absent.
        thinking = getattr(msg, "reasoning_content", None) if msg else None
        return _OllamaShapedResponse(
            _OllamaShapedMessage(
                role="assistant",
                content=content,
                tool_calls=tool_calls,
                thinking=thinking,
            ),
            model,
        )

    async def embeddings(
        self,
        model: str = "",
        prompt: str = "",
        *,
        options: dict[str, Any] | None = None,  # noqa: ARG002
        **_kwargs: Any,
    ) -> Any:
        """Embed `prompt` and return an Ollama-shaped result with `.embedding`.

        Renfield reads `response.embedding` (a flat list of floats); OpenAI
        returns `response.data[0].embedding`. We wrap the OpenAI result in a
        SimpleNamespace so existing call-sites need no change.
        """
        from types import SimpleNamespace

        request_model = model or self._default_model
        response = await self._client.embeddings.create(model=request_model, input=prompt)
        first = response.data[0] if response.data else None
        embedding = list(first.embedding) if first else []
        return SimpleNamespace(embedding=embedding, model=request_model)

    async def list(self) -> Any:  # noqa: D401
        """Return a minimal Ollama-style model list. Used by health checks."""
        from types import SimpleNamespace

        oa_models = await self._client.models.list()
        models = [SimpleNamespace(model=m.id, name=m.id) for m in oa_models.data]
        return SimpleNamespace(models=models)


def _make_openai_compat_client(*, base_url: str, default_model: str) -> OpenAICompatibleClient:
    """Construct an OpenAI-compatible client for the given endpoint."""
    api_key = settings.llm_openai_api_key.get_secret_value() if settings.llm_openai_api_key else "no-key"
    return OpenAICompatibleClient(
        base_url=base_url,
        api_key=api_key,
        default_model=default_model,
    )


def get_openai_compat_client() -> OpenAICompatibleClient | None:
    """Cached chat/agent OpenAI-compatible client, or None if not configured."""
    base_url = settings.llm_openai_base_url
    if not base_url:
        return None
    cache_key = f"__openai_compat_chat__:{_normalize_url(base_url)}"
    if cache_key not in _client_cache:
        _client_cache[cache_key] = _make_openai_compat_client(  # type: ignore[assignment]
            base_url=base_url,
            default_model=settings.llm_openai_model,
        )
    return _client_cache.get(cache_key)  # type: ignore[return-value]


def get_openai_compat_embed_client() -> OpenAICompatibleClient | None:
    """Cached embed OpenAI-compatible client, or None if not configured.

    Distinct from get_openai_compat_client() because embeddings live on a
    separate llama-server pod (with --embedding flag and a different model).
    """
    base_url = settings.llm_openai_embed_base_url
    if not base_url:
        return None
    cache_key = f"__openai_compat_embed__:{_normalize_url(base_url)}"
    if cache_key not in _client_cache:
        _client_cache[cache_key] = _make_openai_compat_client(  # type: ignore[assignment]
            base_url=base_url,
            default_model=settings.llm_openai_embed_model,
        )
    return _client_cache.get(cache_key)  # type: ignore[return-value]


def use_openai_for_tier(tier: str) -> bool:
    """Return True iff the given tier should route through the OpenAI-compatible
    endpoint instead of Ollama.

    Resolution order:
      1. If `llm_openai_base_url` is unset → always False.
      2. If a per-tier override is set explicitly → use it.
      3. Otherwise, follow the agent setting (which defaults to True when the
         endpoint is configured).
    """
    if not settings.llm_openai_base_url:
        return False
    per_tier_attr = f"llm_openai_for_{tier}"
    per_tier = getattr(settings, per_tier_attr, None)
    if per_tier is not None:
        return bool(per_tier)
    agent = settings.llm_openai_for_agent
    if agent is None:
        return True  # default: route everything through llama-server when configured
    return bool(agent)


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
