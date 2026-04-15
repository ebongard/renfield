"""HA-specific intent-context and post-classification validation.

Phase 1 Week 2 follow-up to the Day 4 intent_fallback hook extraction.
Moves the last two remaining HomeAssistantClient-touching code blocks
out of `services/ollama_service.py` (a platform file) into ha_glue.

Two hooks are exposed:

1. **`build_entity_context`** — fired before the intent classifier
   runs, injects a formatted "available HA entities" block into the
   prompt so the LLM has a concrete entity list to choose from. This
   handler owns the message-keyword filtering (German device words →
   HA domains), room-context prioritization, and the 25-entity
   truncation that used to live inline in
   `OllamaService._build_entity_context`.

2. **`validate_classified_intent`** — fired after the intent
   classifier returns a structured intent, before the caller receives
   it. Checks whether an `homeassistant.*` intent is actually backed
   by HA keywords in the message; if not, overrides the classification
   to `general.conversation` so the agent loop doesn't execute a
   bogus HA command. This was the last piece of "HA leakage" inside
   `OllamaService.extract_intent`.

Both hooks early-return `None` for messages that don't match their
domain, so platform-only deploys (where ha_glue isn't loaded) and
pro deploys (where ha_glue is loaded but smart_home=False so the
bootstrap never registers the handlers) both fall through cleanly.

After this module lands, `integrations/homeassistant.py` has exactly
zero platform-side consumers and can move to `ha_glue/integrations/`
as a pure file relocation in a follow-up commit.
"""

from __future__ import annotations

from typing import Any

from loguru import logger


# German device keyword → HA domain mapping. Pre-computed once at module
# load so `_filter_entities_by_message` runs in O(words × domains) instead
# of scanning the dict on every call.
_DEVICE_KEYWORDS_TO_DOMAINS: dict[str, tuple[str, ...]] = {
    "fenster": ("binary_sensor", "sensor"),
    "tür": ("binary_sensor",),
    "licht": ("light",),
    "lampe": ("light",),
    "schalter": ("switch",),
    "heizung": ("climate",),
    "thermostat": ("climate",),
    "rolladen": ("cover",),
    "jalousie": ("cover",),
    "mediaplayer": ("media_player",),
    "player": ("media_player",),
    "fernseher": ("media_player",),
    "tv": ("media_player",),
    "musik": ("media_player",),
    "spotify": ("media_player",),
    "radio": ("media_player",),
}


# ---------------------------------------------------------------------------
# build_entity_context handler
# ---------------------------------------------------------------------------


async def ha_build_entity_context(
    *,
    message: str,
    room_context: dict | None = None,
    lang: str = "de",
) -> str | None:
    """Build an HA-entity-list context block for the intent prompt.

    Fetches the full HA entity map, ranks each entity by relevance to
    the user's message (room match, friendly-name word overlap, device
    domain match), returns the top 25 as a formatted prompt block.

    Returns None if HA is unreachable or returns an empty entity map —
    the platform falls through to an empty context string in that case,
    same behavior as the pre-extraction inline version.
    """
    try:
        from ha_glue.integrations.homeassistant import HomeAssistantClient
        ha_client = HomeAssistantClient()
        entity_map = await ha_client.get_entity_map()
    except Exception as exc:  # noqa: BLE001
        logger.error(f"❌ ha_glue intent_context: entity fetch failed: {exc}")
        return None

    if not entity_map:
        return "VERFÜGBARE ENTITIES: (Keine - Home Assistant nicht erreichbar)"

    message_lower = message.lower()

    current_room = None
    current_room_normalized = None
    if room_context:
        room_name = room_context.get("room_name")
        if room_name:
            current_room = room_name.lower()
            current_room_normalized = _normalize_umlauts(current_room)

    message_words = {w for w in message_lower.split() if len(w) > 2}

    matched_domains: set[str] = set()
    for keyword, domains in _DEVICE_KEYWORDS_TO_DOMAINS.items():
        if keyword in message_lower:
            matched_domains.update(domains)

    scored_entities: list[tuple[int, dict[str, Any]]] = []
    for entity in entity_map:
        score = 0
        entity_room = (entity.get("room") or "").lower()

        if current_room and entity_room:
            entity_room_normalized = _normalize_umlauts(entity_room)
            if current_room in entity_room or current_room_normalized in entity_room_normalized:
                score += 20

        if entity_room and entity_room in message_lower:
            score += 10

        friendly_name_lower = (entity.get("friendly_name") or "").lower()
        friendly_words = set(friendly_name_lower.split())
        overlap = message_words & friendly_words
        if overlap:
            score += 5 * len(overlap)

        if entity.get("domain") in matched_domains:
            score += 8

        if score > 0:
            scored_entities.append((score, entity))

    scored_entities.sort(key=lambda x: x[0], reverse=True)
    top_entities: list[dict[str, Any]] = [e[1] for e in scored_entities[:25]]

    # Fallback: if nothing matched, surface up to 25 entities across
    # distinct domains so the LLM at least sees something to choose from.
    if not top_entities:
        seen_domains: set[str] = set()
        for entity in entity_map:
            domain = entity.get("domain")
            if domain not in seen_domains or len(top_entities) < 25:
                top_entities.append(entity)
                seen_domains.add(domain)
                if len(top_entities) >= 25:
                    break

    context_lines = ["VERFÜGBARE HOME ASSISTANT ENTITIES:"]
    context_lines.append(
        "  [Für MCP HA-Tools: Nutze 'name' = friendly_name, 'area' = Raum-Name]"
    )

    if current_room:
        context_lines.append(
            f"  [Entitäten im aktuellen Raum '{current_room}' haben Priorität]"
        )

    for entity in top_entities:
        entity_room = (entity.get("room") or "").lower()
        is_current_room = bool(current_room and entity_room and current_room in entity_room)

        room_info = f", area: \"{entity['room']}\"" if entity.get("room") else ""
        state_info = f" [aktuell: {entity.get('state', 'unknown')}]"
        marker = " ★" if is_current_room else ""

        context_lines.append(
            f"  - name: \"{entity['friendly_name']}\"{room_info}{state_info}{marker}"
        )

    return "\n".join(context_lines)


