"""Handler registry for the Scheduled Tasks engine (#1137).

A 1:1 ``handler_key`` → code-handler map (mirrors ``services/domain_contract.py``).
A ``ScheduledTask`` row names a ``handler_key``; the engine resolves it here and
runs the handler with the row's ``params``. Keeping the mapping in code (not the
DB) means removing a handler can't be undone by a stale DB row — the engine
treats an unresolved ``handler_key`` as a skip, never a crash (Review D3/D4).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from fastapi import FastAPI

# A handler runs ONE tick of a task. It receives the FastAPI app (for app.state
# dependencies like ``mcp_manager``) and the task's ``params`` dict, and returns
# an optional human-readable detail string (logged / surfaced). RAISING signals
# failure — the engine records ``last_status='error'`` and keeps scheduling.
Handler = Callable[["FastAPI", dict], Awaitable[str | None]]

# An optional per-handler param validator: given the params dict, raise
# ``ValueError`` on an invalid value. Used by the Phase-2 write routes so a bad
# ``params`` is rejected at edit time, not only at handler runtime (Review D8).
ParamValidator = Callable[[dict], None]


@dataclass(frozen=True)
class HandlerSpec:
    key: str
    handler: Handler
    validate_params: ParamValidator | None = None


_REGISTRY: dict[str, HandlerSpec] = {}


def register_handler(
    key: str,
    handler: Handler,
    *,
    validate_params: ParamValidator | None = None,
) -> None:
    """Register (or replace) a handler under ``key``. Idempotent — safe to call
    on every boot (registration happens at import/lifespan time)."""
    _REGISTRY[key] = HandlerSpec(key=key, handler=handler, validate_params=validate_params)
    logger.debug(f"scheduled-task handler registered: {key}")


def get_handler(key: str) -> HandlerSpec | None:
    return _REGISTRY.get(key)


def all_handler_keys() -> list[str]:
    return sorted(_REGISTRY)


def validate_params(key: str, params: dict[str, Any]) -> None:
    """Validate ``params`` for a handler; raise ValueError on a bad value or an
    unknown ``handler_key``. A handler with no validator accepts any params."""
    spec = _REGISTRY.get(key)
    if spec is None:
        raise ValueError(f"unknown handler_key: {key}")
    if spec.validate_params is not None:
        spec.validate_params(params)


def clear_handlers() -> None:
    """Test hook — reset the registry."""
    _REGISTRY.clear()
