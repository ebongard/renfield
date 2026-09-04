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


# ---------------------------------------------------------------------------
# OpenAI-compat primary → in-cluster Ollama fallback (resilience)
# ---------------------------------------------------------------------------


def _should_fallback(exc: BaseException) -> bool:
    """Decide whether a primary-call failure warrants failing over to Ollama.

    Fail over when the primary genuinely CANNOT serve:
    - connection down / unreachable (``openai.APIConnectionError``,
      ``httpx.ConnectError``, ``httpx.ConnectTimeout``), and
    - a 5xx from the server (``openai.APIStatusError`` >= 500) — notably the
      cold-model HTTP 503 during warm-up: the bounded boot-gate lets the backend
      go Ready before the model is warm, so runtime must cover that 503.

    - a connect-timeout (host down / network-unreachable) — which the openai SDK
      surfaces as ``openai.APITimeoutError`` wrapping ``httpx.ConnectTimeout``.

    Do NOT fail over on:
    - read/pool timeouts of a slow-but-HEALTHY primary (``httpx.ReadTimeout`` /
      ``PoolTimeout``, incl. when wrapped as ``openai.APITimeoutError``) —
      silently degrading a busy primary to the weaker local model would hurt
      answer quality, and
    - 4xx client errors (our own bad request) — masking those hides real bugs.

    Because openai flattens connect- AND read/pool-timeouts into one
    ``APITimeoutError`` type, they are told apart by the chained ``__cause__``.

    openai/httpx are imported lazily; if unavailable we do NOT fall back (can't
    classify → surface the error rather than mask it).
    """
    try:
        import httpx
        import openai
    except Exception:  # noqa: BLE001
        return False
    # openai collapses BOTH connect-timeout (host down → MUST fall over) and
    # read/pool-timeout (server up, just slow → keep primary) into APITimeoutError,
    # but chains the original httpx error via __cause__. So classify by the cause:
    # only a genuine read/pool timeout stays on the primary; a connect-timeout —
    # or an unknown/absent cause — falls over, because host-unreachable is the
    # outage this whole feature exists for. (APITimeoutError is a SUBCLASS of
    # APIConnectionError, so it MUST be handled before the APIConnectionError case.)
    if isinstance(exc, openai.APITimeoutError):
        return not isinstance(exc.__cause__, (httpx.ReadTimeout, httpx.PoolTimeout))
    # Raw read/pool timeout (defensive — if it ever surfaces unwrapped) → keep primary.
    if isinstance(exc, (httpx.ReadTimeout, httpx.PoolTimeout)):
        return False
    # Primary down / unreachable → fall over.
    if isinstance(exc, (openai.APIConnectionError, httpx.ConnectError, httpx.ConnectTimeout)):
        return True
    # Server-side 5xx (cold-model 503, 500) → primary can't serve. 4xx → our bug.
    if isinstance(exc, openai.APIStatusError):
        return exc.status_code >= 500
    return False


