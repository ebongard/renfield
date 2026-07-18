"""meeting minutes — §2 Phase 3 (summary / decisions / action-items)

Revision ID: pc20260718_meeting_minutes
Revises: pc20260714b_document_source
Create Date: 2026-07-18

Adds the additive minutes columns to ``meetings``: a ``minutes`` JSONB blob
({summary, decisions[], action_items[]}) plus a ``minutes_status`` gate
(none -> draft -> confirmed) and generated/confirmed timestamps. NULL/'none'
for every existing meeting, so this is behaviourally a no-op until Phase 3 is
used.

Idempotency: inspector-guarded per column — Base.metadata.create_all (backend
boot) already adds these for the model, so re-running is a no-op. Fully
transactional (transaction_per_migration).
"""
import sqlalchemy as sa
from alembic import op

revision = "pc20260718_meeting_minutes"
down_revision = "pc20260714b_document_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("meetings")}
    dialect = bind.dialect.name
    json_type = sa.dialects.postgresql.JSONB() if dialect == "postgresql" else sa.JSON()

    if "minutes" not in cols:
        op.add_column("meetings", sa.Column("minutes", json_type, nullable=True))
    if "minutes_status" not in cols:
        op.add_column(
            "meetings",
            sa.Column(
                "minutes_status", sa.String(20), nullable=False, server_default="none"
            ),
        )
    if "minutes_generated_at" not in cols:
        op.add_column("meetings", sa.Column("minutes_generated_at", sa.DateTime(), nullable=True))
    if "minutes_confirmed_at" not in cols:
        op.add_column("meetings", sa.Column("minutes_confirmed_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("meetings", "minutes_confirmed_at")
    op.drop_column("meetings", "minutes_generated_at")
    op.drop_column("meetings", "minutes_status")
    op.drop_column("meetings", "minutes")
