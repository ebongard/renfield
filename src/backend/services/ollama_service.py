"""
Ollama Service - Lokales LLM

Provides LLM interaction with multilingual support (de/en).
Language can be specified per-call or defaults to system setting.
"""
import asyncio
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from integrations.core.plugin_registry import PluginRegistry
    from models.database import Message
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from services.prompt_manager import prompt_manager
from utils.circuit_breaker import llm_circuit_breaker
from utils.config import settings
from utils.llm_client import (
    extract_response_content,
    get_classification_chat_kwargs,
    get_default_client,
)


class OllamaService:
    """Service für Ollama LLM Interaktion mit Mehrsprachigkeit."""

    def __init__(self):
        self.client = get_default_client()

        # Multi-Modell Konfiguration
        self.model = settings.ollama_model  # Legacy
        self.chat_model = settings.ollama_chat_model
        self.rag_model = settings.ollama_rag_model
        self.embed_model = settings.ollama_embed_model
        self.intent_model = settings.ollama_intent_model

        # Default language from settings
        self.default_lang = settings.default_language

    def get_system_prompt(self, lang: str | None = None, memory_context: str | None = None) -> str:
        """Get system prompt for the specified language, optionally with memory context."""
        lang = lang or self.default_lang
        base = prompt_manager.get("chat", "system_prompt", lang=lang, default=self._default_system_prompt(lang))
        if memory_context:
            base += f"\n\n{memory_context}"
        return base

    def _default_system_prompt(self, lang: str = "de") -> str:
        """Fallback system prompt if YAML not available."""
        if lang == "en":
            return """You are Renfield, a fully offline-capable, self-hosted digital assistant.

Your capabilities:
- Control Home Assistant devices (lights, switches, sensors, etc.)
- Manage camera surveillance
- Execute n8n workflows
- Conduct research
- Manage tasks

IMPORTANT RULES FOR RESPONSES:
1. ALWAYS respond in natural English language
2. NEVER output JSON, code, or technical details
3. Be brief, friendly, and direct
4. If an action was executed, simply confirm it"""
        else:
            return """Du bist Renfield, ein vollständig offline-fähiger, selbst-gehosteter digitaler Assistent.

Deine Fähigkeiten:
- Home Assistant Geräte steuern (Lichter, Schalter, Sensoren, etc.)
- Kamera-Überwachung verwalten
- n8n Workflows ausführen
- Recherchen durchführen
- Aufgaben verwalten

WICHTIGE REGELN FÜR ANTWORTEN:
1. Antworte IMMER in natürlicher deutscher Sprache
2. Gib NIEMALS JSON, Code oder technische Details aus
3. Sei kurz, freundlich und direkt
4. Wenn eine Aktion ausgeführt wurde, bestätige dies einfach"""

    async def ensure_model_loaded(self) -> None:
        """Stelle sicher, dass das Modell geladen ist"""
        try:
            models = await self.client.list()
            # ollama>=0.4.0 uses Pydantic models with .model attribute
            model_names = [m.model for m in models.models]

            if self.model not in model_names:
                logger.info(f"Lade Modell {self.model}...")
                await self.client.pull(self.model)
                logger.info(f"Modell {self.model} geladen")
            else:
                logger.info(f"Modell {self.model} bereits vorhanden")
        except Exception as e:
            logger.error(f"Fehler beim Laden des Modells: {e}")
            raise

    async def chat(self, message: str, history: list[dict] = None, lang: str | None = None, memory_context: str | None = None) -> str:
        """
        Einfacher Chat (nicht-streamend) mit optionaler Konversationshistorie.

        Args:
            message: Die Benutzernachricht
            history: Optionale Konversationshistorie
            lang: Sprache für die Antwort (de/en). None = default_lang
            memory_context: Optional formatted memory section for the system prompt
        """
        lang = lang or self.default_lang

        # Check circuit breaker before LLM call
        if not llm_circuit_breaker.allow_request():
            logger.warning("🔴 LLM circuit breaker OPEN — rejecting chat request")
            return prompt_manager.get("chat", "error_fallback", lang=lang, default="LLM-Service vorübergehend nicht verfügbar.", error="Circuit Breaker aktiv")

        try:
            system_prompt = self.get_system_prompt(lang, memory_context=memory_context)
            messages = [{"role": "system", "content": system_prompt}]

            if history:
                messages.extend(history)

            messages.append({"role": "user", "content": message})

            response = await self.client.chat(
                model=self.model,
                messages=messages,
                options={"num_ctx": settings.ollama_num_ctx}
            )
            # ollama>=0.4.0 uses Pydantic models
            llm_circuit_breaker.record_success()
            return response.message.content
        except Exception as e:
            llm_circuit_breaker.record_failure()
            logger.error(f"Chat Fehler: {e}")
            return prompt_manager.get("chat", "error_fallback", lang=lang, default=f"Entschuldigung, es gab einen Fehler: {e!s}", error=str(e))

    async def chat_stream(self, message: str, history: list[dict] = None, lang: str | None = None, memory_context: str | None = None) -> AsyncGenerator[str, None]:
        """
        Streaming Chat with optional conversation history.

        Args:
            message: Die Benutzernachricht
            history: Optionale Konversationshistorie
            lang: Sprache für die Antwort (de/en). None = default_lang
            memory_context: Optional formatted memory section for the system prompt
        """
        lang = lang or self.default_lang

        # Check circuit breaker before LLM call
        if not llm_circuit_breaker.allow_request():
            logger.warning("🔴 LLM circuit breaker OPEN — rejecting stream request")
            yield prompt_manager.get("chat", "error_fallback", lang=lang, default="LLM-Service vorübergehend nicht verfügbar.", error="Circuit Breaker aktiv")
            return

        try:
            system_prompt = self.get_system_prompt(lang, memory_context=memory_context)
            messages = [{"role": "system", "content": system_prompt}]

            if history:
                messages.extend(history)

            messages.append({"role": "user", "content": message})

            async for chunk in await self.client.chat(
                model=self.model,
                messages=messages,
                stream=True,
                options={"num_ctx": settings.ollama_num_ctx}
            ):
                # ollama>=0.4.0 uses Pydantic models
                if chunk.message and chunk.message.content:
                    yield chunk.message.content

            # Record success after successful streaming
            llm_circuit_breaker.record_success()
        except Exception as e:
            llm_circuit_breaker.record_failure()
            logger.error(f"Streaming Fehler: {e}")
            yield prompt_manager.get("chat", "error_fallback", lang=lang, default=f"Fehler: {e!s}", error=str(e))

    async def extract_intent(
        self,
        message: str,
        plugin_registry=None,
        room_context: dict | None = None,
        conversation_history: list[dict] | None = None,
        lang: str | None = None
    ) -> dict:
        """
        Extrahiere Intent und Parameter aus Nachricht mit Plugin-Unterstützung.

        Args:
            message: Die Benutzernachricht
            plugin_registry: Optional Plugin Registry für zusätzliche Intents
            room_context: Optional Room Context mit Informationen wie:
                - room_name: Name des Raums in dem sich das Gerät befindet
                - room_id: Datenbank-ID des Raums
                - device_type: Typ des Geräts (satellite, web_panel, etc.)
                - speaker_name: Name des erkannten Sprechers (optional)
            conversation_history: Optional conversation history for resolving
                pronouns and references like "dort", "es", "das", "dafür"
            lang: Language for prompts (de/en). None = default_lang

        Returns:
            Dict mit intent, parameters und confidence
        """
        lang = lang or self.default_lang

        # Build dynamic intent types from IntentRegistry
        from services.intent_registry import intent_registry

        # Set plugin registry if provided (for dynamic plugin intents)
        if plugin_registry:
            intent_registry.set_plugin_registry(plugin_registry)

        # Run entity context + correction lookup in parallel (both are I/O-bound)
        entity_context, correction_examples = await asyncio.gather(
            self._build_entity_context(message, room_context),
            self._find_correction_examples(message, lang),
        )

        # Build intent types and examples dynamically
        intent_types = intent_registry.build_intent_prompt(lang=lang)
        examples = intent_registry.build_examples_prompt(lang=lang, max_examples=15)

        # Build room context for the prompt
        room_context_prompt = ""
        if room_context:
            room_name = room_context.get("room_name", "")
            speaker_name = room_context.get("speaker_name", "")

            if room_name:
                room_context_prompt = prompt_manager.get(
                    "intent", "room_context_template", lang=lang, room_name=room_name
                )

            if speaker_name:
                room_context_prompt += "\n" + prompt_manager.get(
                    "intent", "speaker_context_template", lang=lang, speaker_name=speaker_name
                )

        # Build conversation history context for reference resolution
        history_context_prompt = ""
        if conversation_history:
            # With 32k context, include more history for better reference resolution
            recent_history = conversation_history[-6:]
            if recent_history:
                history_lines = []
                for msg in recent_history:
                    role = "User" if lang == "en" else "Nutzer"
                    if msg.get("role") != "user":
                        role = "Assistant" if lang == "en" else "Assistent"
                    content = msg.get("content", "")[:2000]
                    history_lines.append(f"  {role}: {content}")

                history_context_prompt = prompt_manager.get(
                    "intent", "history_context_template", lang=lang,
                    history_lines="\n".join(history_lines)
                )

        # Build the full prompt from externalized template
        prompt = prompt_manager.get(
            "intent", "extraction_prompt", lang=lang,
            message=message,
            room_context=room_context_prompt,
            history_context=history_context_prompt,
            intent_types=intent_types,
            examples=examples,
            entity_context=entity_context,
            correction_examples=correction_examples
        )

        try:
            # Use externalized system message and LLM options
            json_system_message = prompt_manager.get("intent", "json_system_message", lang=lang, default="Reply with JSON only.")
            llm_options = prompt_manager.get_config("intent", "llm_options") or {}

            messages = [
                {
                    "role": "system",
                    "content": json_system_message
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ]

            llm_call_options = {
                "temperature": llm_options.get("temperature", 0.0),
                "top_p": llm_options.get("top_p", 0.1),
                "num_predict": llm_options.get("num_predict", 500),
                "num_ctx": llm_options.get("num_ctx", settings.ollama_num_ctx),
            }

            prompt_length = len(json_system_message) + len(prompt)
            logger.debug(f"Intent prompt length: ~{prompt_length} chars (~{prompt_length // 4} tokens est.)")

            # Option A: Disable thinking mode for intent classification
            classification_kwargs = get_classification_chat_kwargs(self.model)
            response_data = await self.client.chat(
                model=self.model,
                messages=messages,
                options=llm_call_options,
                **classification_kwargs,
            )
            # Option B: Failsafe for empty content with thinking
            response = extract_response_content(response_data)

            # Retry once on empty response (model may have failed silently)
            if not response or not response.strip():
                logger.warning("⚠️ LLM returned empty response, retrying with higher num_predict...")
                retry_options = {**llm_call_options, "num_predict": 500}
                response_data = await self.client.chat(
                    model=self.model,
                    messages=messages,
                    options=retry_options,
                    **classification_kwargs,
                )
                response = extract_response_content(response_data)

            # Robuste JSON-Extraktion
            import json
            import re

            logger.debug(f"Raw LLM response ({len(response) if response else 0} chars): {response[:300] if response else '(empty)'}")

            # Entferne Markdown-Code-Blocks
            response = response.strip()

            # Methode 1: Markdown Code-Block
            if "```" in response:
                match = re.search(r'```(?:json)?\s*(\{.*\})\s*```', response, re.DOTALL)
                if match:
                    response = match.group(1)
                else:
                    # Fallback: Nimm alles zwischen ersten ```
                    parts = response.split("```")
                    if len(parts) >= 2:
                        response = parts[1].strip()
                        if response.startswith("json"):
                            response = response[4:].strip()

            # Methode 2: Balanced braces extraction (supports nested objects/arrays)
            # Find the first { and match to its balanced closing }
            first_brace = response.find('{')
            if first_brace >= 0:
                depth = 0
                in_string = False
                escape_next = False
                end_pos = -1
                for i in range(first_brace, len(response)):
                    c = response[i]
                    if escape_next:
                        escape_next = False
                        continue
                    if c == '\\' and in_string:
                        escape_next = True
                        continue
                    if c == '"' and not escape_next:
                        in_string = not in_string
                        continue
                    if in_string:
                        continue
                    if c == '{':
                        depth += 1
                    elif c == '}':
                        depth -= 1
                        if depth == 0:
                            end_pos = i
                            break
                if end_pos > 0:
                    response = response[first_brace:end_pos + 1]

            # Parse JSON
            try:
                raw_data = json.loads(response)

                # Handle new ranked format: {"intents": [...]}
                # Normalize to single intent_data for backward compatibility
                if "intents" in raw_data and isinstance(raw_data["intents"], list) and raw_data["intents"]:
                    intents_list = raw_data["intents"]
                    # Sort by confidence descending, pick top intent
                    intents_list.sort(key=lambda x: x.get("confidence", 0), reverse=True)
                    intent_data = dict(intents_list[0])  # Copy to avoid mutation
                    intent_data.setdefault("parameters", {})
                    # Preserve full list as separate copies for extract_ranked_intents()
                    intent_data["_ranked_intents"] = [dict(i) for i in intents_list]
                else:
                    intent_data = raw_data
            except json.JSONDecodeError as e:
                logger.warning(f"⚠️ JSON Parse Error: {e}")
                logger.warning(f"Attempted to parse: {response[:200]}")
                intent_data = None

                # Retry once on truncated JSON (starts with { but incomplete)
                if response and response.strip().startswith('{'):
                    logger.warning("⚠️ Truncated JSON detected, retrying with higher num_predict...")
                    try:
                        retry_options = {**llm_call_options, "num_predict": 500}
                        response_data = await self.client.chat(
                            model=self.model,
                            messages=messages,
                            options=retry_options
                        )
                        retry_response = response_data.message.content
                        if retry_response and retry_response.strip():
                            intent_data = self._parse_intent_json(retry_response)
                            if intent_data:
                                logger.info(f"✅ Retry successful: {intent_data.get('intent')}")
                    except Exception as retry_err:
                        logger.warning(f"⚠️ Retry also failed: {retry_err}")

                if intent_data is None:
                    # Letzter Versuch: Prüfe ob wir einen Home Assistant Intent vermuten können
                    message_lower = message.lower()
                    ha_action_keywords = ["schalte", "mach", "stelle", "ist", "zeige", "öffne", "schließe"]

                    has_action = any(keyword in message_lower for keyword in ha_action_keywords)
                    has_device = any(keyword in message_lower for keyword in ["licht", "lampe", "fenster", "tür", "heizung", "rolladen"])

                    if has_action and has_device:
                        logger.warning("⚠️  Vermute Home Assistant Intent - verwende Fallback mit Entity-Suche")
                        # Versuche zumindest die Entity zu finden
                        from integrations.homeassistant import HomeAssistantClient
                        ha_client = HomeAssistantClient()
                        search_results = await ha_client.search_entities(message)

                        if search_results:
                            # Nehme erste gefundene Entity
                            entity_id = search_results[0]["entity_id"]

                            # Bestimme Intent basierend auf Aktion
                            if any(word in message_lower for word in ["ein", "an", "schalte ein"]):
                                intent = "homeassistant.turn_on"
                            elif any(word in message_lower for word in ["aus", "schalte aus"]):
                                intent = "homeassistant.turn_off"
                            elif any(word in message_lower for word in ["ist", "status", "zustand"]):
                                intent = "homeassistant.get_state"
                            else:
                                intent = "homeassistant.turn_on"  # Default

                            logger.info(f"✅ Fallback Intent: {intent} mit Entity: {entity_id}")
                            return {
                                "intent": intent,
                                "parameters": {"entity_id": entity_id},
                                "confidence": 0.6  # Niedrigere Confidence für Fallback
                            }

                    # Fallback: unresolved intent (agent loop can pick this up)
                    return {
                        "intent": "general.unresolved",
                        "parameters": {},
                        "confidence": 0.0
                    }

            # Validierung: Wenn es keine HA-relevante Frage ist, erzwinge general.conversation
            if intent_data.get("intent", "").startswith("homeassistant."):
                # Lade dynamische Keywords von Home Assistant
                ha_keywords = await self._get_ha_keywords()

                message_lower = message.lower()
                has_ha_keyword = any(keyword in message_lower for keyword in ha_keywords)

                if not has_ha_keyword:
                    # Keine HA-Keywords gefunden, überschreibe Intent
                    logger.info(f"⚠️  Intent überschrieben: Keine HA-Keywords in '{message[:50]}' gefunden")
                    intent_data = {
                        "intent": "general.conversation",
                        "parameters": {},
                        "confidence": 1.0
                    }

            logger.info(f"🎯 Intent: {intent_data.get('intent')} | Entity: {intent_data.get('parameters', {}).get('entity_id', 'none')}")

            return intent_data

        except Exception as e:
            logger.error(f"❌ Intent Extraction Fehler: {e}")
            import traceback
            logger.debug(f"Traceback: {traceback.format_exc()}")
            logger.debug(f"Response war: {response if 'response' in locals() else 'keine response'}")
            return {
                "intent": "general.conversation",
                "parameters": {},
                "confidence": 1.0
            }

    async def extract_ranked_intents(
        self,
        message: str,
        plugin_registry=None,
        room_context: dict | None = None,
        conversation_history: list[dict] | None = None,
        lang: str | None = None
    ) -> list[dict]:
        """
        Extract ranked list of intents from message (highest confidence first).

        Calls extract_intent() internally and handles both old single-intent format
        and new ranked format with {"intents": [...]}.

        Args:
            message: User message
            plugin_registry: Optional plugin registry
            room_context: Optional room context
            conversation_history: Optional conversation history
            lang: Language (de/en)

        Returns:
            List of intent dicts sorted by confidence (descending).
            Each dict has: intent, parameters, confidence
        """
        raw = await self.extract_intent(
            message,
            plugin_registry=plugin_registry,
            room_context=room_context,
            conversation_history=conversation_history,
            lang=lang
        )

        # Check if extract_intent() already parsed ranked intents
        if "_ranked_intents" in raw:
            intents = raw["_ranked_intents"]
            # Ensure each intent has required fields
            for intent in intents:
                intent.setdefault("confidence", 0.5)
                intent.setdefault("parameters", {})
                # Remove internal marker
                intent.pop("_ranked_intents", None)
            intents.sort(key=lambda x: x.get("confidence", 0), reverse=True)
            ranked_summary = ", ".join(f"{i.get('intent')}({i.get('confidence', 0):.2f})" for i in intents)
            logger.info(f"🎯 Ranked intents: [{ranked_summary}]")
            return intents

        # Old format: single intent dict
        return [raw]

    @staticmethod
    def _parse_intent_json(raw_response: str) -> dict | None:
        """Parse intent JSON from LLM response, handling markdown and truncation."""
        import json
        import re

        if not raw_response or not raw_response.strip():
            return None

        response = raw_response.strip()

        # Strip markdown code blocks
        if "```" in response:
            match = re.search(r'```(?:json)?\s*(\{.*\})\s*```', response, re.DOTALL)
            if match:
                response = match.group(1)
            else:
                parts = response.split("```")
                if len(parts) >= 2:
                    response = parts[1].strip()
                    if response.startswith("json"):
                        response = response[4:].strip()

        # Balanced braces extraction
        first_brace = response.find('{')
        if first_brace >= 0:
            depth = 0
            in_string = False
            escape_next = False
            end_pos = -1
            for i in range(first_brace, len(response)):
                c = response[i]
                if escape_next:
                    escape_next = False
                    continue
                if c == '\\' and in_string:
                    escape_next = True
                    continue
                if c == '"' and not escape_next:
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if c == '{':
                    depth += 1
                elif c == '}':
                    depth -= 1
                    if depth == 0:
                        end_pos = i
                        break
            if end_pos > 0:
                response = response[first_brace:end_pos + 1]

        try:
            raw_data = json.loads(response)
            if "intents" in raw_data and isinstance(raw_data["intents"], list) and raw_data["intents"]:
                intents_list = raw_data["intents"]
                intents_list.sort(key=lambda x: x.get("confidence", 0), reverse=True)
                intent_data = dict(intents_list[0])
                intent_data.setdefault("parameters", {})
                intent_data["_ranked_intents"] = [dict(i) for i in intents_list]
                return intent_data
            return raw_data
        except json.JSONDecodeError:
            return None

    async def _build_entity_context(
        self,
        message: str,
        room_context: dict | None = None
    ) -> str:
        """
        Erstelle Entity-Kontext für Intent Recognition

        Filtert Entities basierend auf der Nachricht und gibt dem LLM
        eine Liste relevanter Entities zur Auswahl.

        Args:
            message: Die Benutzernachricht
            room_context: Optional dict mit room_name, room_id etc.
        """
        try:
            from integrations.homeassistant import HomeAssistantClient
            ha_client = HomeAssistantClient()

            # Lade alle Entities
            entity_map = await ha_client.get_entity_map()

            if not entity_map:
                return "VERFÜGBARE ENTITIES: (Keine - Home Assistant nicht erreichbar)"

            message_lower = message.lower()

            # Extrahiere aktuellen Raum aus Context
            current_room = None
            current_room_normalized = None
            if room_context:
                room_name = room_context.get("room_name")
                if room_name:
                    current_room = room_name.lower()
                    # Normalisiere Raumnamen (entferne Umlaute für Matching)
                    current_room_normalized = current_room.replace("ä", "a").replace("ö", "o").replace("ü", "u")

            # Pre-compute message words as a set for O(1) lookups
            message_words = {w for w in message_lower.split() if len(w) > 2}

            # Pre-compute device keyword matches: which domains are relevant for this message?
            device_keywords = {
                "fenster": ["binary_sensor", "sensor"],
                "tür": ["binary_sensor"],
                "licht": ["light"],
                "lampe": ["light"],
                "schalter": ["switch"],
                "heizung": ["climate"],
                "thermostat": ["climate"],
                "rolladen": ["cover"],
                "jalousie": ["cover"],
                "mediaplayer": ["media_player"],
                "player": ["media_player"],
                "fernseher": ["media_player"],
                "tv": ["media_player"],
                "musik": ["media_player"],
                "spotify": ["media_player"],
                "radio": ["media_player"]
            }
            # Build a set of domains that match keywords in the message (computed once)
            matched_domains = set()
            for keyword, domains in device_keywords.items():
                if keyword in message_lower:
                    matched_domains.update(domains)

            # Filtere relevante Entities basierend auf Message
            relevant_entities = []

            # Priorisierung: Raum und Device-Type erkennen
            for entity in entity_map:
                relevance_score = 0
                entity_room = (entity.get("room") or "").lower()

                # HÖCHSTE Priorität: Entity ist im aktuellen Raum des Benutzers
                if current_room and entity_room:
                    entity_room_normalized = entity_room.replace("ä", "a").replace("ö", "o").replace("ü", "u")
                    if current_room in entity_room or current_room_normalized in entity_room_normalized:
                        relevance_score += 20  # Starker Bonus für aktuellen Raum

                # Raum-Match aus Nachricht
                if entity_room:
                    if entity_room in message_lower:
                        relevance_score += 10

                # Friendly Name Match — set intersection instead of O(n*m) loop
                friendly_name_lower = (entity.get("friendly_name") or "").lower()
                friendly_words = set(friendly_name_lower.split())
                matches = message_words & friendly_words
                if matches:
                    relevance_score += 5 * len(matches)

                # Device-Type Match — O(1) set lookup instead of iterating all keywords
                if entity.get("domain") in matched_domains:
                    relevance_score += 8

                # Füge Entity hinzu wenn relevant
                if relevance_score > 0:
                    relevant_entities.append((relevance_score, entity))

            # Sortiere nach Relevanz und nimm Top 15
            relevant_entities.sort(key=lambda x: x[0], reverse=True)
            top_entities = [e[1] for e in relevant_entities[:25]]

            # Falls keine relevanten gefunden, zeige die häufigsten Typen
            if not top_entities:
                # Nehme die ersten 10 Entities jedes Typs
                seen_domains = set()
                for entity in entity_map:
                    domain = entity.get("domain")
                    if domain not in seen_domains or len(top_entities) < 25:
                        top_entities.append(entity)
                        seen_domains.add(domain)
                        if len(top_entities) >= 25:
                            break

            # Formatiere als kompakte Liste
            # Note: MCP HA tools use "name" (friendly_name) and "area" (room) as parameters,
            # NOT entity_id. Format the context so the LLM uses the correct parameter values.
            context_lines = ["VERFÜGBARE HOME ASSISTANT ENTITIES:"]
            context_lines.append("  [Für MCP HA-Tools: Nutze 'name' = friendly_name, 'area' = Raum-Name]")

            # Wenn aktueller Raum bekannt, zeige Entities in diesem Raum zuerst
            if current_room:
                context_lines.append(f"  [Entitäten im aktuellen Raum '{current_room}' haben Priorität]")

            for entity in top_entities:
                entity_room = (entity.get("room") or "").lower()
                is_current_room = current_room and entity_room and current_room in entity_room

                room_info = f", area: \"{entity['room']}\"" if entity.get('room') else ""
                state_info = f" [aktuell: {entity.get('state', 'unknown')}]"
                current_room_marker = " ★" if is_current_room else ""

                context_lines.append(
                    f"  - name: \"{entity['friendly_name']}\"{room_info}{state_info}{current_room_marker}"
                )

            return "\n".join(context_lines)

        except Exception as e:
            logger.error(f"❌ Fehler beim Erstellen des Entity-Kontexts: {e}")
            return "VERFÜGBARE ENTITIES: (Fehler beim Laden)"

    async def _find_correction_examples(self, message: str, lang: str) -> str:
        """
        Load correction examples from semantic feedback (if any exist).

        Runs as a parallel task alongside _build_entity_context().
        """
        try:
            from services.database import AsyncSessionLocal
            from services.intent_feedback_service import IntentFeedbackService
            async with AsyncSessionLocal() as feedback_db:
                feedback_service = IntentFeedbackService(feedback_db)
                similar_corrections = await feedback_service.find_similar_corrections(
                    message, feedback_type="intent"
                )
                if similar_corrections:
                    result = feedback_service.format_as_few_shot(similar_corrections, lang=lang)
                    logger.info(f"📝 {len(similar_corrections)} correction example(s) injected into intent prompt")
                    return result
        except Exception as e:
            logger.warning(f"⚠️ Intent correction lookup failed: {e}")
        return ""

    async def _get_ha_keywords(self) -> set:
        """Hole dynamische Keywords von Home Assistant"""
        try:
            from integrations.homeassistant import HomeAssistantClient
            ha_client = HomeAssistantClient()
            keywords = await ha_client.get_keywords()
            return keywords
        except Exception as e:
            logger.error(f"❌ Fehler beim Laden der HA-Keywords: {e}")
            # Fallback zu minimaler Keyword-Liste
            return {
                "licht", "lampe", "schalter", "thermostat", "heizung",
                "fenster", "tür", "rolladen", "ein", "aus", "an", "schalten"
            }

    # ========== Kontext-Management Methoden ==========
    # NOTE: These methods delegate to ConversationService for backwards compatibility.
    # New code should use ConversationService directly.

    async def load_conversation_context(
        self,
        session_id: str,
        db: AsyncSession,
        max_messages: int = 20
    ) -> list[dict]:
        """Lade Konversationskontext aus der Datenbank (delegiert an ConversationService)"""
        from services.conversation_service import ConversationService
        service = ConversationService(db)
        return await service.load_context(session_id, max_messages)

    async def save_message(
        self,
        session_id: str,
        role: str,
        content: str,
        db: AsyncSession,
        metadata: dict | None = None
    ) -> "Message":
        """Speichere eine einzelne Nachricht (delegiert an ConversationService)"""
        from services.conversation_service import ConversationService
        service = ConversationService(db)
        return await service.save_message(session_id, role, content, metadata)

    async def get_conversation_summary(
        self,
        session_id: str,
        db: AsyncSession
    ) -> dict | None:
        """Hole Zusammenfassung einer Konversation (delegiert an ConversationService)"""
        from services.conversation_service import ConversationService
        service = ConversationService(db)
        return await service.get_summary(session_id)

    async def delete_conversation(
        self,
        session_id: str,
        db: AsyncSession
    ) -> bool:
        """Lösche eine komplette Konversation (delegiert an ConversationService)"""
        from services.conversation_service import ConversationService
        service = ConversationService(db)
        return await service.delete(session_id)

    async def get_all_conversations(
        self,
        db: AsyncSession,
        limit: int = 50,
        offset: int = 0
    ) -> list[dict]:
        """Hole Liste aller Konversationen (delegiert an ConversationService)"""
        from services.conversation_service import ConversationService
        service = ConversationService(db)
        return await service.list_all(limit, offset)

    async def search_conversations(
        self,
        query: str,
        db: AsyncSession,
        limit: int = 20
    ) -> list[dict]:
        """Suche in Konversationen nach Text (delegiert an ConversationService)"""
        from services.conversation_service import ConversationService
        service = ConversationService(db)
        return await service.search(query, limit)

    def _build_plugin_context(self, plugin_registry: Optional["PluginRegistry"]) -> str:
        """
        Build plugin context for LLM prompt

        Generates a section listing all available plugin intents
        """
        if not plugin_registry:
            return ""

        intents = plugin_registry.get_all_intents()

        if not intents:
            return ""

        lines = ["PLUGIN INTENTS:"]

        for intent_def in intents:
            # Format parameters
            params_str = ", ".join([
                f"{p.name}{'*' if p.required else ''}"
                for p in intent_def.parameters
            ])

            lines.append(
                f"- {intent_def.name}: {intent_def.description} "
                f"(params: {params_str})" if params_str else f"- {intent_def.name}: {intent_def.description}"
            )

            # Add examples if available
            if intent_def.examples:
                for example in intent_def.examples[:2]:  # Max 2 examples per intent
                    lines.append(f"  Example: \"{example}\"")

        return "\n".join(lines)

    # ==========================================================================
    # RAG (Retrieval-Augmented Generation) Methods
    # ==========================================================================

    async def get_embedding(self, text: str) -> list[float]:
        """
        Generiert Embedding für Text mit dem konfigurierten Embed-Modell.

        Args:
            text: Text für Embedding

        Returns:
            Liste von Floats (768 Dimensionen für nomic-embed-text)
        """
        try:
            response = await self.client.embeddings(
                model=self.embed_model,
                prompt=text
            )
            # ollama>=0.4.0 uses Pydantic models
            return response.embedding
        except Exception as e:
            logger.error(f"Embedding Fehler: {e}")
            raise

    async def chat_with_rag(
        self,
        message: str,
        rag_context: str | None = None,
        history: list[dict] | None = None,
        lang: str | None = None
    ) -> str:
        """
        Chat mit optionalem RAG-Kontext (nicht-streamend).

        Nutzt das größere RAG-Modell wenn Kontext vorhanden.

        Args:
            message: User-Nachricht
            rag_context: Optional formatierter Kontext aus der Wissensdatenbank
            history: Optional Chat-Historie
            lang: Sprache für die Antwort (de/en). None = default_lang

        Returns:
            Generierte Antwort
        """
        lang = lang or self.default_lang
        try:
            # Wähle Modell basierend auf RAG-Kontext
            model = self.rag_model if rag_context else self.chat_model

            # Baue System-Prompt mit RAG-Kontext
            system_prompt = self._build_rag_system_prompt(rag_context, lang=lang)

            messages = [{"role": "system", "content": system_prompt}]

            if history:
                messages.extend(history)

            messages.append({"role": "user", "content": message})

            response = await self.client.chat(
                model=model,
                messages=messages,
                options={"num_ctx": settings.ollama_num_ctx}
            )
            # ollama>=0.4.0 uses Pydantic models
            return response.message.content

        except Exception as e:
            logger.error(f"RAG Chat Fehler: {e}")
            return prompt_manager.get("chat", "error_fallback", lang=lang, default=f"Sorry, there was an error: {e!s}", error=str(e))

    async def chat_stream_with_rag(
        self,
        message: str,
        rag_context: str | None = None,
        history: list[dict] | None = None,
        lang: str | None = None,
        memory_context: str | None = None,
    ) -> AsyncGenerator[str, None]:
        """
        Streaming Chat mit optionalem RAG-Kontext.

        Nutzt das größere RAG-Modell wenn Kontext vorhanden.

        Args:
            message: User-Nachricht
            rag_context: Optional formatierter Kontext aus der Wissensdatenbank
            history: Optional Chat-Historie
            lang: Sprache für die Antwort (de/en). None = default_lang
            memory_context: Optional formatted memory section for the system prompt

        Yields:
            Text-Chunks der Antwort
        """
        lang = lang or self.default_lang
        try:
            # Wähle Modell basierend auf RAG-Kontext
            model = self.rag_model if rag_context else self.chat_model

            # Baue System-Prompt mit RAG-Kontext
            system_prompt = self._build_rag_system_prompt(rag_context, lang=lang, memory_context=memory_context)

            messages = [{"role": "system", "content": system_prompt}]

            if history:
                messages.extend(history)

            messages.append({"role": "user", "content": message})

            logger.debug(f"RAG Stream: model={model}, context_len={len(rag_context) if rag_context else 0}")

            async for chunk in await self.client.chat(
                model=model,
                messages=messages,
                stream=True,
                options={"num_ctx": settings.ollama_num_ctx}
            ):
                # ollama>=0.4.0 uses Pydantic models
                if chunk.message and chunk.message.content:
                    yield chunk.message.content

        except Exception as e:
            logger.error(f"RAG Streaming Fehler: {e}")
            yield prompt_manager.get("chat", "error_fallback", lang=lang, default=f"Error: {e!s}", error=str(e))

    def _build_rag_system_prompt(self, context: str | None = None, lang: str | None = None, memory_context: str | None = None) -> str:
        """
        Erstellt System-Prompt mit optionalem RAG-Kontext.

        Args:
            context: Formatierter Kontext aus der Wissensdatenbank
            lang: Sprache für den Prompt (de/en). None = default_lang
            memory_context: Optional formatted memory section

        Returns:
            System-Prompt für das LLM
        """
        lang = lang or self.default_lang

        # Base RAG system prompt from externalized prompts
        base_prompt = prompt_manager.get("chat", "rag_system_prompt", lang=lang)

        # Append memory context if available
        if memory_context:
            base_prompt += f"\n\n{memory_context}"

        if not context:
            return base_prompt

        # Build context section based on language
        if lang == "en":
            context_section = f"""
KNOWLEDGE BASE CONTEXT:
{context}

IMPORTANT:
- Base your answer on the context
- Do not invent information
- If you are unsure, say so
- Reference the source when quoting from it"""
        else:
            context_section = f"""
KONTEXT AUS WISSENSDATENBANK:
{context}

WICHTIG:
- Basiere deine Antwort auf dem Kontext
- Erfinde keine Informationen
- Wenn du unsicher bist, sage es
- Verweise auf die Quelle wenn du daraus zitierst"""

        return f"{base_prompt}\n{context_section}"

    async def ensure_rag_models_loaded(self) -> dict[str, bool]:
        """
        Stellt sicher, dass alle für RAG benötigten Modelle geladen sind.

        Returns:
            Dict mit Modell-Namen und ob sie verfügbar sind
        """
        result = {}
        models_to_check = [
            self.embed_model,
            self.rag_model,
        ]

        try:
            available = await self.client.list()
            # ollama>=0.4.0 uses Pydantic models with .model attribute
            available_names = [m.model for m in available.models]

            for model in models_to_check:
                if model in available_names:
                    result[model] = True
                    logger.info(f"Modell {model} verfuegbar")
                else:
                    result[model] = False
                    logger.warning(f"Modell {model} nicht gefunden")

        except Exception as e:
            logger.error(f"❌ Fehler beim Prüfen der Modelle: {e}")
            for model in models_to_check:
                result[model] = False

        return result
