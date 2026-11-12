"""chat_uploads.paperless_intent -- a requested filing for the split-aware route.

A chat attachment's Paperless filing can arrive before its KB document exists
(upload and request in one message). The intent is recorded on the upload and
moved onto the document by ``services.chat_paperless_split.settle_intent``.
Nullable, no backfill: NULL means "never requested via that route".

Revision ID: pc20261108_chat_pl_intent
Revises: pc20261107_chat_fk_rules
Create Date: 2026-11-08
"""
import sqlalchemy as sa
from alembic import op

revision: str = "pc20261108_chat_pl_intent"
down_revision: str | None = "pc20261107_chat_fk_rules"
branch_labels: None = None
depends_on: None = None


TABLE = "chat_uploads"
COLUMN = "paperless_intent"


def _has_column() -> bool:
    """A fresh install already has it: the baseline builds the tables from the
    current models, and this migration then runs on top of that."""
    inspector = sa.inspect(op.get_bind())
    if TABLE not in inspector.get_table_names():
        return False
    return COLUMN in {c["name"] for c in inspector.get_columns(TABLE)}


def upgrade() -> None:
    if _has_column():
        return
    op.add_column(TABLE, sa.Column(COLUMN, sa.String(16), nullable=True))


def downgrade() -> None:
    if not _has_column():
        return
    op.drop_column(TABLE, COLUMN)
