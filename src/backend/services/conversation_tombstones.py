"""A deleted conversation's session id is not handed out again (#1485, #1529).

The browser mints the session id and keeps it in ``localStorage``. When the
conversation is deleted somewhere else -- another tab, another device, the
admin cleanup, an atom purge -- that browser still holds the id, and its next
message used to recreate the conversation under it. The deleted id then
carried new content, and everything that outlives a delete and keys on the id
(KG provenance in ``source_session_id``, log lines) was attributed to it.

The fix sits at the chat BOUNDARY, where a client-named id is checked anyway
(``chat_handler._session_registerable_by``, ``routes/chat.py::
session_id_is_reusable``): an id with NO conversation row that was deleted
before is refused, and the existing ``session_replaced`` swap hands out a
fresh one. An id whose row exists is judged by the reach rule as before -- a
room history the satellite path recreated under its fixed id stays continuable.

The record is written by a database trigger, not by the delete call sites:
the atom purge removes conversations through an ``ON DELETE CASCADE`` that no
Python code sees, and a call-site list would miss the next path again. The
trigger DDL lives here once and is used by the migration
``pc20261106_conv_tombstones`` and the test harness.

An upload made under the deleted id AFTER the delete (the tab still showed
the conversation and attached a file to its next turn) follows that turn to the
fresh id (``carry_uploads_to_replacement``). Uploads from before the delete
stay where they were: moving them would revive part of the deleted
conversation, which is the defect itself.

The timestamp is UTC without a zone, like every ``_utcnow`` column, so it
compares with ``chat_uploads.created_at`` whatever the server's time zone is.

Retention (#1529). How long a record is kept is the operator's decision:
``CONVERSATION_TOMBSTONE_RETENTION_DAYS``. 0, the default, keeps every record
forever -- nothing is pruned and no query runs. A positive value N lets the
daily built-in task ``conversation_tombstone_cleanup`` remove records whose
``deleted_at`` is MORE than N days in the past; a record exactly N days old
stays, and a non-positive value never prunes anything
(``prune_expired_tombstones``).

Why a bounded window is safe: the record protects against exactly one thing, a
client that still holds the id of a conversation deleted elsewhere. Its next
message is refused, it is handed a fresh id and it drops the stale one, so the
record is needed only until every such client has spoken once. A pruned record
changes nothing for anybody else -- an id is random and nobody else names it.
What a window gives up: a client that stays silent for LONGER than N days and
then sends under the stale id recreates the conversation under it, which is
the behaviour before #1485 (no content of the deleted conversation returns;
the id carries new content). Choose N above the longest time a browser tab or
device is expected to sit unused. Two reasons to bound it at all: the table
otherwise grows with every delete, and a plugin transport may use an external
identifier as the session id (a chat-platform thread id), which would stay
here indefinitely after its user deleted the conversation.

The cut-off is computed by the DATABASE clock, the one the trigger wrote
``deleted_at`` with, so a skewed application clock cannot shorten the window.
A satellite's fixed room id gets its ``deleted_at`` refreshed on every delete
(``ON CONFLICT DO UPDATE``), so its record ages from the LAST delete.

The functions taking a connection take a SYNC one (Alembic hands one to a
migration; async callers use ``run_sync``).
"""
from __future__ import annotations

from sqlalchemy import delete, exists, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

TABLE = "deleted_conversation_sessions"
FUNCTION = "record_deleted_conversation_session"
TRIGGER = "trg_conversations_record_deleted_session"
#: The ORM-declared index on ``deleted_at`` (``index=True`` naming); the prune
#: is a range delete over it. Created by ``pc20261111_tombstone_idx``.
DELETED_AT_INDEX = "ix_deleted_conversation_sessions_deleted_at"
#: `now()` in UTC without a zone -- the shape `models.database._utcnow` writes.
UTC_NOW_SQL = "timezone('utc', now())"

