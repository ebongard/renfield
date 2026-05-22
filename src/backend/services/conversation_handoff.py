"""
Conversation Handoff — transfers context when a speaker moves between satellites.

When a recognized speaker starts talking at a new satellite, this module copies
conversation context (context_vars, summary, recent messages) from their most
recent satellite conversation to the new session. This allows the LLM to
continue the conversation naturally across rooms.

The handoff function is called from two places:
1. satellite_handler.py (direct call BEFORE history loading — correct timing)
2. presence_enter_room hook (for any additional consumers)

Both calls are idempotent: if the target session already has data, the copy is skipped.
"""

import time
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Conversation, Message

# Debounce: track last handoff time per speaker to avoid rapid-fire copies
_last_handoff: dict[int, float] = {}
_DEBOUNCE_SECONDS = 10.0


async def try_handoff_context(
    speaker_id: int,
    target_session_id: str,
    db: AsyncSession,
    window_minutes: int = 30,
) -> bool:
    """Copy context from speaker's most recent satellite conversation to target session.

    Args:
        speaker_id: Speaker DB ID (from speaker recognition).
        target_session_id: Session ID of the new satellite conversation.
        db: Async database session.
        window_minutes: Only hand off if source conversation was active within this window.

    Returns:
        True if context was copied, False otherwise.
    """
    now = time.time()

    # Debounce: skip if we just did a handoff for this speaker
    last = _last_handoff.get(speaker_id, 0.0)
    if now - last < _DEBOUNCE_SECONDS:
        logger.debug(f"Handoff debounce: speaker {speaker_id} (last {now - last:.1f}s ago)")
        return False

    try:
        # Find source: most recent satellite conversation for this speaker (excluding target)
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=window_minutes)
        result = await db.execute(
            select(Conversation)
            .where(
                Conversation.speaker_id == speaker_id,
                Conversation.session_id.like("satellite-%"),
                Conversation.session_id != target_session_id,
                Conversation.updated_at >= cutoff,
            )
            .order_by(Conversation.updated_at.desc())
            .limit(1)
        )
        source = result.scalar_one_or_none()

        if not source:
            return False

        # Check if target already exists and is more recent
        target_result = await db.execute(
            select(Conversation).where(Conversation.session_id == target_session_id)
        )
        target = target_result.scalar_one_or_none()

        if target and target.updated_at and source.updated_at:
            if target.updated_at >= source.updated_at:
                logger.debug(f"Handoff skip: target {target_session_id} already more recent")
                return False

        # Create or update target conversation with source context
        if not target:
            target = Conversation(
                session_id=target_session_id,
                speaker_id=speaker_id,
                user_id=source.user_id,
                context_vars=source.context_vars,
                summary=source.summary,
            )
            db.add(target)
        else:
            if source.context_vars:
                target.context_vars = source.context_vars
            if source.summary:
                target.summary = source.summary
            if not target.speaker_id:
                target.speaker_id = speaker_id
            if source.user_id and not target.user_id:
                target.user_id = source.user_id

        # If summary is NULL, copy last 5 messages as seed context
        if not source.summary:
            msg_result = await db.execute(
                select(Message)
                .where(Message.conversation_id == source.id)
                .order_by(Message.timestamp.desc())
                .limit(5)
            )
            seed_messages = list(reversed(msg_result.scalars().all()))

            await db.flush()  # ensure target.id is available

            for msg in seed_messages:
                new_msg = Message(
                    conversation_id=target.id,
                    role=msg.role,
                    content=msg.content,
                    message_metadata={**(msg.message_metadata or {}), "handoff_source": source.session_id},
                )
                db.add(new_msg)

        await db.commit()
        _last_handoff[speaker_id] = now
        logger.info(
            f"Conversation handoff: speaker {speaker_id}, "
            f"{source.session_id} → {target_session_id}"
        )
        return True

    except Exception as e:
        logger.warning(f"Conversation handoff failed: {e}")
        await db.rollback()
        return False


async def on_presence_enter_room(**kwargs) -> None:
    """Hook listener for presence_enter_room — triggers handoff when speaker moves rooms."""
    from datetime import date

    from models.database import AsyncSessionLocal

    user_id = kwargs.get("user_id")
    satellite_id = kwargs.get("satellite_id")

    if not user_id or not satellite_id:
        return

    # Look up speaker_id from user_id
    try:
        from models.database import User

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(User.speaker_id).where(User.id == user_id)
            )
            speaker_id = result.scalar_one_or_none()

            if not speaker_id:
                return

            target_session_id = f"satellite-{satellite_id}-{date.today().isoformat()}"
            await try_handoff_context(speaker_id, target_session_id, db)
    except Exception as e:
        logger.warning(f"Handoff hook failed: {e}")
