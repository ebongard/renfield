"""
Tests for the chat API

Covers:
- Sending chat messages
- Conversation history
- Session management
- Search and statistics
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

    # Add messages, CHAINED into the branch tree and with the conversation's
    # active leaf pointed at the last one — exactly the shape production rows
    # have had since the branching migration backfilled them. Without it the
    # message search (which walks the active path) finds nothing: the flat
    # fallback it used to hit only exists off Postgres.
    contents = [
        ("user", "Hallo"),
        ("assistant", "Hallo! Wie kann ich helfen?"),
        ("user", "Schalte das Licht ein"),
        ("assistant", "Ich habe das Licht eingeschaltet."),
    ]
    parent_id = None
    for role, content in contents:
        msg = Message(
            conversation_id=conv.id, role=role, content=content,
            parent_message_id=parent_id,
        )
        db_session.add(msg)
        await db_session.flush()
        parent_id = msg.id

    conv.active_leaf_message_id = parent_id
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
    """Tests for the Conversation model"""

    @pytest.mark.database
    async def test_create_conversation(self, db_session: AsyncSession):
        """Tests creating a conversation"""
        conv = Conversation(session_id="new-test-session")
        db_session.add(conv)
        await db_session.commit()
        await db_session.refresh(conv)

        assert conv.id is not None
        assert conv.session_id == "new-test-session"
        assert conv.created_at is not None

    @pytest.mark.database
    async def test_conversation_unique_session_id(self, db_session: AsyncSession, test_conversation):
        """Tests that session_id must be unique"""
        from sqlalchemy.exc import IntegrityError

        duplicate = Conversation(session_id=test_conversation.session_id)
        db_session.add(duplicate)

        with pytest.raises(IntegrityError):
            await db_session.commit()


class TestMessageModel:
    """Tests for the Message model"""

    @pytest.mark.database
    async def test_create_message(self, db_session: AsyncSession, test_conversation):
        """Tests creating a message"""
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
        """Tests a message with metadata"""
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
    """Tests for the chat history API"""

    @pytest.mark.integration
    async def test_get_history_empty(self, async_client: AsyncClient):
        """Tests GET /api/chat/history for a session that does not exist"""
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
        """Tests GET /api/chat/history with messages"""
        response = await async_client.get(
            f"/api/chat/history/{conversation_with_messages.session_id}"
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["messages"]) == 4
        assert data["messages"][0]["role"] == "user"
        assert data["messages"][0]["content"] == "Hallo"


class TestChatSessionAPI:
    """Tests for the session management API"""

    @pytest.mark.integration
    async def test_delete_session(
        self,
        async_client: AsyncClient,
        test_conversation: Conversation
    ):
        """Tests DELETE /api/chat/session"""
        response = await async_client.delete(
            f"/api/chat/session/{test_conversation.session_id}"
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    @pytest.mark.integration
    async def test_delete_nonexistent_session(self, async_client: AsyncClient):
        """Tests DELETE for a session that does not exist"""
        response = await async_client.delete("/api/chat/session/nonexistent")

        # Should still return success (idempotent)
        assert response.status_code == 200


class TestConversationsAPI:
    """Tests for the conversation list API"""

    @pytest.mark.integration
    async def test_list_conversations(
        self,
        async_client: AsyncClient,
        conversation_with_messages: Conversation
    ):
        """Tests GET /api/chat/conversations"""
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
    """Tests for the chat statistics API"""

    @pytest.mark.integration
    async def test_get_stats(
        self,
        async_client: AsyncClient,
        conversation_with_messages: Conversation
    ):
        """Tests GET /api/chat/stats"""
        response = await async_client.get("/api/chat/stats")

        assert response.status_code == 200
        data = response.json()
        assert "total_conversations" in data
        assert "total_messages" in data
        assert "avg_messages_per_conversation" in data
        assert "messages_last_24h" in data


class TestChatSearchAPI:
    """Tests for the chat search API"""

    @pytest.mark.integration
    async def test_search_too_short_query(self, async_client: AsyncClient):
        """Tests a search with too short a query"""
        response = await async_client.get("/api/chat/search?q=a")

        assert response.status_code == 400

    @pytest.mark.integration
    async def test_search_conversations(
        self,
        async_client: AsyncClient,
        conversation_with_messages: Conversation
    ):
        """Tests GET /api/chat/search"""
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


class TestChatMessageSearchAPI:
    """Tests for the message search API (GET /api/chat/messages/search, item 3)."""

    @pytest.mark.integration
    async def test_message_search_too_short_query(self, async_client: AsyncClient):
        response = await async_client.get("/api/chat/messages/search?q=a")
        assert response.status_code == 400

    @pytest.mark.integration
    async def test_message_search_returns_shape_and_finds_match(
        self,
        async_client: AsyncClient,
        conversation_with_messages: Conversation,
    ):
        """Single-user mode (no auth) → sqlite FTS fallback finds the message."""
        response = await async_client.get("/api/chat/messages/search?q=Licht")
        assert response.status_code == 200
        data = response.json()
        assert data["query"] == "Licht"
        assert "results" in data and "count" in data and "has_more" in data
        sessions = {r["session_id"] for r in data["results"]}
        assert conversation_with_messages.session_id in sessions
        # Every hit carries the jump-to-message metadata.
        for r in data["results"]:
            assert isinstance(r["message_index"], int)
            assert "snippet" in r and "role" in r

    @pytest.mark.integration
    async def test_message_search_in_conversation_scope(
        self,
        async_client: AsyncClient,
        conversation_with_messages: Conversation,
    ):
        response = await async_client.get(
            f"/api/chat/messages/search?q=Licht&session_id={conversation_with_messages.session_id}"
        )
        assert response.status_code == 200
        data = response.json()
        assert {r["session_id"] for r in data["results"]} <= {
            conversation_with_messages.session_id
        }


class TestChatCleanupAPI:
    """Tests for the cleanup API"""

    @pytest.mark.integration
    async def test_cleanup_old_conversations(
        self,
        async_client: AsyncClient,
        old_conversation: Conversation,
        db_session: AsyncSession
    ):
        """Tests DELETE /api/chat/conversations/cleanup"""
        response = await async_client.delete("/api/chat/conversations/cleanup?days=30")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "deleted_count" in data


# ============================================================================
# Query Tests
# ============================================================================

class TestChatQueries:
    """Tests for the chat queries"""

    @pytest.mark.database
    async def test_get_messages_ordered(
        self,
        db_session: AsyncSession,
        conversation_with_messages: Conversation
    ):
        """Tests that messages are sorted chronologically"""
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
        """Tests counting messages per conversation"""
        from sqlalchemy import func

        result = await db_session.execute(
            select(func.count(Message.id))
            .where(Message.conversation_id == conversation_with_messages.id)
        )
        count = result.scalar()

        assert count == 4


class TestRestChatOwnership:
    """`/api/chat/send` is the fallback the browser uses while the socket is not
    ready. It finds the conversation by the CLIENT-supplied session id, so
    without a boundary it would read the last 10 messages of any conversation it
    merely named and append to it — the same rule as the WS path has to hold
    here (auth-on cutover §4.2).

    Real rows, not in-memory model objects: since §8.1 the rule is tier REACH,
    which is four branches of Postgres SQL. A stub conversation could only ever
    re-assert owner equality, which is the thing that changed. The POSITIVE
    reach case (a member continuing the device account's shared thread) lives in
    `test_shared_conversations_boundaries.py` next to its circle memberships;
    what this class pins is that the refusals still refuse.
    """

    @staticmethod
    async def _conv(db, owner_id):
        from models.database import Conversation

        conv = Conversation(session_id=f"s{owner_id}", user_id=owner_id)
        db.add(conv)
        await db.flush()
        return conv

    @pytest.mark.database
    async def test_own_conversation_continues(self, db_session, monkeypatch):
        from api.routes import chat as chat_routes
        from tests.backend.dbrows import ensure_user

        monkeypatch.setattr(chat_routes.settings, "auth_enabled", True)
        user = await ensure_user(db_session, 7)
        conv = await self._conv(db_session, 7)
        assert await chat_routes.conversation_is_callers(conv, user, db_session) is True

    @pytest.mark.database
    async def test_foreign_conversation_is_refused(self, db_session, monkeypatch):
        """No membership anywhere, so no reach: a tier-0 row is nobody else's."""
        from api.routes import chat as chat_routes
        from tests.backend.dbrows import ensure_user

        monkeypatch.setattr(chat_routes.settings, "auth_enabled", True)
        await ensure_user(db_session, 1)
        caller = await ensure_user(db_session, 7)
        conv = await self._conv(db_session, 1)
        assert await chat_routes.conversation_is_callers(conv, caller, db_session) is False

    @pytest.mark.database
    async def test_ownerless_conversation_is_refused(self, db_session, monkeypatch):
        """The case this change is about, and it survives the move to reach:
        with adoption gone an ownerless row stays ownerless, every reach branch
        keys on the owner, so nobody reaches it — anyone holding the id could
        otherwise continue it."""
        from api.routes import chat as chat_routes
        from tests.backend.dbrows import ensure_user

        monkeypatch.setattr(chat_routes.settings, "auth_enabled", True)
        caller = await ensure_user(db_session, 7)
        conv = await self._conv(db_session, None)
        assert await chat_routes.conversation_is_callers(conv, caller, db_session) is False

    @pytest.mark.database
    async def test_a_session_with_no_conversation_is_free(self, db_session, monkeypatch):
        from api.routes import chat as chat_routes
        from tests.backend.dbrows import ensure_user

        monkeypatch.setattr(chat_routes.settings, "auth_enabled", True)
        caller = await ensure_user(db_session, 7)
        assert await chat_routes.conversation_is_callers(None, caller, db_session) is True

    @pytest.mark.database
    async def test_auth_off_is_unchanged(self, db_session, monkeypatch):
        from api.routes import chat as chat_routes
        from tests.backend.dbrows import ensure_user

        monkeypatch.setattr(chat_routes.settings, "auth_enabled", False)
        await ensure_user(db_session, 1)
        caller = await ensure_user(db_session, 7)
        conv = await self._conv(db_session, 1)
        assert await chat_routes.conversation_is_callers(conv, caller, db_session) is True

    @pytest.mark.database
    async def test_no_caller_identity_is_unchanged(self, db_session, monkeypatch):
        # Device/satellite path: no JWT user, legacy behaviour.
        from api.routes import chat as chat_routes
        from tests.backend.dbrows import ensure_user

        monkeypatch.setattr(chat_routes.settings, "auth_enabled", True)
        await ensure_user(db_session, 1)
        conv = await self._conv(db_session, 1)
        assert await chat_routes.conversation_is_callers(conv, None, db_session) is True
