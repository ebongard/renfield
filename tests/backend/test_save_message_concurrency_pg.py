"""Concurrent appends into one conversation must chain, not fork.

Review finding, 2026-09-14: a background scan's outcome is written into the
requesting conversation while that conversation's chat turn may be saving its own
messages. `save_message` reads `active_leaf_message_id` and writes a new leaf;
without a row lock both writers chain onto the SAME parent, and one message ends
up on a sibling branch the active path never shows. Real Postgres only — SQLite
has no row locks, so this race cannot be reproduced there.
"""
import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

pytestmark = [pytest.mark.postgres, pytest.mark.database]


async def test_concurrent_appends_form_one_linear_chain(pg_async_engine):
    from models.database import Conversation, Message
    from services.conversation_service import ConversationService

    make_session = async_sessionmaker(pg_async_engine, expire_on_commit=False)

    async with make_session() as db:
        first = await ConversationService(db).save_message("race-1", "user", "scanne das")

    async def append(content: str):
        async with make_session() as db:
            return await ConversationService(db).save_message("race-1", "assistant", content)

    for _ in range(5):  # several rounds: the unlocked version forks almost every time
        await asyncio.gather(append("turn answer"), append("scan outcome"))

    async with make_session() as db:
        conversation = (await db.execute(
            select(Conversation).where(Conversation.session_id == "race-1")
        )).scalar_one()
        messages = (await db.execute(
            select(Message).where(Message.conversation_id == conversation.id)
        )).scalars().all()

    parents = [m.parent_message_id for m in messages if m.id != first.id]
    assert len(parents) == len(set(parents)), "two messages share a parent — the chain forked"

    # Walk from the leaf: every message must be on the active path.
    by_id = {m.id: m for m in messages}
    walked, cursor = 0, conversation.active_leaf_message_id
    while cursor is not None:
        walked += 1
        cursor = by_id[cursor].parent_message_id
    assert walked == len(messages)