_CREATE_FUNCTION = f"""
CREATE OR REPLACE FUNCTION {FUNCTION}() RETURNS trigger AS $$
BEGIN
    IF OLD.session_id IS NOT NULL THEN
        INSERT INTO {TABLE} (session_id, deleted_at)
        VALUES (OLD.session_id, {UTC_NOW_SQL})
        ON CONFLICT (session_id) DO UPDATE SET deleted_at = EXCLUDED.deleted_at;
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql
"""

_CREATE_TRIGGER = f"""
CREATE TRIGGER {TRIGGER}
AFTER DELETE ON conversations
FOR EACH ROW EXECUTE FUNCTION {FUNCTION}()
"""


def ensure_tombstone_trigger(conn) -> None:
    """Create the trigger function and the trigger; idempotent."""
    conn.execute(text(_CREATE_FUNCTION))
    conn.execute(text(f"DROP TRIGGER IF EXISTS {TRIGGER} ON conversations"))
    conn.execute(text(_CREATE_TRIGGER))


def drop_tombstone_trigger(conn) -> None:
    """Remove the trigger and its function; idempotent."""
    conn.execute(text(f"DROP TRIGGER IF EXISTS {TRIGGER} ON conversations"))
    conn.execute(text(f"DROP FUNCTION IF EXISTS {FUNCTION}()"))


async def is_deleted_session(db: AsyncSession, session_id: str) -> bool:
    """True when a conversation under this session id was deleted before."""
    from models.database import DeletedConversationSession

    result = await db.execute(
        select(DeletedConversationSession.session_id).where(
            DeletedConversationSession.session_id == session_id
        )
    )
    return result.first() is not None


async def carry_uploads_to_replacement(
    db: AsyncSession,
    deleted_session_id: str,
    new_session_id: str,
    uploader_id: int | None,
    *,
    auth_enabled: bool,
) -> int:
    """Move the uploads made under a deleted id since its delete to the new id.

    Only when ``deleted_session_id`` IS a deleted one with no conversation row
    -- for a foreign or ownerless session the replacement must not take any of
    its uploads along. Under auth only the caller's own uploads move. Returns
    the number of rows moved; commits.
    """
    from models.database import ChatUpload, Conversation, DeletedConversationSession

    tombstone = DeletedConversationSession
    conditions = [
        ChatUpload.session_id == deleted_session_id,
        exists().where(
            tombstone.session_id == deleted_session_id,
            ChatUpload.created_at >= tombstone.deleted_at,
        ),
        ~exists().where(Conversation.session_id == deleted_session_id),
    ]
    if auth_enabled:
        if uploader_id is None:
            return 0
        conditions.append(ChatUpload.uploaded_by_user_id == uploader_id)
    result = await db.execute(
        update(ChatUpload)
        .where(*conditions)
        .values(session_id=new_session_id)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return result.rowcount or 0


async def prune_expired_tombstones(db: AsyncSession, retention_days: int) -> int:
    """Delete the records deleted MORE than ``retention_days`` days ago.

    Returns the number of rows removed; commits. ``retention_days <= 0`` means
    keep forever: no statement is sent and 0 is returned -- a negative value
    must never be read as "everything is expired". A record exactly
    ``retention_days`` old is kept (strict ``<``), so an error at the boundary
    keeps a record a little longer instead of dropping it early.

    Whether the conversation was recreated in the meantime does not matter: a
    record only has an effect while NO row exists under its id, and a later
    delete of the recreated row writes a fresh one.
    """
    if retention_days <= 0:
        return 0
    from models.database import DeletedConversationSession

    # Database clock, in the shape the trigger writes: UTC without a zone.
    cutoff = text(f"{UTC_NOW_SQL} - make_interval(days => :retention_days)").bindparams(
        retention_days=retention_days
    )
    result = await db.execute(
        delete(DeletedConversationSession)
        .where(DeletedConversationSession.deleted_at < cutoff)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return result.rowcount or 0