def _normalize_umlauts(s: str) -> str:
    """Replace ä/ö/ü with a/o/u for Umlaut-tolerant string matching."""
    return s.replace("ä", "a").replace("ö", "o").replace("ü", "u")


# ---------------------------------------------------------------------------
# validate_classified_intent handler
# ---------------------------------------------------------------------------


# Minimal fallback keyword set — used when the live HA keyword endpoint
# is unreachable. Matches the legacy inline fallback in ollama_service.py.
_HA_FALLBACK_KEYWORDS: frozenset[str] = frozenset({
    "licht", "lampe", "schalter", "thermostat", "heizung",
    "fenster", "tür", "rolladen", "ein", "aus", "an", "schalten",
})


async def _get_ha_keywords() -> set[str]:
    """Fetch the live HA keyword set, falling back to the hardcoded list."""
    try:
        from ha_glue.integrations.homeassistant import HomeAssistantClient
        ha_client = HomeAssistantClient()
        return await ha_client.get_keywords()
    except Exception as exc:  # noqa: BLE001
        logger.error(f"❌ ha_glue intent_context: HA keyword fetch failed: {exc}")
        return set(_HA_FALLBACK_KEYWORDS)


async def ha_validate_classified_intent(
    *,
    intent_data: dict,
    message: str,
    lang: str = "de",
) -> dict | None:
    """Override a `homeassistant.*` classification that lacks HA keywords.

    The LLM intent classifier sometimes picks a `homeassistant.*` intent
    for messages that don't actually reference smart-home concepts (e.g.
    "schalte mich ab" → `homeassistant.turn_off` when the user really
    meant a casual conversational response). This handler checks the
    message against a keyword set and, if no HA-shaped words are found,
    overrides the classification to `general.conversation` so the agent
    loop stays out of the HA command path.

    Returns None when:
    - The intent is not HA-prefixed (not our domain)
    - The message contains at least one HA keyword (classification is valid)

    Returns an override dict only when the validation fails.
    """
    if not intent_data.get("intent", "").startswith("homeassistant."):
        return None

    ha_keywords = await _get_ha_keywords()
    message_lower = message.lower()
    has_ha_keyword = any(keyword in message_lower for keyword in ha_keywords)
    if has_ha_keyword:
        return None

    logger.info(
        f"⚠️  ha_glue intent_context: {intent_data.get('intent')} overridden to "
        f"general.conversation (no HA keywords in '{message[:50]}')"
    )
    return {
        "intent": "general.conversation",
        "parameters": {},
        "confidence": 1.0,
    }
