"""HA-keyword intent fallback for the platform's LLM intent classifier.

Registered as an `intent_fallback_resolve` hook handler at ha_glue
package import time (see `ha_glue/__init__.py`).

When the platform's LLM intent classifier (`OllamaService.extract_intent`)
fails to parse JSON from the model response — even after a retry — it
fires the `intent_fallback_resolve` hook to give domain-specific
consumers a last chance to recognize the intent before falling through
to `general.unresolved`. This module's handler implements the legacy
"detect German HA action keywords + look up entity in HA" path that
used to live inline in `services/ollama_service.py`.

Match algorithm
---------------
1. Lowercase the message.
2. Check for an action keyword (`schalte`, `mach`, `stelle`, etc).
3. Check for a device keyword (`licht`, `lampe`, `fenster`, etc).
4. If both present, query the HA entity index via
   `HomeAssistantClient.search_entities(message)` for a matching entity.
5. Pick the first match and synthesize a `homeassistant.turn_on/off/get_state`
   intent with `entity_id` as the parameter.

This is a deliberately narrow fallback: it only fires when the primary
classifier crashes on the JSON output AND the message looks unambiguously
HA-shaped. False positives (a non-HA message accidentally hitting both
keyword sets) get routed to a stale entity, which the downstream tool
call will fail visibly. False negatives (an HA message that doesn't
match the keyword sets) fall through to `general.unresolved` and the
agent loop picks it up.

Confidence
----------
Returned at 0.6 — lower than the primary classifier's typical 0.9 — so
the agent loop / orchestrator can deprioritize it if other signals
disagree.
"""

from __future__ import annotations

from loguru import logger


# German HA action keywords (verbs that indicate a smart-home command)
_HA_ACTION_KEYWORDS = (
    "schalte", "mach", "stelle", "ist", "zeige", "öffne", "schließe",
)

# German HA device keywords (nouns that indicate a smart-home target)
_HA_DEVICE_KEYWORDS = (
    "licht", "lampe", "fenster", "tür", "heizung", "rolladen",
)

# German "turn on" verbs
_HA_TURN_ON_KEYWORDS = ("ein", "an", "schalte ein")

# German "turn off" verbs
_HA_TURN_OFF_KEYWORDS = ("aus", "schalte aus")

# German "get state" verbs
_HA_GET_STATE_KEYWORDS = ("ist", "status", "zustand")


def _classify_action(message_lower: str) -> str:
    """Pick the HA intent verb based on which keywords appear in the message."""
    if any(word in message_lower for word in _HA_TURN_ON_KEYWORDS):
        return "homeassistant.turn_on"
    if any(word in message_lower for word in _HA_TURN_OFF_KEYWORDS):
        return "homeassistant.turn_off"
    if any(word in message_lower for word in _HA_GET_STATE_KEYWORDS):
        return "homeassistant.get_state"
    return "homeassistant.turn_on"  # default action


async def ha_intent_fallback(*, message: str, lang: str) -> dict | None:
    """Hook handler for `intent_fallback_resolve`.

    Args:
        message: The user's raw message text.
        lang: Language code (currently unused — keyword sets are German-only,
            English support would be a separate handler or an extended set).

    Returns:
        A dict with `{"intent": str, "parameters": dict, "confidence": float}`
        when the fallback matched, or `None` to defer to other handlers /
        the platform's `general.unresolved` default.
    """
    message_lower = message.lower()

    has_action = any(keyword in message_lower for keyword in _HA_ACTION_KEYWORDS)
    has_device = any(keyword in message_lower for keyword in _HA_DEVICE_KEYWORDS)
    if not (has_action and has_device):
        return None

    logger.warning(
        "⚠️  ha_glue intent_fallback: HA-shaped message detected, attempting "
        "entity lookup via HomeAssistantClient"
    )

    # Late import — the platform side never touches `integrations.homeassistant`,
    # so we keep all HA client imports inside ha_glue. If HA itself is offline
    # this raises and the hook system catches + logs it; the fallback returns
    # None and the platform falls through to general.unresolved cleanly.
    from integrations.homeassistant import HomeAssistantClient
    ha_client = HomeAssistantClient()
    search_results = await ha_client.search_entities(message)

    if not search_results:
        return None

    entity_id = search_results[0]["entity_id"]
    intent = _classify_action(message_lower)
    logger.info(f"✅ ha_glue intent_fallback: {intent} mit Entity: {entity_id}")
    return {
        "intent": intent,
        "parameters": {"entity_id": entity_id},
        "confidence": 0.6,
    }