class _OpenAICompatFallbackClient:
    """Wrap the OpenAI-compat chat/agent client with a transparent retry on the
    in-cluster Ollama when the primary (external llama-server, e.g. cuda.local)
    cannot serve — so a downed/cold external GPU box degrades to the local model
    instead of failing the whole turn (outage 2026-08-08).

    Fail-over trigger: connection-down OR a 5xx (incl. the cold-model 503) — see
    :func:`_should_fallback`. A slow-but-healthy primary (read/pool timeout) is
    NOT failed over (no silent quality drop), and 4xx client errors surface.

    Covered outage shapes: box down (connection refused → APIConnectionError, OR
    connect-timeout → APITimeoutError wrapping httpx.ConnectTimeout) and box
    restarting (cold-model 503). KNOWN LIMITATIONS: (a) a primary that ACCEPTS the
    connection then HANGS mid-generation is cancelled by the caller's own
    ``asyncio.wait_for`` budget before this wrapper's ``except`` runs (surfaces as
    a timeout, not covered); (b) the fallback call runs inside that same per-step
    budget, so a COLD in-cluster model could be cancelled before it answers —
    mitigated in this deployment because the fallback model (qwen3:14b) is pinned
    resident (OLLAMA_KEEP_ALIVE=-1).

    - **Non-streaming** ``chat``: primary first; on a fail-over error, retried on
      Ollama with the model remapped to a known in-cluster model
      (:meth:`_fallback_model`).
    - **Streaming** ``chat``: fails over ONLY if the FIRST chunk fails. Once
      content has started streaming, a mid-stream drop cannot be re-run without
      duplicating output, so it is surfaced.
    - ``list`` / ``embeddings`` delegate to the PRIMARY only (no fail-over): they
      are status/health probes and must reflect the primary's real state (a
      masked ``list`` would make a dead cuda.local look healthy), and embeddings
      never route through this client anyway (see :func:`get_embed_client`).
    - Recovery is automatic: the primary is always tried first.
    """

    def __init__(self, primary: LLMClient, fallback: LLMClient) -> None:
        self._primary = primary
        self._fallback = fallback

    def __getattr__(self, name: str) -> Any:
        # Transparent passthrough for any attr not defined here (e.g. capability
        # flags like ``supports_native_tools``) so wrapping the primary doesn't
        # hide them. Guard the internal names to avoid recursion before __init__
        # has set them. __getattr__ only fires when normal lookup misses.
        if name in ("_primary", "_fallback"):
            raise AttributeError(name)
        return getattr(self._primary, name)

    def _fallback_model(self) -> str:
        """The in-cluster model to use on fail-over — always a single known
        resident model (``llm_openai_fallback_model``, else ``ollama_model``,
        e.g. ``qwen3:14b``). The caller's model (an external-only alias like
        ``qwen3.6``, or a per-role name Ollama may not have) is NOT passed
        through — that would 404 on Ollama during the very outage this covers.
        The primary (llama-server) ignores the requested name anyway, and
        qwen3:14b handles every tier that routes here (chat/agent/intent) in
        degraded mode.
        """
        return settings.llm_openai_fallback_model or settings.ollama_model

    def _fallback_kwargs(self, kwargs: dict[str, Any], fb_model: str) -> dict[str, Any]:
        """Adjust call kwargs for the fallback model. The caller's ``think`` kwarg
        was computed for the PRIMARY model; if the fallback model is a thinking
        model and the caller didn't set ``think``, force ``think=False`` so it
        doesn't return reasoning with empty ``message.content`` (the ollama-python
        0.6.1 empty-content trap → agent JSON-parse failure) during the outage.
        """
        if is_thinking_model(fb_model) and "think" not in kwargs:
            return {**kwargs, "think": False}
        return kwargs

    @staticmethod
    def _shrink_messages_for_fallback(
        messages: list[dict[str, Any]] | None, kwargs: dict[str, Any]
    ) -> list[dict[str, Any]] | None:
        """Fit the prompt into the fallback's context window (#1104).

        The fallback runs at the ``options.num_ctx`` forwarded verbatim in the
        call kwargs (else Ollama's own default) — NOT at whatever wide window
        the primary served (``llm_openai_num_ctx``). On overflow Ollama silently
        drops the prompt HEAD (system/tools framing) and the ReAct contract
        collapses. Instead: keep the system message whole and MIDDLE-CUT the
        oversized user message (the agent prompt is one monolithic string —
        framing at the head, question at the tail, compressible mass in the
        middle), so an outage answer stays coherent instead of turning to
        garbage. Token estimation via the content-aware ``token_counter``
        (rare path — one scan per fail-over call is fine).
        """
        from utils.token_counter import token_counter

        if not messages:
            return messages
        options = kwargs.get("options") or {}
        effective_ctx = options.get("num_ctx") or settings.ollama_num_ctx
        reserved = int(options.get("num_predict") or 0)
        total = sum(
            token_counter.count(str(m.get("content") or "")) for m in messages
        )
        if total + reserved <= effective_ctx:
            return messages

        # Budget for the LARGEST message = window minus everything else.
        # In practice that is the monolithic agent user-prompt; the small
        # system message stays whole.
        largest_i = max(
            range(len(messages)),
            key=lambda i: len(str(messages[i].get("content") or "")),
        )
        others = sum(
            token_counter.count(str(m.get("content") or ""))
            for i, m in enumerate(messages) if i != largest_i
        )
        # Safety margin: the chars/token estimate can undercount; leave 10%.
        budget = int((effective_ctx - reserved - others) * 0.9)
        shrunk_text, shrunk = token_counter.truncate_middle_to_budget(
            str(messages[largest_i].get("content") or ""), budget
        )
        if not shrunk:
            logger.warning(
                f"Fallback prompt (~{total} tokens) exceeds the in-cluster "
                f"fallback context (num_ctx={effective_ctx}) and could not be "
                f"shrunk — the response may be truncated/degraded"
            )
            return messages
        out = [dict(m) for m in messages]
        out[largest_i]["content"] = shrunk_text
        logger.warning(
            f"Fallback prompt (~{total} tokens) exceeded the in-cluster "
            f"fallback context (num_ctx={effective_ctx}) — middle-cut the "
            f"prompt to ~{token_counter.count(shrunk_text)} tokens so the "
            f"framing and the question survive the outage"
        )
        return out

    def _prepare_fallback(
        self,
        exc: Exception,
        messages: list[dict[str, Any]] | None,
        kwargs: dict[str, Any],
        *,
        stream: bool,
    ) -> tuple[str, dict[str, Any], list[dict[str, Any]] | None]:
        """Shared fail-over preamble for the streaming and non-streaming paths:
        resolve the in-cluster model, adjust kwargs, log the fail-over, and
        shrink an oversized prompt into the fallback window."""
        fb_model = self._fallback_model()
        fb_kwargs = self._fallback_kwargs(kwargs, fb_model)
        where = " on stream open" if stream else ""
        logger.warning(
            f"OpenAI-compat LLM primary failed{where} ({exc!r}); "
            f"falling back to in-cluster Ollama (model={fb_model})"
        )
        fb_messages = self._shrink_messages_for_fallback(messages, kwargs)
        return fb_model, fb_kwargs, fb_messages

    async def chat(
        self,
        model: str = "",
        messages: list[dict[str, Any]] | None = None,
        *,
        stream: bool = False,
        **kwargs: Any,
    ) -> Any:
        if stream:
            return self._chat_stream(model, messages, kwargs)
        try:
            return await self._primary.chat(model=model, messages=messages, stream=False, **kwargs)
        except Exception as exc:  # noqa: BLE001 — _should_fallback re-raises what it can't handle
            if not _should_fallback(exc):
                raise
            fb_model, fb_kwargs, fb_messages = self._prepare_fallback(exc, messages, kwargs, stream=False)
            return await self._fallback.chat(model=fb_model, messages=fb_messages, stream=False, **fb_kwargs)

    async def _chat_stream(
        self, model: str, messages: list[dict[str, Any]] | None, kwargs: dict[str, Any]
    ) -> Any:
        try:
            primary_gen = await self._primary.chat(model=model, messages=messages, stream=True, **kwargs)
            first = await primary_gen.__anext__()
        except StopAsyncIteration:
            return  # primary produced an empty (but successful) stream
        except Exception as exc:  # noqa: BLE001
            if not _should_fallback(exc):
                raise
            fb_model, fb_kwargs, fb_messages = self._prepare_fallback(exc, messages, kwargs, stream=True)
            fb_gen = await self._fallback.chat(model=fb_model, messages=fb_messages, stream=True, **fb_kwargs)
            async for chunk in fb_gen:
                yield chunk
            return
        # Primary opened successfully → stream it through (no mid-stream failover).
        yield first
        async for chunk in primary_gen:
            yield chunk

    async def embeddings(self, *args: Any, **kwargs: Any) -> Any:  # noqa: D102
        # No fail-over: embeddings route via get_embed_client(), not this client.
        return await self._primary.embeddings(*args, **kwargs)

    async def list(self, *args: Any, **kwargs: Any) -> Any:  # noqa: D102
        # No fail-over: list() is a health/status probe — it must reflect the
        # PRIMARY's real state, not Ollama's (a masked list makes a dead
        # cuda.local look healthy).
        return await self._primary.list(*args, **kwargs)


