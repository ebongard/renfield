"""Every session-keyed table is accounted for by the tombstone prune (#1529).

A tombstone may only expire when nothing of the deleted conversation is left
that a reader would hand to whoever recreates the id. Rows in tables that
carry a session id WITHOUT a cascading foreign key to ``conversations``
survive the delete. Each such column must therefore be exactly one of:

- cascade-deleted with its conversation (a foreign key to ``conversations``
  with ``ON DELETE CASCADE`` on that column);
- named in ``conversation_tombstones._prune_guards`` -- its rows keep the
  record alive;
- entered in ``SESSION_KEYS_WITHOUT_SESSION_READER`` with the reason no reader
  selects its rows by session id.

A new table with a session id column that is none of the three fails here,
before it can reopen #1485 through an expired record.

The detector is the column NAME (contains ``session``), over every table of
the ORM metadata, core and ha_glue. It cannot see a session id stored inside
a JSON column or under a name that does not say so; that stays a review
matter. Its negative control is below.
"""
from __future__ import annotations

import pytest
from sqlalchemy import Column, ForeignKey, Integer, MetaData, String, Table

import ha_glue.models.database  # noqa: F401 -- registers the ha_glue tables on Base
from models.database import Base
from services.conversation_tombstones import (
    SESSION_KEYS_WITHOUT_SESSION_READER,
    TABLE,
    _prune_guards,
)

pytestmark = [pytest.mark.backend, pytest.mark.unit]

#: The session id itself and its tombstone.
_THE_ID_ITSELF = {"conversations.session_id", f"{TABLE}.session_id"}

#: Columns whose name contains "session" but mean something other than a chat
#: session id, with the reason. Empty today.
_NOT_A_CHAT_SESSION_ID: dict[str, str] = {}


def _session_columns(metadata: MetaData) -> dict[str, Column]:
    return {
        f"{table.name}.{column.name}": column
        for table in metadata.tables.values()
        for column in table.columns
        if "session" in column.name.lower()
    }


def _cascades_with_the_conversation(column: Column) -> bool:
    return any(
        fk.column.table.name == "conversations" and (fk.ondelete or "").upper() == "CASCADE"
        for fk in column.foreign_keys
    )


def _guarded() -> set[str]:
    return {f"{column.table.name}.{column.name}" for column, _extra in _prune_guards()}


def _unaccounted(metadata: MetaData) -> list[str]:
    """Session-keyed columns that survive a delete and are neither guarded nor
    exempted with a reason."""
    accounted = (
        _THE_ID_ITSELF | set(_NOT_A_CHAT_SESSION_ID) | _guarded()
        | set(SESSION_KEYS_WITHOUT_SESSION_READER)
    )
    return sorted(
        key for key, column in _session_columns(metadata).items()
        if key not in accounted and not _cascades_with_the_conversation(column)
    )


def test_every_session_keyed_column_is_cascaded_guarded_or_exempted():
    missing = _unaccounted(Base.metadata)
    assert missing == [], (
        f"{missing}: rows keyed by a session id that survive the conversation's delete. "
        "Give the column an ON DELETE CASCADE foreign key to conversations, add it to "
        "conversation_tombstones._prune_guards (a reader hands its rows to the holder of "
        "the id), or enter it in SESSION_KEYS_WITHOUT_SESSION_READER with the reason."
    )


def test_the_detector_sees_the_known_columns():
    """Negative control for the scope: a detector that found nothing would
    pass the test above just as well."""
    found = set(_session_columns(Base.metadata))
    assert {
        "conversations.session_id",
        "deleted_conversation_sessions.session_id",
        "chat_uploads.session_id",
        "paperless_pending_confirms.session_id",
        "paperless_pending_finalize.session_id",
        "reminders.session_id",
        "conversation_memories.source_session_id",
        "episodic_memories.session_id",
        "kg_relations.source_session_id",
        "memory_v2_shadow_log.session_id",
    } <= found
    # Core and ha_glue are both in the walk.
    assert "rooms" in Base.metadata.tables or "room_output_devices" in Base.metadata.tables


def test_a_new_session_keyed_table_fails_and_a_cascading_one_does_not():
    metadata = MetaData()
    Table("conversations", metadata, Column("id", Integer, primary_key=True))
    Table(
        "new_feature_rows", metadata,
        Column("id", Integer, primary_key=True),
        Column("session_id", String),
    )
    Table(
        "new_provenance_rows", metadata,
        Column("id", Integer, primary_key=True),
        Column("origin_session_id", String),
    )
    Table(
        "new_child_rows", metadata,
        Column("id", Integer, primary_key=True),
        Column(
            "conversation_session_ref", Integer,
            ForeignKey("conversations.id", ondelete="CASCADE"),
        ),
    )
    Table(
        "new_detached_rows", metadata,
        Column("id", Integer, primary_key=True),
        Column("session_ref", Integer, ForeignKey("conversations.id", ondelete="SET NULL")),
    )

    assert _unaccounted(metadata) == [
        "new_detached_rows.session_ref",
        "new_feature_rows.session_id",
        "new_provenance_rows.origin_session_id",
    ]


def test_guards_and_exemptions_name_existing_columns_and_do_not_overlap():
    known = set(_session_columns(Base.metadata))
    guarded, exempt = _guarded(), set(SESSION_KEYS_WITHOUT_SESSION_READER)

    assert guarded <= known
    assert exempt <= known, f"stale exemption: {sorted(exempt - known)}"
    assert not guarded & exempt
    assert not (guarded | exempt) & _THE_ID_ITSELF
    assert all(reason.strip() for reason in SESSION_KEYS_WITHOUT_SESSION_READER.values())
    # A cascading column needs neither -- an entry for one would be dead.
    assert not [
        key for key in guarded | exempt
        if _cascades_with_the_conversation(_session_columns(Base.metadata)[key])
    ]


def test_the_upload_table_is_guarded():
    """The table #1529's review found: read by session id through four paths,
    indexed uploads never cleaned up."""
    assert "chat_uploads.session_id" in _guarded()
