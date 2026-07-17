"""document.source — ingest provenance (§2 D14 Schicht-A gate)

Revision ID: pc20260714b_document_source
Revises: pc20260714_meetings
Create Date: 2026-07-14

Adds a nullable ``documents.source`` column. NULL for every existing/normal
document; "meeting_transcript" marks a §2 transcript so the Schicht-A
extraction hook skips it (no phantom obligations from meeting small talk).

Idempotency: guarded by an inspector check — Base.metadata.create_all (backend
boot) already adds this column for the model, so re-running is a no-op.
Fully transactional (transaction_per_migration).
"""
import sqlalchemy as sa
from alembic import op

revision = "pc20260714b_document_source"
down_revision = "pc20260714_meetings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("documents")}
    if "source" not in cols:
        op.add_column("documents", sa.Column("source", sa.String(30), nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "source")
