"""Tests for conversation handoff between satellites."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from models.database import Conversation, Message
from services.conversation_handoff import (
    _DEBOUNCE_SECONDS,
    _last_handoff,
    try_handoff_context,
)


@pytest.fixture(autouse=True)
def clear_debounce():
    """Clear the debounce dict between tests."""
    _last_handoff.clear()
    yield
    _last_handoff.clear()


def _make_conversation(session_id, speaker_id, user_id=None, context_vars=None, summary=None, minutes_ago=5):
    """Create a Conversation-like mock for testing."""
    conv = MagicMock(spec=Conversation)
    conv.id = hash(session_id) % 10000
    conv.session_id = session_id
    conv.speaker_id = speaker_id
    conv.user_id = user_id
    conv.context_vars = context_vars
    conv.summary = summary
    conv.updated_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=minutes_ago)
    return conv


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handoff_happy_path(db_session):
    """Speaker has recent conversation at another satellite — context copied."""
    # Create source conversation at satellite-A
    source = Conversation(
        session_id="satellite-sat-a-2026-03-30",
        speaker_id=1,
        user_id=10,
        context_vars={"topic": "weather", "entity_id": 42},
        summary="User asked about the weather in Berlin.",
    )
    db_session.add(source)
    await db_session.commit()

    # Save a message in the source conversation
    msg = Message(
        conversation_id=source.id,
        role="user",
        content="Wie ist das Wetter in Berlin?",
    )
    db_session.add(msg)
    await db_session.commit()

    # Try handoff to satellite-B
    result = await try_handoff_context(
        speaker_id=1,
        target_session_id="satellite-sat-b-2026-03-30",
        db=db_session,
    )

    assert result is True

    # Verify target conversation was created with copied context
    target_result = await db_session.execute(
        select(Conversation).where(Conversation.session_id == "satellite-sat-b-2026-03-30")
    )
    target = target_result.scalar_one()
    assert target.speaker_id == 1
    assert target.user_id == 10
    assert target.context_vars == {"topic": "weather", "entity_id": 42}
    assert target.summary == "User asked about the weather in Berlin."


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handoff_no_source(db_session):
    """Speaker has no prior satellite conversation — returns False."""
    result = await try_handoff_context(
        speaker_id=999,
        target_session_id="satellite-sat-b-2026-03-30",
        db=db_session,
    )
    assert result is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handoff_idempotent(db_session):
    """Target already has more recent data — skip."""
    # Create source (older)
    source = Conversation(
        session_id="satellite-sat-a-2026-03-30",
        speaker_id=1,
        context_vars={"old": True},
    )
    source.updated_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=10)
    db_session.add(source)

    # Create target (newer)
    target = Conversation(
        session_id="satellite-sat-b-2026-03-30",
        speaker_id=1,
        context_vars={"new": True},
    )
    target.updated_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1)
    db_session.add(target)
    await db_session.commit()

    result = await try_handoff_context(
        speaker_id=1,
        target_session_id="satellite-sat-b-2026-03-30",
        db=db_session,
    )

    assert result is False

    # Verify target was not overwritten
    target_result = await db_session.execute(
        select(Conversation).where(Conversation.session_id == "satellite-sat-b-2026-03-30")
    )
    target = target_result.scalar_one()
    assert target.context_vars == {"new": True}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handoff_debounce(db_session):
    """Second call within 10s for same speaker — no-op."""
    # Create source conversation
    source = Conversation(
        session_id="satellite-sat-a-2026-03-30",
        speaker_id=1,
        context_vars={"topic": "test"},
    )
    db_session.add(source)
    await db_session.commit()

    # First call succeeds
    result1 = await try_handoff_context(
        speaker_id=1,
        target_session_id="satellite-sat-b-2026-03-30",
        db=db_session,
    )
    assert result1 is True

    # Second call within debounce window — no-op
    result2 = await try_handoff_context(
        speaker_id=1,
        target_session_id="satellite-sat-c-2026-03-30",
        db=db_session,
    )
    assert result2 is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handoff_null_summary_copies_messages(db_session):
    """Source has NULL summary — copies last 5 messages as seed."""
    source = Conversation(
        session_id="satellite-sat-a-2026-03-30",
        speaker_id=1,
        context_vars={"key": "value"},
        summary=None,
    )
    db_session.add(source)
    await db_session.flush()

    # Add 7 messages — should copy last 5
    for i in range(7):
        msg = Message(
            conversation_id=source.id,
            role="user" if i % 2 == 0 else "assistant",
            content=f"Message {i}",
        )
        db_session.add(msg)
    await db_session.commit()

    result = await try_handoff_context(
        speaker_id=1,
        target_session_id="satellite-sat-b-2026-03-30",
        db=db_session,
    )
    assert result is True

    # Verify messages were copied
    target_result = await db_session.execute(
        select(Conversation).where(Conversation.session_id == "satellite-sat-b-2026-03-30")
    )
    target = target_result.scalar_one()

    msg_result = await db_session.execute(
        select(Message).where(Message.conversation_id == target.id)
    )
    copied_messages = msg_result.scalars().all()
    assert len(copied_messages) == 5
    assert all("handoff_source" in (m.message_metadata or {}) for m in copied_messages)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handoff_filters_web_conversations(db_session):
    """Web conversation (non-satellite session) is ignored."""
    # Create web conversation (more recent)
    web = Conversation(
        session_id="chat-web-device-abc",
        speaker_id=1,
        context_vars={"web": True},
    )
    db_session.add(web)
    await db_session.commit()

    result = await try_handoff_context(
        speaker_id=1,
        target_session_id="satellite-sat-b-2026-03-30",
        db=db_session,
    )
    assert result is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handoff_expired_source(db_session):
    """Source conversation older than window — no handoff."""
    source = Conversation(
        session_id="satellite-sat-a-2026-03-29",
        speaker_id=1,
        context_vars={"old": True},
    )
    source.updated_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=60)
    db_session.add(source)
    await db_session.commit()

    result = await try_handoff_context(
        speaker_id=1,
        target_session_id="satellite-sat-b-2026-03-30",
        db=db_session,
        window_minutes=30,
    )
    assert result is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handoff_auth_disabled_speaker_only(db_session):
    """Works with speaker_id alone, no user_id (auth disabled)."""
    source = Conversation(
        session_id="satellite-sat-a-2026-03-30",
        speaker_id=5,
        user_id=None,
        context_vars={"topic": "no-auth"},
    )
    db_session.add(source)
    await db_session.commit()

    result = await try_handoff_context(
        speaker_id=5,
        target_session_id="satellite-sat-b-2026-03-30",
        db=db_session,
    )
    assert result is True

    target_result = await db_session.execute(
        select(Conversation).where(Conversation.session_id == "satellite-sat-b-2026-03-30")
    )
    target = target_result.scalar_one()
    assert target.speaker_id == 5
    assert target.user_id is None
    assert target.context_vars == {"topic": "no-auth"}
