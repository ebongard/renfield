"""Tests for Conversation persistence and management.

Tests ConversationService CRUD operations using the database fixtures from conftest.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Conversation, Message
from services.conversation_service import ConversationService

# ============================================================================
# ConversationService.save_message Tests
# ============================================================================

@pytest.mark.backend
@pytest.mark.database
class TestSaveMessage:
    """Tests for ConversationService.save_message()."""

    async def test_save_message_creates_conversation(self, db_session: AsyncSession):
        service = ConversationService(db_session)
        msg = await service.save_message("new-session-1", "user", "Hello")

        assert msg.id is not None
        assert msg.role == "user"
        assert msg.content == "Hello"

        # Verify conversation was created
        result = await db_session.execute(
            select(Conversation).where(Conversation.session_id == "new-session-1")
        )
        conv = result.scalar_one()
        assert conv is not None
        assert conv.session_id == "new-session-1"

    async def test_save_message_reuses_existing_conversation(self, db_session: AsyncSession):
        service = ConversationService(db_session)
        await service.save_message("session-reuse", "user", "First")
        await service.save_message("session-reuse", "assistant", "Response")

        # Should only have one conversation
        result = await db_session.execute(
            select(Conversation).where(Conversation.session_id == "session-reuse")
        )
        conversations = result.scalars().all()
        assert len(conversations) == 1

        # Should have two messages
        conv = conversations[0]
        result = await db_session.execute(
            select(Message).where(Message.conversation_id == conv.id)
        )
        messages = result.scalars().all()
        assert len(messages) == 2

    async def test_save_message_with_metadata(self, db_session: AsyncSession):
        service = ConversationService(db_session)
        metadata = {"intent": "homeassistant.turn_on", "confidence": 0.95}
        msg = await service.save_message("session-meta", "assistant", "Done", metadata=metadata)
        assert msg.message_metadata == metadata

    async def test_save_message_updates_conversation_timestamp(self, db_session: AsyncSession):
        service = ConversationService(db_session)
        await service.save_message("session-ts", "user", "First")

        result = await db_session.execute(
            select(Conversation).where(Conversation.session_id == "session-ts")
        )
        conv = result.scalar_one()
        first_updated = conv.updated_at

        await service.save_message("session-ts", "user", "Second")
        await db_session.refresh(conv)
        # updated_at should be >= first (may be same if fast enough)
        assert conv.updated_at >= first_updated


# ============================================================================
# ConversationService.load_context Tests
# ============================================================================

@pytest.mark.backend
@pytest.mark.database
class TestLoadContext:
    """Tests for ConversationService.load_context()."""

    async def test_load_context_empty_session(self, db_session: AsyncSession):
        service = ConversationService(db_session)
        result = await service.load_context("nonexistent-session")
        assert result == []

    async def test_load_context_returns_messages(self, db_session: AsyncSession):
        service = ConversationService(db_session)
        await service.save_message("ctx-session", "user", "Hello")
        await service.save_message("ctx-session", "assistant", "Hi there")
        await service.save_message("ctx-session", "user", "How are you?")

        context = await service.load_context("ctx-session")
        assert len(context) == 3
        assert context[0]["role"] == "user"
        assert context[0]["content"] == "Hello"
        assert context[-1]["role"] == "user"
        assert context[-1]["content"] == "How are you?"

    async def test_load_context_respects_max_messages(self, db_session: AsyncSession):
        service = ConversationService(db_session)
        for i in range(10):
            await service.save_message("ctx-limit", "user", f"Message {i}")

        context = await service.load_context("ctx-limit", max_messages=3)
        assert len(context) == 3
        # Should be the 3 most recent, in chronological order
        assert context[0]["content"] == "Message 7"
        assert context[-1]["content"] == "Message 9"

    async def test_load_context_with_fixture_conversation(
        self, db_session: AsyncSession, test_conversation: Conversation, test_message: Message
    ):
        service = ConversationService(db_session)
        context = await service.load_context(test_conversation.session_id)
        assert len(context) == 1
        assert context[0]["content"] == test_message.content


# ============================================================================
# ConversationService.get_summary Tests
# ============================================================================

@pytest.mark.backend
@pytest.mark.database
class TestGetSummary:
    """Tests for ConversationService.get_summary()."""

    async def test_summary_nonexistent(self, db_session: AsyncSession):
        service = ConversationService(db_session)
        result = await service.get_summary("no-such-session")
        assert result is None

    async def test_summary_fields(self, db_session: AsyncSession):
        service = ConversationService(db_session)
        await service.save_message("summary-session", "user", "First message")
        await service.save_message("summary-session", "assistant", "Last message")

        summary = await service.get_summary("summary-session")
        assert summary is not None
        assert summary["session_id"] == "summary-session"
        assert summary["message_count"] == 2
        assert summary["first_message"] == "First message"
        assert summary["last_message"] == "Last message"
        assert "created_at" in summary
        assert "updated_at" in summary


# ============================================================================
# ConversationService.delete Tests
# ============================================================================

@pytest.mark.backend
@pytest.mark.database
class TestDelete:
    """Tests for ConversationService.delete()."""

    async def test_delete_existing_conversation(self, db_session: AsyncSession):
        service = ConversationService(db_session)
        await service.save_message("del-session", "user", "to delete")
        await service.save_message("del-session", "assistant", "response")

        result = await service.delete("del-session")
        assert result is True

        # Conversation should be gone
        db_result = await db_session.execute(
            select(Conversation).where(Conversation.session_id == "del-session")
        )
        assert db_result.scalar_one_or_none() is None

    async def test_delete_nonexistent_returns_false(self, db_session: AsyncSession):
        service = ConversationService(db_session)
        result = await service.delete("no-such-session")
        assert result is False

    async def test_delete_cascades_to_messages(self, db_session: AsyncSession):
        service = ConversationService(db_session)
        await service.save_message("cascade-session", "user", "msg1")
        await service.save_message("cascade-session", "assistant", "msg2")

        # Get conversation id first
        db_result = await db_session.execute(
            select(Conversation).where(Conversation.session_id == "cascade-session")
        )
        conv = db_result.scalar_one()
        conv_id = conv.id

        await service.delete("cascade-session")

        # Messages should be gone too (cascade)
        msg_result = await db_session.execute(
            select(Message).where(Message.conversation_id == conv_id)
        )
        assert msg_result.scalars().all() == []


# ============================================================================
# ConversationService.list_all Tests
# ============================================================================

@pytest.mark.backend
@pytest.mark.database
class TestListAll:
    """Tests for ConversationService.list_all()."""

    async def test_list_empty(self, db_session: AsyncSession):
        service = ConversationService(db_session)
        result = await service.list_all()
        assert result == []

    async def test_list_with_conversations(self, db_session: AsyncSession):
        service = ConversationService(db_session)
        await service.save_message("list-1", "user", "First conv")
        await service.save_message("list-2", "user", "Second conv")

        result = await service.list_all()
        assert len(result) == 2
        # Each entry should have expected fields
        for entry in result:
            assert "session_id" in entry
            assert "created_at" in entry
            assert "updated_at" in entry
            assert "message_count" in entry
            assert "preview" in entry

    async def test_list_with_pagination(self, db_session: AsyncSession):
        service = ConversationService(db_session)
        for i in range(5):
            await service.save_message(f"page-{i}", "user", f"Conv {i}")

        page1 = await service.list_all(limit=2, offset=0)
        assert len(page1) == 2

        page2 = await service.list_all(limit=2, offset=2)
        assert len(page2) == 2

        page3 = await service.list_all(limit=2, offset=4)
        assert len(page3) == 1

    async def test_list_ordered_by_updated_at_desc(self, db_session: AsyncSession):
        service = ConversationService(db_session)
        await service.save_message("older", "user", "Old")
        await service.save_message("newer", "user", "New")

        result = await service.list_all()
        # Most recently updated first
        assert result[0]["session_id"] == "newer"
        assert result[1]["session_id"] == "older"


# ============================================================================
# ConversationService.search Tests
# ============================================================================

@pytest.mark.backend
@pytest.mark.database
class TestSearch:
    """Tests for ConversationService.search()."""

    async def test_search_no_results(self, db_session: AsyncSession):
        service = ConversationService(db_session)
        await service.save_message("s1", "user", "Hello world")
        result = await service.search("nonexistent-xyz")
        assert result == []

    async def test_search_finds_matching_messages(self, db_session: AsyncSession):
        service = ConversationService(db_session)
        await service.save_message("s1", "user", "Schalte das Licht ein")
        await service.save_message("s2", "user", "Wie wird das Wetter")
        await service.save_message("s3", "user", "Mach das Licht aus")

        result = await service.search("Licht")
        assert len(result) == 2
        session_ids = [r["session_id"] for r in result]
        assert "s1" in session_ids
        assert "s3" in session_ids

    async def test_search_case_insensitive(self, db_session: AsyncSession):
        service = ConversationService(db_session)
        await service.save_message("ci", "user", "UPPERCASE Test")
        result = await service.search("uppercase")
        assert len(result) == 1

    async def test_search_groups_by_conversation(self, db_session: AsyncSession):
        service = ConversationService(db_session)
        await service.save_message("group", "user", "keyword here")
        await service.save_message("group", "assistant", "also keyword")

        result = await service.search("keyword")
        assert len(result) == 1
        assert len(result[0]["matching_messages"]) == 2

    async def test_search_respects_limit(self, db_session: AsyncSession):
        service = ConversationService(db_session)
        for i in range(10):
            await service.save_message(f"limit-{i}", "user", f"common term {i}")

        result = await service.search("common term", limit=3)
        # limit applies to messages, so we get at most 3 message matches
        total_messages = sum(len(r["matching_messages"]) for r in result)
        assert total_messages <= 3


# ============================================================================
# Database Model Tests
# ============================================================================

@pytest.mark.backend
@pytest.mark.database
class TestConversationModel:
    """Tests for the Conversation and Message models."""

    async def test_conversation_creation(self, db_session: AsyncSession):
        conv = Conversation(session_id="model-test-1")
        db_session.add(conv)
        await db_session.commit()
        await db_session.refresh(conv)

        assert conv.id is not None
        assert conv.session_id == "model-test-1"
        assert conv.created_at is not None
        assert conv.updated_at is not None

    async def test_message_creation(self, db_session: AsyncSession):
        conv = Conversation(session_id="model-msg-test")
        db_session.add(conv)
        await db_session.commit()
        await db_session.refresh(conv)

        msg = Message(
            conversation_id=conv.id,
            role="user",
            content="Test content",
            message_metadata={"key": "value"},
        )
        db_session.add(msg)
        await db_session.commit()
        await db_session.refresh(msg)

        assert msg.id is not None
        assert msg.conversation_id == conv.id
        assert msg.role == "user"
        assert msg.content == "Test content"
        assert msg.message_metadata == {"key": "value"}
        assert msg.timestamp is not None

    async def test_conversation_unique_session_id(self, db_session: AsyncSession):
        from sqlalchemy.exc import IntegrityError

        conv1 = Conversation(session_id="unique-test")
        db_session.add(conv1)
        await db_session.commit()

        conv2 = Conversation(session_id="unique-test")
        db_session.add(conv2)
        with pytest.raises(IntegrityError):
            await db_session.commit()

    async def test_message_ordering(self, db_session: AsyncSession):
        service = ConversationService(db_session)
        await service.save_message("order-test", "user", "First")
        await service.save_message("order-test", "assistant", "Second")
        await service.save_message("order-test", "user", "Third")

        context = await service.load_context("order-test")
        assert [m["content"] for m in context] == ["First", "Second", "Third"]

    async def test_conversation_with_fixture(self, test_conversation: Conversation, test_message: Message):
        """Test using conftest fixtures."""
        assert test_conversation.session_id == "test-session-123"
        assert test_message.conversation_id == test_conversation.id
        assert test_message.role == "user"
        assert test_message.content == "Schalte das Licht ein"
