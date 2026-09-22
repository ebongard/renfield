"""Tests for ConversationService.associate_speaker()."""

import pytest
from sqlalchemy import select

from models.database import Conversation
from services.conversation_service import ConversationService

async def _seed_identities(db):
    """Speakers 10 and 42, users 3 and 7 — the ids these tests name."""
    from tests.backend.dbrows import ensure_speaker, ensure_user

    for sid in (10, 42):
        await ensure_speaker(db, sid)
    for uid in (3, 7):
        await ensure_user(db, uid)
    await db.commit()



@pytest.mark.unit
@pytest.mark.asyncio
async def test_associate_speaker_sets_speaker_id(db_session):
    """associate_speaker sets speaker_id on existing conversation."""
    await _seed_identities(db_session)
    conv = Conversation(session_id="satellite-test-2026-03-30")
    db_session.add(conv)
    await db_session.commit()

    svc = ConversationService(db_session)
    await svc.associate_speaker("satellite-test-2026-03-30", speaker_id=42)

    result = await db_session.execute(
        select(Conversation).where(Conversation.session_id == "satellite-test-2026-03-30")
    )
    conv = result.scalar_one()
    assert conv.speaker_id == 42
    assert conv.user_id is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_associate_speaker_sets_user_id_when_provided(db_session):
    """associate_speaker sets both speaker_id and user_id."""
    await _seed_identities(db_session)
    conv = Conversation(session_id="satellite-test-2026-03-30")
    db_session.add(conv)
    await db_session.commit()

    svc = ConversationService(db_session)
    await svc.associate_speaker("satellite-test-2026-03-30", speaker_id=42, user_id=7)

    result = await db_session.execute(
        select(Conversation).where(Conversation.session_id == "satellite-test-2026-03-30")
    )
    conv = result.scalar_one()
    assert conv.speaker_id == 42
    assert conv.user_id == 7


@pytest.mark.unit
@pytest.mark.asyncio
async def test_associate_speaker_idempotent(db_session):
    """associate_speaker does not overwrite existing speaker_id."""
    await _seed_identities(db_session)
    conv = Conversation(session_id="satellite-test-2026-03-30", speaker_id=10, user_id=3)
    db_session.add(conv)
    await db_session.commit()

    svc = ConversationService(db_session)
    await svc.associate_speaker("satellite-test-2026-03-30", speaker_id=99, user_id=88)

    result = await db_session.execute(
        select(Conversation).where(Conversation.session_id == "satellite-test-2026-03-30")
    )
    conv = result.scalar_one()
    assert conv.speaker_id == 10  # Not overwritten
    assert conv.user_id == 3  # Not overwritten


@pytest.mark.unit
@pytest.mark.asyncio
async def test_associate_speaker_nonexistent_session(db_session):
    """associate_speaker with nonexistent session_id is a no-op."""
    await _seed_identities(db_session)
    svc = ConversationService(db_session)
    await svc.associate_speaker("nonexistent-session", speaker_id=42)
    # Should not raise
