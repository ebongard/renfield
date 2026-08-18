"""PDF-split error taxonomy (import-light — the worker imports this module to
extend its transient-exception classification without pulling the detector's
LLM/prompt import graph).

Two-way contract with the document worker's PEL-retry semantics:

- :class:`SplitExecutionError` — TERMINAL. A split could not be executed for a
  reason a retry won't fix (a child ingest failed terminally, an invalid piece
  list). The worker marks the parent failed and acks; the entry-point re-push →
  REINGEST path remains the deliberate retry.
- :class:`SplitTransientError` — RETRYABLE. The infrastructure blinked (LLM
  host down mid-detection, disk-full persisting a child, a lost create race).
  The worker leaves the entry in the PEL so reclaim re-delivers it and the
  idempotent resume continues where the last run stopped.
"""
from __future__ import annotations

import asyncio

import httpx

try:  # mirror the worker's guard — ollama is in the backend import graph
    from ollama import ResponseError as _OllamaResponseError
except Exception:  # pragma: no cover - defensive
    _OllamaResponseError = None


class SplitExecutionError(RuntimeError):
    """Terminal split failure — see module docstring."""


class SplitTransientError(SplitExecutionError):
    """Retryable split failure — the worker's PEL-retry taxonomy treats this
    as transient (it is listed in ``_TRANSIENT_EXC``)."""


def is_llm_transient(exc: BaseException) -> bool:
    """True when a boundary-detection failure is an infra blip that warrants a
    PEL retry instead of silently committing a single-document verdict.

    Covers BOTH production LLM client shapes (mirrors
    ``utils.llm_client._should_fallback``'s classification):

    - the OpenAI-compat client (``LLM_OPENAI_BASE_URL`` → cuda.local
      llama-server, the deployment's documented recurring outage) raises
      ``openai.APIConnectionError``/``APITimeoutError`` (timeout is a SUBCLASS
      of connection-error) and ``openai.APIStatusError`` — often with the raw
      httpx error only chained via ``__cause__``, so the chain is walked;
    - the bare ollama client raises httpx transport errors directly, or
      ``ollama.ResponseError`` with a status code.

    Unlike ``_should_fallback`` (which deliberately keeps read/pool timeouts on
    the primary), ANY timeout is transient here: for detection the alternative
    to a retry is a PERMANENT wrong single-document verdict, not a degraded
    answer. 4xx statuses stay terminal (our own bad request — retrying can't
    fix it, and the single-doc fallback is the safe outcome).
    """
    try:
        import openai
    except Exception:  # noqa: BLE001 - classification degrades gracefully
        openai = None  # type: ignore[assignment]

    seen: set[int] = set()
    e: BaseException | None = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if isinstance(e, (asyncio.TimeoutError, ConnectionError)):
            return True
        # httpx.TransportError covers Connect/Read/Write/Pool timeouts AND
        # ConnectError/ReadError/WriteError/RemoteProtocolError.
        if isinstance(e, httpx.TransportError):
            return True
        if _OllamaResponseError is not None and isinstance(e, _OllamaResponseError):
            # 5xx = host reachable but degraded (model loading, gateway) →
            # retry; 4xx = config/data error → terminal for this attempt.
            return getattr(e, "status_code", 0) >= 500
        if openai is not None:
            if isinstance(e, openai.APIConnectionError):
                return True
            if isinstance(e, openai.APIStatusError):
                return getattr(e, "status_code", 0) >= 500
        e = e.__cause__
    return False
