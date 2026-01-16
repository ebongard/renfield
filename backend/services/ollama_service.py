"""
Ollama Service - Lokales LLM
"""
import ollama
from typing import AsyncGenerator, List, Dict, Optional
from loguru import logger
from utils.config import settings
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from models.database import Conversation, Message
from datetime import datetime

class OllamaService:
    """Service für Ollama LLM Interaktion"""
    
    def __init__(self):
        self.client = ollama.AsyncClient(host=settings.ollama_url)
        self.model = settings.ollama_model
        self.system_prompt = """Du bist Renfield, ein hilfreicher KI-Assistent für Smart Home Steuerung.

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
4. Wenn eine Aktion ausgeführt wurde, bestätige dies einfach

GUTE Beispiele:
User: "Ist das Licht an?"
Du: "Das Licht ist eingeschaltet."

User: "Schalte das Licht aus"
Du: "Ich habe das Licht ausgeschaltet."

SCHLECHTE Beispiele (NICHT SO):
Du: '{"intent": "homeassistant.turn_on", "entity_id": "light.x"}'
Du: 'Hier sind die Ergebnisse: {...}'
Du: 'System: Aktion ausgeführt'
"""
    
    async def ensure_model_loaded(self):
        """Stelle sicher, dass das Modell geladen ist"""
        try:
            models = await self.client.list()
            model_names = [m['name'] for m in models['models']]
            
            if self.model not in model_names:
                logger.info(f"📥 Lade Modell {self.model}...")
                await self.client.pull(self.model)
                logger.info(f"✅ Modell {self.model} geladen")
            else:
                logger.info(f"✅ Modell {self.model} bereits vorhanden")
        except Exception as e:
            logger.error(f"❌ Fehler beim Laden des Modells: {e}")
            raise
    
    async def chat(self, message: str, context: List[Dict] = None) -> str:
        """Einfacher Chat (nicht-streamend)"""
        try:
            messages = [{"role": "system", "content": self.system_prompt}]
            
            if context:
                messages.extend(context)
            
            messages.append({"role": "user", "content": message})
            
            response = await self.client.chat(
                model=self.model,
                messages=messages
            )
            
            return response['message']['content']
        except Exception as e:
            logger.error(f"❌ Chat Fehler: {e}")
            return f"Entschuldigung, es gab einen Fehler: {str(e)}"
    
    async def chat_stream(self, message: str, context: List[Dict] = None) -> AsyncGenerator[str, None]:
        """Streaming Chat"""
        try:
            messages = [{"role": "system", "content": self.system_prompt}]
            
            if context:
                messages.extend(context)
            
            messages.append({"role": "user", "content": message})
            
            async for chunk in await self.client.chat(
                model=self.model,
                messages=messages,
                stream=True
            ):
                if 'message' in chunk and 'content' in chunk['message']:
                    yield chunk['message']['content']
        except Exception as e:
            logger.error(f"❌ Streaming Fehler: {e}")
            yield f"Fehler: {str(e)}"
    
    async def extract_intent(self, message: str, plugin_registry=None) -> Dict:
        """Extrahiere Intent und Parameter aus Nachricht mit Plugin-Unterstützung"""

        # Lade echte Entity Map von Home Assistant
        entity_context = await self._build_entity_context(message)

        # Build plugin context (NEW)
        plugin_context = ""
        if plugin_registry:
            plugin_context = self._build_plugin_context(plugin_registry)

        prompt = f"""Erkenne den Intent für diese Nachricht: "{message}"

INTENT-TYPEN:

CORE INTENTS:
- homeassistant.turn_on: Gerät einschalten
- homeassistant.turn_off: Gerät ausschalten
- homeassistant.get_state: Status abfragen
- general.conversation: Normale Konversation (kein Smart Home)

{plugin_context}

{entity_context}

BEISPIELE:
1. "Schalte Licht ein" → {{"intent":"homeassistant.turn_on","parameters":{{"entity_id":"light.xxx"}}}}
2. "Ist Fenster offen?" → {{"intent":"homeassistant.get_state","parameters":{{"entity_id":"binary_sensor.xxx"}}}}
3. "Wie geht es dir?" → {{"intent":"general.conversation","parameters":{{}}}}

WICHTIG: Verwende NUR Intents aus der Liste oben! Antworte NUR mit JSON, kein Text!"""
        
        try:
            # Nutze direkt den Client mit temperature=0 für deterministische Antworten
            messages = [
                {
                    "role": "system",
                    "content": "Antworte nur mit JSON."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ]

            response_data = await self.client.chat(
                model=self.model,
                messages=messages,
                options={
                    "temperature": 0.0,  # Deterministisch
                    "top_p": 0.1,        # Sehr fokussiert
                    "num_predict": 300   # Etwas mehr Platz für JSON
                }
            )

            response = response_data['message']['content']

            # Robuste JSON-Extraktion
            import json
            import re

            logger.debug(f"Raw response: {response[:200]}")
            
            # Entferne Markdown-Code-Blocks
            response = response.strip()
            
            # Methode 1: Markdown Code-Block
            if "```" in response:
                match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', response, re.DOTALL)
                if match:
                    response = match.group(1)
                else:
                    # Fallback: Nimm alles zwischen ersten ```
                    parts = response.split("```")
                    if len(parts) >= 2:
                        response = parts[1].strip()
                        if response.startswith("json"):
                            response = response[4:].strip()
            
            # Methode 2: Extrahiere erstes JSON-Objekt (robusteste Methode)
            json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', response, re.DOTALL)
            if json_match:
                response = json_match.group(0)
            
            # Entferne alles nach dem schließenden }
            if '}' in response:
                response = response[:response.rfind('}')+1]
            
            # Parse JSON
            try:
                intent_data = json.loads(response)
            except json.JSONDecodeError as e:
                logger.error(f"❌ JSON Parse Error: {e}")
                logger.error(f"Attempted to parse: {response[:200]}")
                logger.warning("⚠️  LLM hat mit Text statt JSON geantwortet")

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

                # Fallback zu general.conversation
                return {
                    "intent": "general.conversation",
                    "parameters": {},
                    "confidence": 1.0
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
    
    async def _build_entity_context(self, message: str) -> str:
        """
        Erstelle Entity-Kontext für Intent Recognition

        Filtert Entities basierend auf der Nachricht und gibt dem LLM
        eine Liste relevanter Entities zur Auswahl.
        """
        try:
            from integrations.homeassistant import HomeAssistantClient
            ha_client = HomeAssistantClient()

            # Lade alle Entities
            entity_map = await ha_client.get_entity_map()

            if not entity_map:
                return "VERFÜGBARE ENTITIES: (Keine - Home Assistant nicht erreichbar)"

            message_lower = message.lower()

            # Filtere relevante Entities basierend auf Message
            relevant_entities = []

            # Priorisierung: Raum und Device-Type erkennen
            for entity in entity_map:
                relevance_score = 0

                # Raum-Match
                if entity.get("room"):
                    if entity["room"] in message_lower:
                        relevance_score += 10

                # Friendly Name Match
                friendly_name_lower = entity.get("friendly_name", "").lower()
                for word in message_lower.split():
                    if len(word) > 2 and word in friendly_name_lower:
                        relevance_score += 5

                # Device-Type Match (fenster, licht, etc.)
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

                for keyword, domains in device_keywords.items():
                    if keyword in message_lower and entity.get("domain") in domains:
                        relevance_score += 8

                # Füge Entity hinzu wenn relevant
                if relevance_score > 0:
                    relevant_entities.append((relevance_score, entity))

            # Sortiere nach Relevanz und nimm Top 15
            relevant_entities.sort(key=lambda x: x[0], reverse=True)
            top_entities = [e[1] for e in relevant_entities[:15]]

            # Falls keine relevanten gefunden, zeige die häufigsten Typen
            if not top_entities:
                # Nehme die ersten 10 Entities jedes Typs
                seen_domains = set()
                for entity in entity_map:
                    domain = entity.get("domain")
                    if domain not in seen_domains or len(top_entities) < 15:
                        top_entities.append(entity)
                        seen_domains.add(domain)
                        if len(top_entities) >= 15:
                            break

            # Formatiere als kompakte Liste
            context_lines = ["VERFÜGBARE HOME ASSISTANT ENTITIES:"]
            for entity in top_entities:
                room_suffix = f" ({entity['room']})" if entity.get('room') else ""
                state_info = f" [aktuell: {entity.get('state', 'unknown')}]"
                context_lines.append(
                    f"  - {entity['entity_id']}: {entity['friendly_name']}{room_suffix}{state_info}"
                )

            return "\n".join(context_lines)

        except Exception as e:
            logger.error(f"❌ Fehler beim Erstellen des Entity-Kontexts: {e}")
            return "VERFÜGBARE ENTITIES: (Fehler beim Laden)"

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

    async def load_conversation_context(
        self,
        session_id: str,
        db: AsyncSession,
        max_messages: int = 20
    ) -> List[Dict]:
        """Lade Konversationskontext aus der Datenbank"""
        try:
            # Finde Conversation
            result = await db.execute(
                select(Conversation).where(Conversation.session_id == session_id)
            )
            conversation = result.scalar_one_or_none()

            if not conversation:
                logger.debug(f"Keine Konversation gefunden für session_id: {session_id}")
                return []

            # Lade letzte N Nachrichten
            result = await db.execute(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.timestamp.desc())
                .limit(max_messages)
            )
            messages = result.scalars().all()

            # Konvertiere zu Chat-Format (älteste zuerst)
            context = [
                {"role": msg.role, "content": msg.content}
                for msg in reversed(messages)
            ]

            logger.info(f"📚 Geladen: {len(context)} Nachrichten für Session {session_id}")
            return context

        except Exception as e:
            logger.error(f"❌ Fehler beim Laden des Kontexts: {e}")
            return []

    async def save_message(
        self,
        session_id: str,
        role: str,
        content: str,
        db: AsyncSession,
        metadata: Optional[Dict] = None
    ) -> Message:
        """Speichere eine einzelne Nachricht"""
        try:
            # Finde oder erstelle Conversation
            result = await db.execute(
                select(Conversation).where(Conversation.session_id == session_id)
            )
            conversation = result.scalar_one_or_none()

            if not conversation:
                conversation = Conversation(session_id=session_id)
                db.add(conversation)
                await db.flush()

            # Erstelle Message
            message = Message(
                conversation_id=conversation.id,
                role=role,
                content=content,
                message_metadata=metadata
            )
            db.add(message)

            # Update conversation timestamp
            conversation.updated_at = datetime.utcnow()

            await db.commit()
            await db.refresh(message)

            logger.debug(f"💾 Nachricht gespeichert: {role} - {content[:50]}...")
            return message

        except Exception as e:
            logger.error(f"❌ Fehler beim Speichern der Nachricht: {e}")
            await db.rollback()
            raise

    async def get_conversation_summary(
        self,
        session_id: str,
        db: AsyncSession
    ) -> Optional[Dict]:
        """Hole Zusammenfassung einer Konversation"""
        try:
            result = await db.execute(
                select(Conversation).where(Conversation.session_id == session_id)
            )
            conversation = result.scalar_one_or_none()

            if not conversation:
                return None

            # Zähle Nachrichten
            result = await db.execute(
                select(func.count(Message.id))
                .where(Message.conversation_id == conversation.id)
            )
            message_count = result.scalar()

            # Hole erste und letzte Nachricht
            result = await db.execute(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.timestamp.asc())
                .limit(1)
            )
            first_message = result.scalar_one_or_none()

            result = await db.execute(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.timestamp.desc())
                .limit(1)
            )
            last_message = result.scalar_one_or_none()

            return {
                "session_id": session_id,
                "created_at": conversation.created_at.isoformat(),
                "updated_at": conversation.updated_at.isoformat(),
                "message_count": message_count,
                "first_message": first_message.content[:100] if first_message else None,
                "last_message": last_message.content[:100] if last_message else None
            }

        except Exception as e:
            logger.error(f"❌ Fehler beim Laden der Zusammenfassung: {e}")
            return None

    async def delete_conversation(
        self,
        session_id: str,
        db: AsyncSession
    ) -> bool:
        """Lösche eine komplette Konversation"""
        try:
            result = await db.execute(
                select(Conversation).where(Conversation.session_id == session_id)
            )
            conversation = result.scalar_one_or_none()

            if conversation:
                await db.delete(conversation)
                await db.commit()
                logger.info(f"🗑️  Konversation gelöscht: {session_id}")
                return True

            return False

        except Exception as e:
            logger.error(f"❌ Fehler beim Löschen der Konversation: {e}")
            await db.rollback()
            return False

    async def get_all_conversations(
        self,
        db: AsyncSession,
        limit: int = 50,
        offset: int = 0
    ) -> List[Dict]:
        """Hole Liste aller Konversationen"""
        try:
            result = await db.execute(
                select(Conversation)
                .order_by(Conversation.updated_at.desc())
                .limit(limit)
                .offset(offset)
            )
            conversations = result.scalars().all()

            summaries = []
            for conv in conversations:
                # Zähle Nachrichten
                result = await db.execute(
                    select(func.count(Message.id))
                    .where(Message.conversation_id == conv.id)
                )
                message_count = result.scalar()

                # Hole erste User-Nachricht als Vorschau
                result = await db.execute(
                    select(Message)
                    .where(Message.conversation_id == conv.id, Message.role == "user")
                    .order_by(Message.timestamp.asc())
                    .limit(1)
                )
                first_user_msg = result.scalar_one_or_none()

                summaries.append({
                    "session_id": conv.session_id,
                    "created_at": conv.created_at.isoformat(),
                    "updated_at": conv.updated_at.isoformat(),
                    "message_count": message_count,
                    "preview": first_user_msg.content[:100] if first_user_msg else "Leere Konversation"
                })

            logger.info(f"📋 Geladen: {len(summaries)} Konversationen")
            return summaries

        except Exception as e:
            logger.error(f"❌ Fehler beim Laden der Konversationen: {e}")
            return []

    async def search_conversations(
        self,
        query: str,
        db: AsyncSession,
        limit: int = 20
    ) -> List[Dict]:
        """Suche in Konversationen nach Text"""
        try:
            # Suche in Message-Content
            result = await db.execute(
                select(Message)
                .where(Message.content.ilike(f"%{query}%"))
                .order_by(Message.timestamp.desc())
                .limit(limit)
            )
            messages = result.scalars().all()

            # Gruppiere nach Conversation
            conversation_ids = list(set(msg.conversation_id for msg in messages))

            results = []
            for conv_id in conversation_ids[:limit]:
                result = await db.execute(
                    select(Conversation).where(Conversation.id == conv_id)
                )
                conv = result.scalar_one_or_none()

                if conv:
                    # Finde matching messages
                    matching_msgs = [
                        {
                            "role": msg.role,
                            "content": msg.content,
                            "timestamp": msg.timestamp.isoformat()
                        }
                        for msg in messages if msg.conversation_id == conv_id
                    ]

                    results.append({
                        "session_id": conv.session_id,
                        "created_at": conv.created_at.isoformat(),
                        "updated_at": conv.updated_at.isoformat(),
                        "matching_messages": matching_msgs
                    })

            logger.info(f"🔍 Gefunden: {len(results)} Konversationen mit '{query}'")
            return results

        except Exception as e:
            logger.error(f"❌ Fehler bei der Suche: {e}")
            return []

    def _build_plugin_context(self, plugin_registry) -> str:
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
