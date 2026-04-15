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
    # Late shutdown phase — fires AFTER MCP shutdown, zeroconf stop, and
    # all other platform-owned teardown. Used by plugins that need to
    # clean up dependencies the platform was still using during its own
    # shutdown (e.g. ha_glue's HA/Frigate HTTP client singletons, which
    # MCP may still invoke during its shutdown sequence). Handlers
    # receive `app` as the only kwarg. Exceptions from handlers are
    # logged and ignored — late-shutdown must never block pod exit.
    "shutdown_finalize",
    "register_routes",
    "register_tools",
    "execute_tool",
    "post_message",
    "post_document_ingest",
    "retrieve_context",
    "pre_agent_context",
    "pre_save_message",
    "presence_enter_room",
    "presence_leave_room",
    "presence_first_arrived",
    "presence_last_left",
    "compact_mcp_result",
    "authenticate",
    # Intent classification fallback — fired by the LLM intent dispatcher
    # when JSON parsing fails and a domain-specific consumer (e.g. HA via
    # ha_glue) might still recognize the user's intent from raw keywords.
    # Handlers receive `message: str, lang: str` and return a dict
    # `{"intent": str, "parameters": dict, "confidence": float}` on success
    # or None to fall through. First well-shaped non-None result wins —
    # registration order determines precedence, so earlier-registered
    # handlers shadow later ones for the same input. The call site
    # validates each candidate is a dict with an "intent" key before
    # accepting it.
    "intent_fallback_resolve",
    # Entity context for intent classification — fired by OllamaService
    # .extract_intent to build a domain-specific "available entities" block
    # that gets injected into the intent prompt. Handlers receive
    # `message: str, room_context: dict | None, lang: str` and return a
    # formatted string (multi-line prompt context) or None. First
    # well-shaped non-None result wins. Empty string fall-through means
    # "no domain context available" and the intent prompt is built without
    # an entity list. ha_glue's handler returns the HA entity list filtered
    # by message keywords.
    "build_entity_context",
    # Post-classification validation — fired by OllamaService.extract_intent
    # AFTER the LLM returns a structured intent. Handlers receive
    # `intent_data: dict, message: str, lang: str` and return EITHER an
    # override dict (`{"intent": ..., "parameters": ..., "confidence": ...}`)
    # that replaces the classification, OR None to leave the classification
    # unchanged. First well-shaped non-None override wins — registration
    # order determines precedence. ha_glue's handler uses this to validate
    # `homeassistant.*` intents against an HA keyword set and fall back to
    # `general.conversation` when the message doesn't actually contain
    # HA-shaped words.
    "validate_classified_intent",
    # Chat message context established — fired by the WebSocket chat
    # handler when an authenticated user's message starts processing and
    # a room context is known. Kwargs: `user_id: int, room_id: int,
    # room_name: str | None, lang: str`. Handlers return nothing. Used
    # by ha_glue to register BLE voice-auth presence when a user speaks
    # from a known room. Platform default (no handler) is a no-op.
    "chat_context_established",
    # Privacy-aware TTS gate for notifications — fired by the notification
    # service when delivering a non-public notification that may require
    # suppression based on room occupancy. Kwargs: `privacy: str,
    # target_user_id: int | None, room_id: int | None`. Handlers return
    # `bool` — True if TTS allowed, False to suppress. First well-shaped
    # non-None result wins. Platform default (no handler) falls back to
    # "suppress non-public TTS" as fail-safe.
    "should_play_tts_for_notification",
    # Resolve a user's current room — fired by the notification service
    # when a notification has a target user but no explicit room. Kwargs:
    # `user_id: int`. Handlers return a dict
    # `{"room_id": int, "room_name": str}` or None. First well-shaped
    # non-None result wins. Platform default (no handler) leaves the
    # notification un-roomed.
    "resolve_user_current_room",
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
