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


# LLM-client infrastructure blips during boundary detection. Deliberately a
# subset of the worker's _TRANSIENT_EXC: detection talks only to the LLM host,
# so DB/Redis exception types are not wrapped here (they propagate raw and the
# worker classifies them itself).
_LLM_TRANSIENT_EXC: tuple[type[BaseException], ...] = (
    asyncio.TimeoutError,
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)


def is_llm_transient(exc: BaseException) -> bool:
    """True when a boundary-detection failure is an infra blip that warrants a
    PEL retry instead of silently committing a single-document verdict."""
    if isinstance(exc, _LLM_TRANSIENT_EXC):
        return True
    if _OllamaResponseError is not None and isinstance(exc, _OllamaResponseError):
        # 5xx = host reachable but degraded (model loading, gateway) → retry;
        # 4xx = config/data error → terminal for the detection attempt.
        return getattr(exc, "status_code", 0) >= 500
    return False