def _maybe_wrap_openai_fallback(client: LLMClient | None) -> LLMClient | None:
    """Wrap an OpenAI-compat client with the in-cluster Ollama fallback when
    ``llm_openai_fallback_enabled`` is on. No-op otherwise (byte-identical)."""
    if client is None or not settings.llm_openai_fallback_enabled:
        return client
    cache_key = "__openai_compat_fallback__"
    if cache_key not in _client_cache:
        fallback = create_llm_client(settings.ollama_url)
        _client_cache[cache_key] = _OpenAICompatFallbackClient(client, fallback)  # type: ignore[assignment]
    return _client_cache.get(cache_key)


def get_dedicated_client(url: str) -> LLMClient:
    """Client bound to an explicit URL, bypassing the OpenAI-tier short-circuit.

    For callers with their own dedicated endpoint (e.g. the router's
    ``AGENT_ROUTER_URL``) that must NOT follow the agent tier to an external
    OpenAI-compatible API: those callers use small local model names which
    external APIs reject with 400. llama-server ignores the requested model
    name, which masked this for local deployments.
    """
    return _make_client_with_fallback(url)


def get_default_client() -> LLMClient:
    """Return the client for the default chat tier.

    When ``LLM_OPENAI_BASE_URL`` is set and the chat tier opts in (default: yes),
    routes through the OpenAI-compatible endpoint (llama-server). Otherwise
    falls back to the Ollama URL with transparent OLLAMA_FALLBACK_URL retry.
    """
    if use_openai_for_tier("chat"):
        client = get_openai_compat_client()
        if client is not None:
            return _maybe_wrap_openai_fallback(client)  # type: ignore[return-value]
    return _make_client_with_fallback(settings.ollama_url)


