"""documents.paperless_document_id — persist the filed Paperless document id

Revision ID: pc20260613_pl_docid
Revises: pc20260612_pl_state
Create Date: 2026-06-08

The folder-ingest Paperless leg already RECEIVES the resulting Paperless
document id from ``await_consume_result`` but used to drop it (log-only). Persist
it so (a) a later metadata re-tag / backfill can address the Paperless document
directly instead of fuzzy-matching by filename, and (b) future features can
deep-link our Document to its Paperless record.

Additive + nullable + unindexed (read only after locating our row by id/hash).
NULL = never filed, or filed before this migration (those are recoverable via
the filename+date match path in bin/backfill_paperless_metadata.py). Fully
transactional.
"""
from alembic import op
import sqlalchemy as sa


revision = "pc20260613_pl_docid"
down_revision = "pc20260612_pl_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("paperless_document_id", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("documents", "paperless_document_id")
