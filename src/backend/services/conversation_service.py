"""
Conversation Service - Manages conversation persistence

Extracted from OllamaService for better separation of concerns.
Handles all database operations for conversations and messages.
"""
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from models.database import Conversation, Message


class ConversationService:
    """
    Service für Konversations-Persistenz.

    Bietet:
    - Konversations-Kontext laden
    - Nachrichten speichern
    - Konversations-Management (Liste, Suche, Löschen)
    """

    def __init__(self, db: AsyncSession):
        """
        Initialisiert den Conversation Service.

        Args:
            db: AsyncSession für Datenbankoperationen
        """
        self.db = db

    async def load_context(
        self,
        session_id: str,
        max_messages: int = 20
    ) -> list[dict[str, str]]:
        """
        Lade Konversationskontext aus der Datenbank.

        Args:
            session_id: Session ID der Konversation
            max_messages: Maximale Anzahl zu ladender Nachrichten

        Returns:
            Liste von Nachrichten im Format [{"role": "user|assistant", "content": "..."}]
        """
        try:
            # Finde Conversation
            result = await self.db.execute(
                select(Conversation).where(Conversation.session_id == session_id)
            )
            conversation = result.scalar_one_or_none()

            if not conversation:
                logger.debug(f"Keine Konversation gefunden für session_id: {session_id}")
                return []

            # Lade letzte N Nachrichten
            result = await self.db.execute(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.timestamp.desc())
                .limit(max_messages)
            )
            messages = result.scalars().all()

            # Konvertiere zu Chat-Format (älteste zuerst)
            # Reconstruct action summary prefix for LLM context (kept out of DB content for clean UI)
            context = []
            for msg in reversed(messages):
                content = msg.content
                if (msg.role == "assistant"
                        and msg.message_metadata
                        and msg.message_metadata.get("action_summary")):
                    summary = msg.message_metadata["action_summary"]
                    content = (
                        f"[Aktionsergebnis — Verwende diese Daten für "
                        f"Folgeanfragen (IDs, Titel, etc.):\n{summary}]\n\n{content}"
                    )
                context.append({
                    "role": msg.role,
                    "content": content,
                    "metadata": msg.message_metadata,
                })

            logger.info(f"Geladen: {len(context)} Nachrichten für Session {session_id}")
            return context

        except Exception as e:
            logger.error(f"Fehler beim Laden des Kontexts: {e}")
            return []

    async def save_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: dict | None = None
    ) -> Message:
        """
        Speichere eine einzelne Nachricht.

        Args:
            session_id: Session ID der Konversation
            role: "user" oder "assistant"
            content: Nachrichteninhalt
            metadata: Optional zusätzliche Metadaten

        Returns:
            Gespeicherte Message
        """
        try:
            # Let plugins modify content/metadata before saving
            from utils.hooks import run_hooks
            hook_results = await run_hooks(
                "pre_save_message",
                role=role, content=content,
                metadata=metadata or {}, session_id=session_id,
            )
            if hook_results:
                result = hook_results[0]
                content = result.get("content", content)
                metadata = result.get("metadata", metadata)

            # Finde oder erstelle Conversation
            result = await self.db.execute(
                select(Conversation).where(Conversation.session_id == session_id)
            )
            conversation = result.scalar_one_or_none()

            if not conversation:
                conversation = Conversation(session_id=session_id)
                self.db.add(conversation)
                await self.db.flush()

            # Erstelle Message
            message = Message(
                conversation_id=conversation.id,
                role=role,
                content=content,
                message_metadata=metadata
            )
            self.db.add(message)

            # Update conversation timestamp
            conversation.updated_at = datetime.now(UTC).replace(tzinfo=None)

            await self.db.commit()
            await self.db.refresh(message)

            logger.debug(f"Nachricht gespeichert: {role} - {content[:50]}...")
            return message

        except Exception as e:
            logger.error(f"Fehler beim Speichern der Nachricht: {e}")
            await self.db.rollback()
            raise

    async def associate_speaker(
        self,
        session_id: str,
        speaker_id: int,
        user_id: int | None = None,
    ) -> None:
        """Associate a conversation with a speaker and optionally a user.

        speaker_id: Always set (from speaker recognition, works without auth).
        user_id: Set when Speaker→User mapping exists (requires auth + linked user).
        Idempotent: only sets fields that are currently NULL.
        """
        try:
            result = await self.db.execute(
                select(Conversation).where(Conversation.session_id == session_id)
            )
            conv = result.scalar_one_or_none()
            if not conv:
                return

            changed = False
            if conv.speaker_id is None:
                conv.speaker_id = speaker_id
                changed = True
            if user_id and conv.user_id is None:
                conv.user_id = user_id
                changed = True

            if changed:
                await self.db.commit()
                logger.debug(f"Conversation {session_id} associated: speaker={speaker_id}, user={user_id}")
        except Exception as e:
            logger.warning(f"Failed to associate speaker with conversation: {e}")
            await self.db.rollback()

    async def save_context_vars(
        self,
        session_id: str,
        vars_dict: dict,
    ) -> None:
        """Merge-update conversation context variables (pinned state).

        Existing keys are preserved; new keys are added; keys set to None are removed.
        """
        try:
            result = await self.db.execute(
                select(Conversation).where(Conversation.session_id == session_id)
            )
            conversation = result.scalar_one_or_none()
            if not conversation:
                return

            current = dict(conversation.context_vars or {})
            for k, v in vars_dict.items():
                if v is None:
                    current.pop(k, None)
                else:
                    current[k] = v
            conversation.context_vars = current
            await self.db.commit()
            logger.debug(f"Context vars updated for {session_id}: {list(current.keys())}")
        except Exception as e:
            logger.warning(f"Failed to save context vars: {e}")
            await self.db.rollback()

    async def load_context_vars(self, session_id: str) -> dict:
        """Load conversation context variables. Returns empty dict if none."""
        try:
            result = await self.db.execute(
                select(Conversation.context_vars).where(
                    Conversation.session_id == session_id
                )
            )
            row = result.scalar_one_or_none()
            return dict(row) if row else {}
        except Exception as e:
            logger.warning(f"Failed to load context vars: {e}")
            return {}

    async def update_summary(
        self,
        session_id: str,
        llm_client,
        model: str,
        threshold: int = 10,
        keep_recent: int = 4,
    ) -> str | None:
        """Generate an LLM summary of older messages when conversation grows.

        Triggers only when message count exceeds *threshold*. Summarizes all
        messages except the most recent *keep_recent* ones. Stores the result
        in ``conversations.summary``.

        Returns the summary text, or None if not triggered / failed.
        """
        try:
            result = await self.db.execute(
                select(Conversation).where(Conversation.session_id == session_id)
            )
            conversation = result.scalar_one_or_none()
            if not conversation:
                return None

            # Count messages
            result = await self.db.execute(
                select(func.count(Message.id)).where(
                    Message.conversation_id == conversation.id
                )
            )
            msg_count = result.scalar() or 0
            if msg_count < threshold:
                return conversation.summary  # return existing summary if any

            # Load oldest messages (all except keep_recent)
            result = await self.db.execute(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.timestamp.asc())
                .limit(msg_count - keep_recent)
            )
            old_messages = result.scalars().all()
            if not old_messages:
                return conversation.summary

            # Build text to summarize
            lines = []
            for msg in old_messages:
                role = "User" if msg.role == "user" else "Assistant"
                content = msg.content[:300] if msg.content else ""
                lines.append(f"{role}: {content}")
            conversation_text = "\n".join(lines)

            # LLM summarization
            from utils.llm_client import extract_response_content
            response = await llm_client.chat(
                model=model,
                messages=[
                    {"role": "system", "content": (
                        "Summarize this conversation in 3-5 sentences. "
                        "Preserve key decisions, entity names/IDs, and open questions. "
                        "Write in the same language as the conversation."
                    )},
                    {"role": "user", "content": conversation_text},
                ],
            )
            summary = extract_response_content(response)

            conversation.summary = summary
            await self.db.commit()
            logger.info(f"Conversation summary updated for {session_id} ({len(summary)} chars)")
            return summary

        except Exception as e:
            logger.warning(f"Failed to update summary: {e}")
            await self.db.rollback()
            return None

    async def load_summary(self, session_id: str) -> str | None:
        """Load the conversation summary. Returns None if none exists."""
        try:
            result = await self.db.execute(
                select(Conversation.summary).where(
                    Conversation.session_id == session_id
                )
            )
            return result.scalar_one_or_none()
        except Exception as e:
            logger.warning(f"Failed to load summary: {e}")
            return None

    async def get_summary(
        self,
        session_id: str
    ) -> dict | None:
        """
        Hole Zusammenfassung einer Konversation.

        Args:
            session_id: Session ID der Konversation

        Returns:
            Dict mit session_id, created_at, updated_at, message_count, first_message, last_message
        """
        try:
            result = await self.db.execute(
                select(Conversation).where(Conversation.session_id == session_id)
            )
            conversation = result.scalar_one_or_none()

            if not conversation:
                return None

            # Zähle Nachrichten
            result = await self.db.execute(
                select(func.count(Message.id))
                .where(Message.conversation_id == conversation.id)
            )
            message_count = result.scalar()

            # Hole erste und letzte Nachricht
            result = await self.db.execute(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.timestamp.asc())
                .limit(1)
            )
            first_message = result.scalar_one_or_none()

            result = await self.db.execute(
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
            logger.error(f"Fehler beim Laden der Zusammenfassung: {e}")
            return None

    async def delete(
        self,
        session_id: str
    ) -> bool:
        """
        Lösche eine komplette Konversation.

        Args:
            session_id: Session ID der zu löschenden Konversation

        Returns:
            True wenn gelöscht, False wenn nicht gefunden
        """
        try:
            result = await self.db.execute(
                select(Conversation).where(Conversation.session_id == session_id)
            )
            conversation = result.scalar_one_or_none()

            if conversation:
                await self.db.delete(conversation)
                await self.db.commit()
                logger.info(f"Konversation gelöscht: {session_id}")
                return True

            return False

        except Exception as e:
            logger.error(f"Fehler beim Löschen der Konversation: {e}")
            await self.db.rollback()
            return False

    async def list_all(
        self,
        limit: int = 50,
        offset: int = 0,
        user_id: int | None = None,
    ) -> list[dict]:
        """
        Hole Liste aller Konversationen.

        Args:
            limit: Maximale Anzahl
            offset: Pagination-Offset
            user_id: Wenn gesetzt, nur Konversationen dieses Users (auth mode).
                Wenn None, werden alle Konversationen zurueckgegeben (single-user mode).

        Returns:
            Liste von Konversations-Zusammenfassungen mit `preview` (gekuerzte
            erste User-Nachricht) und `message_count`.
        """
        try:
            # Count subquery: message count per conversation
            count_subq = (
                select(
                    Message.conversation_id,
                    func.count(Message.id).label("message_count")
                )
                .group_by(Message.conversation_id)
                .subquery()
            )

            # Preview subquery: first user message per conversation
            preview_subq = (
                select(
                    Message.conversation_id,
                    Message.content,
                    func.row_number().over(
                        partition_by=Message.conversation_id,
                        order_by=Message.timestamp.asc()
                    ).label("rn")
                )
                .where(Message.role == "user")
                .subquery()
            )
            first_msg = aliased(preview_subq)

            # Main query joining both subqueries
            stmt = (
                select(
                    Conversation,
                    func.coalesce(count_subq.c.message_count, 0).label("message_count"),
                    first_msg.c.content.label("preview_content"),
                )
                .outerjoin(count_subq, Conversation.id == count_subq.c.conversation_id)
                .outerjoin(first_msg, (Conversation.id == first_msg.c.conversation_id) & (first_msg.c.rn == 1))
                .order_by(Conversation.updated_at.desc())
                .limit(limit)
                .offset(offset)
            )
            if user_id is not None:
                stmt = stmt.where(Conversation.user_id == user_id)

            result = await self.db.execute(stmt)
            rows = result.all()

            summaries = []
            for conv, message_count, preview_content in rows:
                summaries.append({
                    "session_id": conv.session_id,
                    "created_at": conv.created_at.isoformat(),
                    "updated_at": conv.updated_at.isoformat(),
                    "message_count": message_count,
                    "preview": preview_content[:100] if preview_content else "Leere Konversation"
                })

            logger.info(f"Geladen: {len(summaries)} Konversationen")
            return summaries

        except Exception as e:
            logger.error(f"Fehler beim Laden der Konversationen: {e}")
            return []

    async def search(
        self,
        query: str,
        limit: int = 20
    ) -> list[dict]:
        """
        Suche in Konversationen nach Text.

        Args:
            query: Suchbegriff
            limit: Maximale Anzahl Ergebnisse

        Returns:
            Liste von Konversationen mit passenden Nachrichten
        """
        try:
            # Search messages and join conversations in a single query
            result = await self.db.execute(
                select(Message, Conversation)
                .join(Conversation, Message.conversation_id == Conversation.id)
                .where(Message.content.ilike(f"%{query}%"))
                .order_by(Message.timestamp.desc())
                .limit(limit)
            )
            rows = result.all()

            # Group by conversation
            from collections import OrderedDict
            conv_groups: OrderedDict[int, dict] = OrderedDict()
            for msg, conv in rows:
                if conv.id not in conv_groups:
                    conv_groups[conv.id] = {
                        "session_id": conv.session_id,
                        "created_at": conv.created_at.isoformat(),
                        "updated_at": conv.updated_at.isoformat(),
                        "matching_messages": []
                    }
                conv_groups[conv.id]["matching_messages"].append({
                    "role": msg.role,
                    "content": msg.content,
                    "timestamp": msg.timestamp.isoformat()
                })

            results = list(conv_groups.values())
            logger.info(f"Gefunden: {len(results)} Konversationen mit '{query}'")
            return results

        except Exception as e:
            logger.error(f"Fehler bei der Suche: {e}")
            return []