class _CountingEmbedClient:
    """Wrap the embed client so a raising ``embeddings()`` is counted.

    Every consumer of embeddings (semantic router, memory retrieval, KG,
    episodic memory, intent feedback) catches the exception, logs a WARNING
    and degrades to "no context". That is the right per-turn behaviour, but
    it means a dead embedding endpoint leaves no metric anywhere: in 2026-09
    the embed model failed to load for two days and the only trace was one
    WARNING per call in the pod log. Counting here, at the single chokepoint
    every consumer goes through, makes it alertable. The exception is
    re-raised unchanged; ``inner`` exposes the wrapped client.
    """

    def __init__(self, inner: LLMClient) -> None:
        self.inner = inner

    async def embeddings(self, *args: Any, **kwargs: Any) -> Any:  # noqa: D102
        try:
            return await self.inner.embeddings(*args, **kwargs)
        except Exception:
            from utils.metrics import record_embedding_error

            record_embedding_error(self._model_label(args, kwargs))
            raise

    @staticmethod
    def _model_label(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
        """Resolve the ``model`` label from either calling convention.

        Every call site today passes ``model=`` as a keyword, but ``model`` is
        the FIRST positional parameter of the ``LLMClient.embeddings``
        protocol. Reading only ``kwargs`` would silently label a future
        positional caller ``""`` — a metric that exists but cannot be grouped
        by model is worse than one that is obviously missing.
        """
        model = kwargs.get("model")
        if model is None and args:
            model = args[0]
        return str(model or "")

    async def chat(self, *args: Any, **kwargs: Any) -> Any:  # noqa: D102
        return await self.inner.chat(*args, **kwargs)

    async def list(self, *args: Any, **kwargs: Any) -> Any:  # noqa: D102
        return await self.inner.list(*args, **kwargs)

    async def generate(self, *args: Any, **kwargs: Any) -> Any:  # noqa: D102
        return await self.inner.generate(*args, **kwargs)


def get_intent_client() -> LLMClient:
    """Return the client for the small/fast "intent" tier.

    The intent tier is short classification work — agent-role routing, the
    OCR-gibberish verdict, follow-up chips. None of it needs the main model,
    and running it there costs a slot on the primary GPU and pollutes that
    server's prefix cache with one-off prompts.

    ``llm_openai_for_intent`` decides where it goes:

    - unset / True  → the OpenAI-compat endpoint, i.e. the main llama-server.
      That is the pre-2026-09 behaviour and stays the default, so this function
      is a no-op until the flag is set.
    - False         → the Ollama endpoint (``ollama_url``), which serves the
      dedicated ``ollama_intent_model``.

    Before this existed the flag was dead config: only the ``chat`` and
    ``agent`` tiers were ever passed to ``use_openai_for_tier``, so
    ``LLM_OPENAI_FOR_INTENT`` could be set to anything with no effect, and the
    intent consumers silently ran on the main model. The model NAME was still
    read from ``ollama_intent_model`` and handed to llama-server, which ignores
    a requested model name — so the setting looked configured and did nothing.
    """
    if use_openai_for_tier("intent"):
        client = get_openai_compat_client()
        if client is not None:
            return _maybe_wrap_openai_fallback(client)  # type: ignore[return-value]
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
        return _CountingEmbedClient(embed_oa)  # type: ignore[return-value]
    if settings.ollama_embed_url:
        return _CountingEmbedClient(_make_client_with_fallback(settings.ollama_embed_url))  # type: ignore[return-value]
    return _CountingEmbedClient(_make_client_with_fallback(settings.ollama_url))  # type: ignore[return-value]


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
            return _maybe_wrap_openai_fallback(client), settings.llm_openai_base_url or ""  # type: ignore[return-value]
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
    """Mimics ollama.ChatResponse with .message attribute.

    ``done_reason`` carries why generation stopped, using ollama's vocabulary
    (``"stop"`` / ``"length"``). OpenAI calls the same thing ``finish_reason``
    and the values coincide for the cases we care about, so the adapter maps it
    straight through. Without it a completion cut off at ``max_tokens`` is
    indistinguishable from one the model chose to end — see
    ``response_was_truncated``.
    """

    __slots__ = ("message", "model", "done", "done_reason")

    def __init__(
        self, message: _OllamaShapedMessage, model: str, done_reason: str | None = None
    ) -> None:
        self.message = message
        self.model = model
        self.done = True
        self.done_reason = done_reason


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
        fmt = kwargs.get("format")
        if fmt == "json":
            oa["response_format"] = {"type": "json_object"}
        elif isinstance(fmt, dict):
            # A JSON Schema → constrained decoding. llama-server enforces the
            # schema (verified: json_object is NOT enforced on this build, but
            # json_schema IS — see the typed-contracts Phase-1 spike). Guarantees
            # a schema-conforming, always-parseable response.
            oa["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "schema", "schema": fmt, "strict": True},
            }
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
        if settings.llm_openai_reasoning_effort:
            # Cap reasoning-model effort (see config.llm_openai_reasoning_effort).
            # Single emit point: both the blocking and streaming paths below
            # pass this extra_body through.
            extra_body["reasoning_effort"] = settings.llm_openai_reasoning_effort

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
                done_reason=getattr(choice, "finish_reason", None),
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
            done_reason=getattr(choice, "finish_reason", None) if choice else None,
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


def effective_agent_num_ctx() -> int:
    """Context window the agent tier can actually fill.

    The OpenAI-compat server (llama-server) ignores client-side ``num_ctx`` —
    its ``--ctx-size`` governs — so when the agent tier routes there AND the
    operator declared that size via ``llm_openai_num_ctx``, the backend token
    budget may fill it. Everywhere else (Ollama routing, or the setting unset)
    the budget stays at ``ollama_num_ctx``. NOTE: this is a budget bound only;
    with ``llm_openai_fallback_enabled`` a prompt wider than ``ollama_num_ctx``
    degrades on fail-over (see ``_OpenAICompatFallbackClient``).
    """
    if settings.llm_openai_num_ctx and use_openai_for_tier("agent"):
        return settings.llm_openai_num_ctx
    return settings.ollama_num_ctx


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


def response_was_truncated(response: Any) -> bool:
    """True when the model stopped because it hit the output-token cap.

    Both back-ends report this, under different names — ollama on
    ``response.done_reason``, OpenAI-compatible servers on the choice's
    ``finish_reason`` (which the adapter maps onto ``done_reason``). Either way
    the value is ``"length"``.

    This distinction is not cosmetic: a capped completion is delivered to the
    user as a normal answer that simply stops mid-sentence, with nothing in the
    logs to say why. Callers are expected to at least record it.

    Unknown/absent reason → False: never claim truncation we cannot see.
    """
    reason = getattr(response, "done_reason", None)
    return isinstance(reason, str) and reason.strip().lower() == "length"


# ---------------------------------------------------------------------------
# Strict-JSON extractor plumbing shared by the LLM extractors
# (schicht_a_extractor delegates here; pdf_split_detector consumes directly;
# meeting_minutes / paperless_metadata_extractor still carry local legacy
# copies — migrate them here when next touched).
# ---------------------------------------------------------------------------

def parse_llm_json(raw: str) -> dict | None:
    """Parse an LLM response to a dict; tolerate markdown fences + surrounding
    prose. Returns None if nothing parseable.

    Defense-in-depth: if the strict parse fails — overwhelmingly because the
    response was truncated at the token cap mid-JSON (unbalanced braces / an
    unterminated string) — fall back to :func:`salvage_truncated_json` so the
    complete leading entries survive instead of discarding the whole batch
    (the failure that once cost a document all 14 of its Schicht-A facts)."""
    import json

    if not raw:
        return None
    text = raw.strip()
    # A fenced JSON block ANYWHERE wins first: a prose preamble may contain a
    # stray '{' that would poison the first-brace slice below (e.g.
    # "Analyse: {mehrere} Dokumente.\n```json\n{...}\n```").
    import re as _re

    fence = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, _re.DOTALL)
    if fence:
        try:
            parsed = json.loads(fence.group(1))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass  # fall through to the slice + salvage path
    if text.startswith("```"):
        nl = text.find("\n")
        if nl >= 0:
            text = text[nl + 1:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    first = text.find("{")
    if first < 0:
        return None
    body = text[first:]
    last = body.rfind("}")
    if last > 0:
        try:
            parsed = json.loads(body[:last + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    salvaged = salvage_truncated_json(body)
    if salvaged is not None:
        logger.warning(
            "parse_llm_json: response unparseable (likely truncated at the "
            "token cap) — salvaged the complete leading entries"
        )
    return salvaged


def salvage_truncated_json(s: str) -> dict | None:
    """Best-effort recovery of a truncated JSON object.

    Scan with a bracket stack and remember the last position that sits on a clean
    boundary, cut there, drop the half-written trailing entry, and append the
    missing closers. Returns a dict or None.

    A boundary is recorded in two cases:
      1. just after a nested container closes (``}``/``]`` with a parent still
         open), and
      2. at a comma **between elements of the OUTERMOST array** only
         (``arr_depth == 1``).

    The depth gate on (2) is the load-bearing rule: a comma inside a *nested*
    array (``arr_depth >= 2``, e.g. ``"items":[10,20,30,40``) is NOT a cut point,
    so a truncated nested array is never force-closed as a complete (wrong)
    value — its whole enclosing element is dropped via (1) instead. (1) alone
    can't recover the elements of a truncated top-level array of SCALARS (they
    don't close with a bracket), which is why (2) is kept rather than removed.
    A truncated FIRST element recovers nothing (None beats corrupt)."""
    import json

    if not s or s[0] != "{":
        return None
    stack: list[str] = []
    arr_depth = 0  # number of currently-open '[' (so we can gate comma cuts)
    in_str = esc = False
    cut: int | None = None
    for i, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            stack.append(ch)
        elif ch == "[":
            stack.append(ch)
            arr_depth += 1
        elif ch == "}":
            if stack:
                stack.pop()
            if stack:  # closed a nested container, parent still open → clean cut
                cut = i + 1
        elif ch == "]":
            if stack:
                stack.pop()
            if arr_depth > 0:
                arr_depth -= 1
            if stack:
                cut = i + 1
        elif ch == "," and arr_depth == 1 and stack and stack[-1] == "[":
            # between elements of the OUTERMOST array — a clean boundary.
            # Gated to depth 1 so a truncated nested array can't be cut here.
            cut = i
    if cut is None:
        return None
    candidate = s[:cut]
    # Recompute the still-open containers for the candidate and close them.
    st: list[str] = []
    in_str = esc = False
    for ch in candidate:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            st.append(ch)
        elif ch in "}]":
            if st:
                st.pop()
    closers = "".join("}" if c == "{" else "]" for c in reversed(st))
    try:
        parsed = json.loads(candidate + closers)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
