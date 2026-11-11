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

What a record is for: it stops a client that still holds the id of a
conversation deleted elsewhere -- and a deferred writer that still carries it
(a scan outcome, a Paperless announcement, ``save_message``) -- from creating a
conversation under that id again. Once the stale client has spoken it is
handed a fresh id and drops the old one.

What survives a conversation delete, and why the prune has a guard: several
tables carry the session id WITHOUT a foreign key to ``conversations``, so
their rows outlive the conversation. Some of them are READ by session id, and
whoever owns the conversation row under an id is handed those rows:
``chat_uploads`` (the attachment and its extracted text; the owner of the row
may forward, e-mail and file it) and ``paperless_pending_confirms`` (the open
"file this?" question). An unfinalised ``paperless_pending_finalize`` row is a
writer that will still announce into the id. While the record exists none of
that is reachable, because no conversation can be created under the id. So a
record is NEVER pruned while such a row exists (``_prune_guards``) -- an
indexed upload is never cleaned up, and its record then stays for good.
Without the guard a pruned id would hand the deleted conversation's
attachments to whoever recreates it; in a shared conversation that can be a
different user than the one who deleted it.

What a pruned id still permits: the id is an unknown one again. A client that
stayed silent for LONGER than N days recreates a conversation under it, owned
by that client's user -- the behaviour before #1485, minus the guarded rows.
Rows of the deleted conversation in the tables of
``SESSION_KEYS_WITHOUT_SESSION_READER`` (memory and KG provenance, episodic
memories, reminders, a shadow log) keep the id and from then on share it with
the new conversation's rows; no reader selects them by session id, so nothing
is handed out, but attribution by id no longer tells the two conversations
apart. Short-lived Redis state that carries the id (a remembered scan job, 24 h)
is outside the database and not guarded: it expires before the smallest
window of one day does. Choose N above the longest time a browser tab or
device is expected to sit unused.

Why bound it at all: the table otherwise grows with every delete, and a plugin
transport may use an external identifier as the session id (a chat-platform
thread id), which would stay here indefinitely after its user deleted the
conversation.

A NEW table with a session id column must be one of three things: cascade-
deleted with its conversation, named in ``_prune_guards``, or entered in
``SESSION_KEYS_WITHOUT_SESSION_READER`` with the reason no reader hands its
rows to the holder of the id. ``test_conversation_tombstone_session_keys.py``
fails otherwise.

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

#: Session-keyed columns (``table.column``) whose rows survive a conversation
#: delete but are NOT guarded by the prune, each with the reason: no reader
#: selects them by session id, so recreating the id hands nothing out. An entry
#: here is a decision made on the code -- re-check it when a reader is added.
SESSION_KEYS_WITHOUT_SESSION_READER: dict[str, str] = {
    "conversation_memories.source_session_id":
        "provenance only; retrieval filters by user and circle, never by session",
    "kg_relations.source_session_id":
        "provenance only (also holds 'doc:N'); no reader filters by a chat session id",
    "episodic_memories.session_id":
        "written with the episode; retrieval is per user, never by session",
    "reminders.session_id":
        "written at creation; delivery goes to the reminder's user, never by session",
    "memory_v2_shadow_log.session_id":
        "write-only diagnostics log",
}


def _prune_guards() -> list[tuple]:
    """``(session id column, extra condition or None)`` per table whose rows
    keep a record alive: read by session id on behalf of whoever holds the
    conversation under it, or a writer that will still announce into it."""
    from models.database import (
        ChatUpload,
        PaperlessPendingConfirm,
        PaperlessPendingFinalize,
    )

    return [
        # The attachment itself: `_get_owned_upload`, `_caller_owns_upload`,
        # the session fallback of the forward tools, `_fetch_document_context`.
        (ChatUpload.session_id, None),
        # The open confirm question: `_pending_paperless_confirm`, the commit tool.
        (PaperlessPendingConfirm.session_id, None),
        # A filing still in flight announces its outcome into the session.
        (PaperlessPendingFinalize.session_id, PaperlessPendingFinalize.finalized_at.is_(None)),
    ]

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
    """Delete the records deleted MORE than ``retention_days`` days ago --
    except those whose id still has surviving rows (``_prune_guards``).

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
    conditions = [DeletedConversationSession.deleted_at < cutoff]
    # A record outlives its window while a row of the deleted conversation
    # would be handed to whoever recreates the id (module docstring).
    for column, extra in _prune_guards():
        survivor = [column == DeletedConversationSession.session_id]
        if extra is not None:
            survivor.append(extra)
        conditions.append(~exists().where(*survivor))
    result = await db.execute(
        delete(DeletedConversationSession)
        .where(*conditions)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return result.rowcount or 0
