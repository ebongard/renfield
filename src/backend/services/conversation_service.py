"""
Conversation Service - Manages conversation persistence

Extracted from OllamaService for better separation of concerns.
Handles all database operations for conversations and messages.
"""
from typing import List, Dict, Optional
from datetime import datetime
from loguru import logger

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, literal_column
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
    ) -> List[Dict[str, str]]:
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
            context = [
                {"role": msg.role, "content": msg.content}
                for msg in reversed(messages)
            ]

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
        metadata: Optional[Dict] = None
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
            conversation.updated_at = datetime.utcnow()

            await self.db.commit()
            await self.db.refresh(message)

            logger.debug(f"Nachricht gespeichert: {role} - {content[:50]}...")
            return message

        except Exception as e:
            logger.error(f"Fehler beim Speichern der Nachricht: {e}")
            await self.db.rollback()
            raise

    async def get_summary(
        self,
        session_id: str
    ) -> Optional[Dict]:
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
        offset: int = 0
    ) -> List[Dict]:
        """
        Hole Liste aller Konversationen.

        Args:
            limit: Maximale Anzahl
            offset: Pagination-Offset

        Returns:
            Liste von Konversations-Zusammenfassungen
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
            result = await self.db.execute(
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
    ) -> List[Dict]:
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
