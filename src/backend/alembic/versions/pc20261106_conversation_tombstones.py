"""deleted_conversation_sessions + trigger: a deleted session id is not reused (#1485).

The browser mints the chat session id. A conversation deleted elsewhere (other
tab, other device, admin cleanup, atom purge) was recreated under the same id
by the next message of a browser that still held it. This table records every
deleted id; the chat boundary refuses an id that has no row but is recorded
here and hands out a fresh one (``services/conversation_tombstones.py``).

A trigger fills it, so the FK cascade of an atom purge counts as well.

No backfill: ids deleted before this revision are not recorded anywhere.

Revision ID: pc20261106_conv_tombstones
Revises: pc20261101_upload_uploader
Create Date: 2026-11-06
"""
import sqlalchemy as sa
from alembic import op

from services.conversation_tombstones import (
    TABLE,
    UTC_NOW_SQL,
    drop_tombstone_trigger,
    ensure_tombstone_trigger,
)

revision: str = "pc20261106_conv_tombstones"
down_revision: str | None = "pc20261101_upload_uploader"
branch_labels: None = None
depends_on: None = None


def _has_table() -> bool:
    """Development checkouts run ``create_all`` and may already have it.

    Qualified with current_schema(): the inspector's default schema is the one
    the connection STARTED with, and a bare to_regclass() would also find a
    table further down the search_path -- either would skip the create in a
    schema that does not have it.
    """
    return bool(op.get_bind().execute(sa.text(
        "SELECT to_regclass(quote_ident(current_schema()) || '.' || :t) IS NOT NULL"
    ), {"t": TABLE}).scalar())


def upgrade() -> None:
    if not _has_table():
        op.create_table(
            TABLE,
            sa.Column("session_id", sa.String(), primary_key=True),
            sa.Column(
                "deleted_at", sa.DateTime(), nullable=False,
                server_default=sa.text(UTC_NOW_SQL),
            ),
        )
    ensure_tombstone_trigger(op.get_bind())


def downgrade() -> None:
    # Trigger first: it writes into the table.
    drop_tombstone_trigger(op.get_bind())
    if _has_table():
        op.drop_table(TABLE)
