"""
Minimal async hook system for the Open-Core plugin architecture.

Plugins (e.g. renfield-twin) register async callbacks for well-known
lifecycle events. Renfield never crashes due to a plugin error — each
hook is wrapped in try/except.
"""

from collections import defaultdict
from collections.abc import Callable, Coroutine
from typing import Any

from loguru import logger

HOOK_EVENTS: frozenset[str] = frozenset({
    "startup",
    "shutdown",
    "register_routes",
    "register_tools",
    "post_message",
    "post_document_ingest",
    "retrieve_context",
    "presence_enter_room",
    "presence_leave_room",
    "presence_first_arrived",
    "presence_last_left",
})

HookFn = Callable[..., Coroutine[Any, Any, Any]]

_hooks: dict[str, list[HookFn]] = defaultdict(list)


def register_hook(event: str, fn: HookFn) -> None:
    """Register an async callback for *event*. Raises ValueError for unknown events."""
    if event not in HOOK_EVENTS:
        raise ValueError(f"Unknown hook event {event!r}. Valid: {sorted(HOOK_EVENTS)}")
    _hooks[event].append(fn)
    logger.debug(f"Hook registered: {event} → {getattr(fn, '__qualname__', repr(fn))}")


async def run_hooks(event: str, **kwargs: Any) -> list[Any]:
    """Run all hooks for *event*, return non-None results. Never raises."""
    results: list[Any] = []
    for fn in _hooks.get(event, []):
        try:
            result = await fn(**kwargs)
            if result is not None:
                results.append(result)
        except Exception:
            logger.opt(exception=True).warning(
                f"Hook {getattr(fn, '__qualname__', repr(fn))} failed for {event}"
            )
    return results


def clear_hooks() -> None:
    """Remove all registered hooks. Used for test isolation."""
    _hooks.clear()
