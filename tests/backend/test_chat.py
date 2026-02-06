"""
Tests für Chat API

Testet:
- Chat-Nachrichten senden
- Konversations-Historie
- Session-Management
- Suche und Statistiken
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Conversation, Message

# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def sample_chat_request():
    """Sample chat request data"""
    return {
        "message": "Schalte das Licht im Wohnzimmer ein",
        "session_id": "test-session-chat-123"
    }


@pytest.fixture
async def conversation_with_messages(db_session: AsyncSession) -> Conversation:
    """Create a conversation with multiple messages"""
    conv = Conversation(session_id="test-conv-with-messages")
    db_session.add(conv)
    await db_session.commit()
    await db_session.refresh(conv)

    # Add messages
    messages = [
        Message(conversation_id=conv.id, role="user", content="Hallo"),
        Message(conversation_id=conv.id, role="assistant", content="Hallo! Wie kann ich helfen?"),
        Message(conversation_id=conv.id, role="user", content="Schalte das Licht ein"),
        Message(conversation_id=conv.id, role="assistant", content="Ich habe das Licht eingeschaltet."),
    ]
    for msg in messages:
        db_session.add(msg)

    await db_session.commit()
    return conv


@pytest.fixture
async def old_conversation(db_session: AsyncSession) -> Conversation:
    """Create an old conversation for cleanup tests"""
    conv = Conversation(session_id="old-conv-to-delete")
    conv.updated_at = datetime.utcnow() - timedelta(days=60)
    db_session.add(conv)
    await db_session.commit()
    await db_session.refresh(conv)
    return conv


# ============================================================================
# Model Tests
# ============================================================================

class TestConversationModel:
    """Tests für das Conversation Model"""

    @pytest.mark.database
    async def test_create_conversation(self, db_session: AsyncSession):
        """Testet das Erstellen einer Konversation"""
        conv = Conversation(session_id="new-test-session")
        db_session.add(conv)
        await db_session.commit()
        await db_session.refresh(conv)

        assert conv.id is not None
        assert conv.session_id == "new-test-session"
        assert conv.created_at is not None

    @pytest.mark.database
    async def test_conversation_unique_session_id(self, db_session: AsyncSession, test_conversation):
        """Testet, dass session_id eindeutig sein muss"""
        from sqlalchemy.exc import IntegrityError

        duplicate = Conversation(session_id=test_conversation.session_id)
        db_session.add(duplicate)

        with pytest.raises(IntegrityError):
            await db_session.commit()


class TestMessageModel:
    """Tests für das Message Model"""

    @pytest.mark.database
    async def test_create_message(self, db_session: AsyncSession, test_conversation):
        """Testet das Erstellen einer Nachricht"""
        msg = Message(
            conversation_id=test_conversation.id,
            role="user",
            content="Test message"
        )
        db_session.add(msg)
        await db_session.commit()
        await db_session.refresh(msg)

        assert msg.id is not None
        assert msg.role == "user"
        assert msg.content == "Test message"
        assert msg.timestamp is not None

    @pytest.mark.database
    async def test_message_with_metadata(self, db_session: AsyncSession, test_conversation):
        """Testet Nachricht mit Metadata"""
        metadata = {"intent": "homeassistant.turn_on", "entity_id": "light.wohnzimmer"}
        msg = Message(
            conversation_id=test_conversation.id,
            role="assistant",
            content="Licht eingeschaltet",
            message_metadata=metadata
        )
        db_session.add(msg)
        await db_session.commit()
        await db_session.refresh(msg)

        assert msg.message_metadata is not None
        assert msg.message_metadata["intent"] == "homeassistant.turn_on"


# ============================================================================
# API Tests
# ============================================================================

class TestChatHistoryAPI:
    """Tests für Chat-Historie API"""

    @pytest.mark.integration
    async def test_get_history_empty(self, async_client: AsyncClient):
        """Testet GET /api/chat/history für nicht-existente Session"""
        response = await async_client.get("/api/chat/history/non-existent-session")

        assert response.status_code == 200
        data = response.json()
        assert data["messages"] == []

    @pytest.mark.integration
    async def test_get_history_with_messages(
        self,
        async_client: AsyncClient,
        conversation_with_messages: Conversation
    ):
        """Testet GET /api/chat/history mit Nachrichten"""
        response = await async_client.get(
            f"/api/chat/history/{conversation_with_messages.session_id}"
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["messages"]) == 4
        assert data["messages"][0]["role"] == "user"
        assert data["messages"][0]["content"] == "Hallo"


class TestChatSessionAPI:
    """Tests für Session-Management API"""

    @pytest.mark.integration
    async def test_delete_session(
        self,
        async_client: AsyncClient,
        test_conversation: Conversation
    ):
        """Testet DELETE /api/chat/session"""
        response = await async_client.delete(
            f"/api/chat/session/{test_conversation.session_id}"
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    @pytest.mark.integration
    async def test_delete_nonexistent_session(self, async_client: AsyncClient):
        """Testet DELETE für nicht-existente Session"""
        response = await async_client.delete("/api/chat/session/nonexistent")

        # Should still return success (idempotent)
        assert response.status_code == 200


class TestConversationsAPI:
    """Tests für Konversations-Liste API"""

    @pytest.mark.integration
    async def test_list_conversations(
        self,
        async_client: AsyncClient,
        conversation_with_messages: Conversation
    ):
        """Testet GET /api/chat/conversations"""
        # Mock the ollama service method - app is imported from main module
        with patch('main.app') as mock_app:
            mock_ollama = AsyncMock()
            mock_ollama.get_all_conversations = AsyncMock(return_value=[
                {
                    "session_id": conversation_with_messages.session_id,
                    "message_count": 4,
                    "created_at": datetime.utcnow().isoformat()
                }
            ])
            mock_app.state.ollama = mock_ollama

            response = await async_client.get("/api/chat/conversations")

        assert response.status_code == 200
        data = response.json()
        assert "conversations" in data
        assert "count" in data


class TestChatStatsAPI:
    """Tests für Chat-Statistiken API"""

    @pytest.mark.integration
    async def test_get_stats(
        self,
        async_client: AsyncClient,
        conversation_with_messages: Conversation
    ):
        """Testet GET /api/chat/stats"""
        response = await async_client.get("/api/chat/stats")

        assert response.status_code == 200
        data = response.json()
        assert "total_conversations" in data
        assert "total_messages" in data
        assert "avg_messages_per_conversation" in data
        assert "messages_last_24h" in data


class TestChatSearchAPI:
    """Tests für Chat-Suche API"""

    @pytest.mark.integration
    async def test_search_too_short_query(self, async_client: AsyncClient):
        """Testet Suche mit zu kurzem Query"""
        response = await async_client.get("/api/chat/search?q=a")

        assert response.status_code == 400

    @pytest.mark.integration
    async def test_search_conversations(
        self,
        async_client: AsyncClient,
        conversation_with_messages: Conversation
    ):
        """Testet GET /api/chat/search"""
        # Mock the ollama service - app is imported from main module
        with patch('main.app') as mock_app:
            mock_ollama = AsyncMock()
            mock_ollama.search_conversations = AsyncMock(return_value=[
                {
                    "session_id": conversation_with_messages.session_id,
                    "content": "Licht eingeschaltet"
                }
            ])
            mock_app.state.ollama = mock_ollama

            response = await async_client.get("/api/chat/search?q=Licht")

        assert response.status_code == 200
        data = response.json()
        assert "query" in data
        assert "results" in data
        assert "count" in data


class TestChatCleanupAPI:
    """Tests für Cleanup API"""

    @pytest.mark.integration
    async def test_cleanup_old_conversations(
        self,
        async_client: AsyncClient,
        old_conversation: Conversation,
        db_session: AsyncSession
    ):
        """Testet DELETE /api/chat/conversations/cleanup"""
        response = await async_client.delete("/api/chat/conversations/cleanup?days=30")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "deleted_count" in data


# ============================================================================
# Query Tests
# ============================================================================

class TestChatQueries:
    """Tests für Chat-Abfragen"""

    @pytest.mark.database
    async def test_get_messages_ordered(
        self,
        db_session: AsyncSession,
        conversation_with_messages: Conversation
    ):
        """Testet, dass Nachrichten chronologisch sortiert sind"""
        result = await db_session.execute(
            select(Message)
            .where(Message.conversation_id == conversation_with_messages.id)
            .order_by(Message.timestamp.asc())
        )
        messages = result.scalars().all()

        assert len(messages) == 4
        # First message should be oldest
        for i in range(1, len(messages)):
            assert messages[i].timestamp >= messages[i-1].timestamp

    @pytest.mark.database
    async def test_count_messages_per_conversation(
        self,
        db_session: AsyncSession,
        conversation_with_messages: Conversation
    ):
        """Testet Nachrichten-Zählung pro Konversation"""
        from sqlalchemy import func

        result = await db_session.execute(
            select(func.count(Message.id))
            .where(Message.conversation_id == conversation_with_messages.id)
        )
        count = result.scalar()

        assert count == 4
