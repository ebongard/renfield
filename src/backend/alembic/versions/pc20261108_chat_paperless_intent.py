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


def upgrade() -> None:
    op.add_column("chat_uploads", sa.Column("paperless_intent", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_uploads", "paperless_intent")
