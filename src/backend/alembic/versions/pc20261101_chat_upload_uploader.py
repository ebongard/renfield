"""chat_uploads: record who uploaded the file (#1459).

Until now a chat upload carried only ``session_id``; the owner of the
conversation was the only record of whose upload it was. In a shared room
thread (the kitchen device account owns the thread) a member who attached a
file therefore got 403 when filing it into their own KB or forwarding it by
mail, while the device account could.

``uploaded_by_user_id`` is set server-side from the authenticated caller at
upload time. Nullable and NOT backfilled: an existing row does not know who
uploaded it, and guessing (e.g. "the conversation owner") would pass ignorance
off as fact. NULL keeps today's owner-only rule.

``ON DELETE SET NULL``: deleting an account must not be blocked by a chat
attachment, and a row whose uploader is gone falls back to the owner-only rule
-- the narrower one.

Revision ID: pc20261101_upload_uploader
Revises: pc20261102_kb_share_can_delete
Create Date: 2026-11-01
"""
import sqlalchemy as sa
from alembic import op

revision: str = "pc20261101_upload_uploader"
down_revision: str | None = "pc20261102_kb_share_can_delete"
branch_labels: None = None
depends_on: None = None

TABLE = "chat_uploads"
COLUMN = "uploaded_by_user_id"
INDEX = "ix_chat_uploads_uploaded_by_user_id"


def _has_column() -> bool:
    """Development checkouts run ``create_all`` and may already have it."""
    inspector = sa.inspect(op.get_bind())
    if TABLE not in inspector.get_table_names():
        return False
    return COLUMN in {c["name"] for c in inspector.get_columns(TABLE)}


def upgrade() -> None:
    if _has_column():
        return
    # Inline foreign key: Postgres names it the way ``create_all`` does
    # (``chat_uploads_uploaded_by_user_id_fkey``), so both schemas match.
    op.add_column(TABLE, sa.Column(
        COLUMN, sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    ))
    op.create_index(INDEX, TABLE, [COLUMN])


def downgrade() -> None:
    # The way back loses the uploader of every row written since the upgrade;
    # it is recorded nowhere else. Those rows return to the owner-only rule,
    # which is exactly the pre-upgrade behaviour.
    if not _has_column():
        return
    op.drop_column(TABLE, COLUMN)  # takes its index and foreign key with it
