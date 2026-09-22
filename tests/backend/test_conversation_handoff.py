"""Tests for conversation handoff between satellites."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from models.database import Conversation, Message
from services.conversation_handoff import (
    _last_handoff,
    emit_continued_handoff_frame,
    try_handoff_context,
)


@pytest.fixture(autouse=True)
def clear_debounce():
    """Clear the debounce dict between tests."""
    _last_handoff.clear()
    yield
    _last_handoff.clear()


async def _seed_identities(db):
    """The speakers and users these conversations point at.

    `conversations.speaker_id → speakers.id` and `.user_id → users.id` are
    enforced by Postgres; the tests name speaker 1 (and 999 for the
    "no such speaker" case) and user 10. Only the rows that must EXIST are
    created — 999 stays absent on purpose.
    """
    from sqlalchemy import select as _select

    from models.database import Role, Speaker, User

    for sid in (1, 5):
        if (await db.execute(
            _select(Speaker).where(Speaker.id == sid)
        )).scalar_one_or_none() is None:
            db.add(Speaker(id=sid, name=f"Sprecher {sid}"))
    role = (await db.execute(
        _select(Role).where(Role.name == "handoff-rolle")
    )).scalar_one_or_none()
    if role is None:
        role = Role(name="handoff-rolle", permissions=[], is_system=False)
        db.add(role)
        await db.flush()
    if (await db.execute(_select(User).where(User.id == 10))).scalar_one_or_none() is None:
        db.add(User(id=10, username="handoff-nutzer", password_hash="x", role_id=role.id))
    await db.commit()


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
    await _seed_identities(db_session)
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
    await _seed_identities(db_session)
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
    await _seed_identities(db_session)
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
    await _seed_identities(db_session)
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
    await _seed_identities(db_session)
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
    await _seed_identities(db_session)
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
    await _seed_identities(db_session)
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
    await _seed_identities(db_session)
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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_continued_frame_emitted_when_enabled():
    """Flag on + room → broadcast a media_handoff/continued frame to that room."""
    dm = MagicMock()
    dm.broadcast_to_room = AsyncMock()
    with patch("services.conversation_handoff.settings") as mock_settings, \
         patch("ha_glue.services.device_manager.get_device_manager", return_value=dm):
        mock_settings.room_handoff_enabled = True
        await emit_continued_handoff_frame("Wohnzimmer")

    dm.broadcast_to_room.assert_awaited_once()
    room, frame = dm.broadcast_to_room.await_args.args
    assert room == "Wohnzimmer"
    assert frame == {
        "type": "media_handoff",
        "kind": "continued",
        "room": "Wohnzimmer",
        "title": "",
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_continued_frame_dark_when_flag_off():
    """Flag off → no frame (feature ships dark)."""
    dm = MagicMock()
    dm.broadcast_to_room = AsyncMock()
    with patch("services.conversation_handoff.settings") as mock_settings, \
         patch("ha_glue.services.device_manager.get_device_manager", return_value=dm):
        mock_settings.room_handoff_enabled = False
        await emit_continued_handoff_frame("Wohnzimmer")

    dm.broadcast_to_room.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_continued_frame_skipped_without_room():
    """No room name → nothing to target, no frame (even with flag on)."""
    dm = MagicMock()
    dm.broadcast_to_room = AsyncMock()
    with patch("services.conversation_handoff.settings") as mock_settings, \
         patch("ha_glue.services.device_manager.get_device_manager", return_value=dm):
        mock_settings.room_handoff_enabled = True
        await emit_continued_handoff_frame(None)

    dm.broadcast_to_room.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_continued_frame_only_on_successful_handoff():
    """on_presence_enter_room emits the chip only when ITS context copy succeeds.

    The chip fires from whichever of the two call sites wins the shared debounce.
    When this (presence) path loses — try_handoff_context returns False because
    the satellite speak-path already copied within the 10s window — the speak-path
    emits instead, so this path must NOT also emit (no duplicate)."""
    from services.conversation_handoff import on_presence_enter_room

    user = MagicMock()
    user.scalar_one_or_none = MagicMock(return_value=7)  # speaker_id
    db = MagicMock()
    db.execute = AsyncMock(return_value=user)
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=False)

    with patch("services.database.AsyncSessionLocal", return_value=db), \
         patch("services.conversation_handoff.try_handoff_context",
               new=AsyncMock(return_value=True)) as mock_handoff, \
         patch("services.conversation_handoff.emit_continued_handoff_frame",
               new=AsyncMock()) as mock_emit:
        await on_presence_enter_room(user_id=10, satellite_id=3, room_name="Küche")

    mock_handoff.assert_awaited_once()
    mock_emit.assert_awaited_once_with("Küche")

    # And NOT emitted when the handoff is a no-op (debounced / no source).
    with patch("services.database.AsyncSessionLocal", return_value=db), \
         patch("services.conversation_handoff.try_handoff_context",
               new=AsyncMock(return_value=False)), \
         patch("services.conversation_handoff.emit_continued_handoff_frame",
               new=AsyncMock()) as mock_emit2:
        await on_presence_enter_room(user_id=10, satellite_id=3, room_name="Küche")

    mock_emit2.assert_not_awaited()


@pytest.mark.unit
async def test_on_presence_enter_room_session_import_resolves():
    """Regression: on_presence_enter_room imported AsyncSessionLocal from
    models.database (wrong → ImportError on EVERY room change, escaping the
    handler to run_hooks). The import sits above the satellite_id guard, so a
    call with no satellite_id still executes it — returning without raising
    proves it now resolves from services.database."""
    from services.conversation_handoff import on_presence_enter_room
    # no satellite_id → early return, but only AFTER the import line has run
    await on_presence_enter_room(user_id=2, room_name="Arbeitszimmer")
