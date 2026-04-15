"""
Ollama Service - Lokales LLM

Provides LLM interaction with multilingual support (de/en).
Language can be specified per-call or defaults to system setting.
"""
import asyncio
import json
import re
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
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
    get_embed_client,
)

from services.input_guard import sanitize_user_input as _sanitize_user_input


_VALID_PERSONALITY_STYLES = {"freundlich", "direkt", "formell", "casual"}


def _build_personality_context(personality_style: str, personality_prompt: str | None, lang: str) -> str:
    """Build the personality context section for injection into system/agent prompts."""
    if personality_style not in _VALID_PERSONALITY_STYLES:
        return ""

    # Access the raw YAML dict directly (prompt_manager.get returns str for dicts)
    chat_data = prompt_manager._cache.get("chat", {})
    lang_data = chat_data.get(lang, {})
    styles_dict = lang_data.get("personality_styles", {})

    instructions = styles_dict.get(personality_style, "") if isinstance(styles_dict, dict) else ""

    if personality_prompt:
        instructions += f"\n{personality_prompt}"

    if not instructions:
        return ""

    return prompt_manager.get(
        "chat", "personality_context", lang=lang,
        personality_instructions=instructions,
    )


class OllamaService:
    """Service für Ollama LLM Interaktion mit Mehrsprachigkeit."""

    def __init__(self):
        self.client = get_default_client()
        self.embed_client = get_embed_client()

        # Multi-Modell Konfiguration
        self.model = settings.ollama_model  # Legacy
        self.chat_model = settings.ollama_chat_model
        self.rag_model = settings.ollama_rag_model
        self.embed_model = settings.ollama_embed_model
        self.intent_model = settings.ollama_intent_model

        # Default language from settings
        self.default_lang = settings.default_language

    def get_system_prompt(
        self,
        lang: str | None = None,
        memory_context: str | None = None,
        personality_style: str | None = None,
        personality_prompt: str | None = None,
    ) -> str:
        """Get system prompt for the specified language, optionally with memory and personality context."""
        lang = lang or self.default_lang
        base = prompt_manager.get("chat", "system_prompt", lang=lang, default=self._default_system_prompt(lang))

        # Personality injection
        if personality_style:
            personality_section = _build_personality_context(personality_style, personality_prompt, lang)
            if personality_section:
                base += f"\n\n{personality_section}"

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
        if not await llm_circuit_breaker.allow_request():
            logger.warning("🔴 LLM circuit breaker OPEN — rejecting chat request")
            return prompt_manager.get("chat", "error_fallback", lang=lang, default="LLM-Service vorübergehend nicht verfügbar.", error="Circuit Breaker aktiv")

        try:
            message = _sanitize_user_input(message)
            system_prompt = self.get_system_prompt(lang, memory_context=memory_context)
            messages = [{"role": "system", "content": system_prompt}]

            if history:
                messages.extend(history)

            messages.append({"role": "user", "content": message})

            classification_kwargs = get_classification_chat_kwargs(self.chat_model)
            response = await self.client.chat(
                model=self.chat_model,
                messages=messages,
                options={"num_ctx": settings.ollama_num_ctx},
                **classification_kwargs,
            )
            await llm_circuit_breaker.record_success()
            return extract_response_content(response) or ""
        except Exception as e:
            await llm_circuit_breaker.record_failure()
            logger.error(f"Chat Fehler: {e}")
            return prompt_manager.get("chat", "error_fallback", lang=lang, default=f"Entschuldigung, es gab einen Fehler: {e!s}", error=str(e))

    async def chat_stream(
        self,
        message: str,
        history: list[dict] = None,
        lang: str | None = None,
        memory_context: str | None = None,
        document_context: str | None = None,
        personality_style: str | None = None,
        personality_prompt: str | None = None,
    ) -> AsyncGenerator[str, None]:
        """
        Streaming Chat with optional conversation history.

        Args:
            message: Die Benutzernachricht
            history: Optionale Konversationshistorie
            lang: Sprache für die Antwort (de/en). None = default_lang
            memory_context: Optional formatted memory section for the system prompt
            document_context: Optional formatted document section for the system prompt
            personality_style: Optional personality style (freundlich/direkt/formell/casual)
            personality_prompt: Optional free-text personality fine-tuning
        """
        lang = lang or self.default_lang

        # Check circuit breaker before LLM call
        if not await llm_circuit_breaker.allow_request():
            logger.warning("🔴 LLM circuit breaker OPEN — rejecting stream request")
            yield prompt_manager.get("chat", "error_fallback", lang=lang, default="LLM-Service vorübergehend nicht verfügbar.", error="Circuit Breaker aktiv")
            return

        try:
            message = _sanitize_user_input(message)
            system_prompt = self.get_system_prompt(lang, memory_context=memory_context, personality_style=personality_style, personality_prompt=personality_prompt)
            if document_context:
                system_prompt += f"\n\n{document_context}"
            messages = [{"role": "system", "content": system_prompt}]

            if history:
                messages.extend(history)

            messages.append({"role": "user", "content": message})

            classification_kwargs = get_classification_chat_kwargs(self.chat_model)
            async for chunk in await self.client.chat(
                model=self.chat_model,
                messages=messages,
                stream=True,
                options={"num_ctx": settings.ollama_num_ctx},
                **classification_kwargs,
            ):
                # ollama>=0.4.0 uses Pydantic models
                if chunk.message and chunk.message.content:
                    yield chunk.message.content

            # Record success after successful streaming
            await llm_circuit_breaker.record_success()
        except Exception as e:
            await llm_circuit_breaker.record_failure()
            logger.error(f"Streaming Fehler: {e}")
            yield prompt_manager.get("chat", "error_fallback", lang=lang, default=f"Fehler: {e!s}", error=str(e))

    async def chat_stream_with_image(
        self,
        message: str,
        image_b64: str,
        history: list[dict] | None = None,
        lang: str | None = None,
    ) -> AsyncGenerator[str, None]:
        """
        Streaming chat with a vision-capable model.

        Sends an image alongside the user message using Ollama's images parameter.

        Args:
            message: User message (transcribed speech)
            image_b64: Base64-encoded JPEG image
            history: Optional conversation history
            lang: Language for the response (de/en)
        """
        lang = lang or self.default_lang
        vision_model = settings.ollama_vision_model

        if not vision_model:
            logger.warning("chat_stream_with_image called but no vision model configured")
            async for chunk in self.chat_stream(message, history=history, lang=lang):
                yield chunk
            return

        if not await llm_circuit_breaker.allow_request():
            logger.warning("🔴 LLM circuit breaker OPEN — rejecting vision request")
            yield prompt_manager.get("chat", "error_fallback", lang=lang, default="LLM-Service vorübergehend nicht verfügbar.", error="Circuit Breaker aktiv")
            return

        try:
            message = _sanitize_user_input(message)
            system_prompt = self.get_system_prompt(lang)
            messages = [{"role": "system", "content": system_prompt}]

            if history:
                messages.extend(history)

            messages.append({
                "role": "user",
                "content": message,
                "images": [image_b64],
            })

            # Use dedicated vision URL if configured, otherwise default client
            if settings.ollama_vision_url:
                from utils.llm_client import _make_client_with_fallback
                vision_client = _make_client_with_fallback(settings.ollama_vision_url)
            else:
                vision_client = self.client

            classification_kwargs = get_classification_chat_kwargs(vision_model)
            async for chunk in await vision_client.chat(
                model=vision_model,
                messages=messages,
                stream=True,
                options={"num_ctx": settings.ollama_num_ctx},
                **classification_kwargs,
            ):
                if chunk.message and chunk.message.content:
                    yield chunk.message.content

            await llm_circuit_breaker.record_success()
        except Exception as e:
            await llm_circuit_breaker.record_failure()
            logger.error(f"Vision streaming error: {e}")
            yield prompt_manager.get("chat", "error_fallback", lang=lang, default=f"Fehler: {e!s}", error=str(e))

    async def extract_intent(
        self,
        message: str,
        room_context: dict | None = None,
        conversation_history: list[dict] | None = None,
        lang: str | None = None
    ) -> dict:
        """
        Extrahiere Intent und Parameter aus Nachricht.

        Args:
            message: Die Benutzernachricht
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
        message = _sanitize_user_input(message)

        # Build dynamic intent types from IntentRegistry
        from services.intent_registry import intent_registry

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
                    # JSON parsing of the LLM response failed even after retry.
                    # Fire the `intent_fallback_resolve` hook so domain-specific
                    # consumers (e.g. ha_glue's HA-keyword fallback) can still
                    # recognize the intent. First handler that returns a
                    # well-shaped non-None result wins. If no handler matches,
                    # fall through to general.unresolved and let the agent loop
                    # pick it up.
                    from utils.hooks import run_hooks
                    fallback_results = await run_hooks(
                        "intent_fallback_resolve",
                        message=message,
                        lang=lang,
                    )
                    for candidate in fallback_results:
                        if isinstance(candidate, dict) and "intent" in candidate:
                            logger.info(
                                f"✅ Intent fallback resolved by hook: "
                                f"{candidate.get('intent')!r}"
                            )
                            return candidate
                        logger.warning(
                            f"⚠️  intent_fallback_resolve handler returned "
                            f"unexpected shape (type={type(candidate).__name__}); "
                            f"ignoring and trying next handler"
                        )

                    # Fallback: unresolved intent (agent loop can pick this up)
                    return {
                        "intent": "general.unresolved",
                        "parameters": {},
                        "confidence": 0.0
                    }

            # Post-classification validation via hook. Domain-specific
            # handlers (e.g. ha_glue's HA keyword check) can override the
            # classification when they detect a false positive — for
            # instance an `homeassistant.*` intent on a message that
            # doesn't contain HA-shaped words. First well-shaped non-None
            # override wins; if no handler overrides, intent_data stays.
            from utils.hooks import run_hooks
            overrides = await run_hooks(
                "validate_classified_intent",
                intent_data=intent_data,
                message=message,
                lang=lang,
            )
            for override in overrides:
                if isinstance(override, dict) and "intent" in override:
                    intent_data = override
                    break
                logger.warning(
                    f"⚠️  validate_classified_intent handler returned unexpected "
                    f"shape (type={type(override).__name__}); ignoring"
                )

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
            room_context: Optional room context
            conversation_history: Optional conversation history
            lang: Language (de/en)

        Returns:
            List of intent dicts sorted by confidence (descending).
            Each dict has: intent, parameters, confidence
        """
        raw = await self.extract_intent(
            message,
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
        room_context: dict | None = None,
        lang: str = "de",
    ) -> str:
        """Build a domain-specific entity-context block for the intent prompt.

        Fires the `build_entity_context` hook so domain-specific consumers
        (e.g. ha_glue's HA entity filtering logic) can inject a prompt
        block listing relevant entities. First handler to return a
        well-shaped non-None string wins. If no handler registers or all
        return None, falls through to an empty string — the intent prompt
        is built without an entity list, same as a pro deploy or one
        without the HA integration wired up.

        Runs as a parallel task alongside `_find_correction_examples()`
        (see `asyncio.gather` in `extract_intent`), so keeping this
        method as a thin wrapper preserves the parallelism.
        """
        from utils.hooks import run_hooks
        results = await run_hooks(
            "build_entity_context",
            message=message,
            room_context=room_context,
            lang=lang,
        )
        for candidate in results:
            if isinstance(candidate, str):
                return candidate
            logger.warning(
                f"⚠️  build_entity_context handler returned unexpected shape "
                f"(type={type(candidate).__name__}); ignoring"
            )
        return ""

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
            response = await self.embed_client.embeddings(
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

            classification_kwargs = get_classification_chat_kwargs(model)
            response = await self.client.chat(
                model=model,
                messages=messages,
                options={"num_ctx": settings.ollama_num_ctx},
                **classification_kwargs,
            )
            return extract_response_content(response) or ""

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
        document_context: str | None = None,
        personality_style: str | None = None,
        personality_prompt: str | None = None,
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
            document_context: Optional formatted document section for the system prompt
            personality_style: Optional personality style (freundlich/direkt/formell/casual)
            personality_prompt: Optional free-text personality fine-tuning

        Yields:
            Text-Chunks der Antwort
        """
        lang = lang or self.default_lang
        try:
            # Wähle Modell basierend auf RAG-Kontext
            model = self.rag_model if rag_context else self.chat_model

            # Baue System-Prompt mit RAG-Kontext
            system_prompt = self._build_rag_system_prompt(rag_context, lang=lang, memory_context=memory_context, document_context=document_context, personality_style=personality_style, personality_prompt=personality_prompt)

            messages = [{"role": "system", "content": system_prompt}]

            if history:
                messages.extend(history)

            messages.append({"role": "user", "content": message})

            logger.debug(f"RAG Stream: model={model}, context_len={len(rag_context) if rag_context else 0}")

            classification_kwargs = get_classification_chat_kwargs(model)
            async for chunk in await self.client.chat(
                model=model,
                messages=messages,
                stream=True,
                options={"num_ctx": settings.ollama_num_ctx},
                **classification_kwargs,
            ):
                # ollama>=0.4.0 uses Pydantic models
                if chunk.message and chunk.message.content:
                    yield chunk.message.content

        except Exception as e:
            logger.error(f"RAG Streaming Fehler: {e}")
            yield prompt_manager.get("chat", "error_fallback", lang=lang, default=f"Error: {e!s}", error=str(e))

    def _build_rag_system_prompt(self, context: str | None = None, lang: str | None = None, memory_context: str | None = None, document_context: str | None = None, personality_style: str | None = None, personality_prompt: str | None = None) -> str:
        """
        Erstellt System-Prompt mit optionalem RAG-Kontext.

        Args:
            context: Formatierter Kontext aus der Wissensdatenbank
            lang: Sprache für den Prompt (de/en). None = default_lang
            memory_context: Optional formatted memory section
            document_context: Optional formatted document section
            personality_style: Optional personality style
            personality_prompt: Optional free-text personality fine-tuning

        Returns:
            System-Prompt für das LLM
        """
        lang = lang or self.default_lang

        # Base RAG system prompt from externalized prompts
        base_prompt = prompt_manager.get("chat", "rag_system_prompt", lang=lang)

        # Personality injection
        if personality_style:
            personality_section = _build_personality_context(personality_style, personality_prompt, lang)
            if personality_section:
                base_prompt += f"\n\n{personality_section}"

        # Append memory context if available
        if memory_context:
            base_prompt += f"\n\n{memory_context}"

        # Append document context if available
        if document_context:
            base_prompt += f"\n\n{document_context}"

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
