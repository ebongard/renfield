"""meetings — §2 meeting transcription (lean table)

Revision ID: pc20260714_meetings
Revises: pc20260713_projects
Create Date: 2026-07-14

The lean §2 ``meetings`` table (docs/design/meeting-transcription.md): an
uploaded multi-speaker recording becomes a ``pending`` row processed by a
dedicated Redis-Streams worker into a speaker-attributed transcript Document.
Gated by ``settings.meeting_transcription_enabled``; both instances leave it
off. NO ``project_id`` / minutes fields yet (additive later phases).

Idempotency: every DDL op is guarded — Base.metadata.create_all (backend boot)
creates this table for a new model, so re-running this migration must be a
no-op (same pattern as pc20260713_projects). Fully transactional
(transaction_per_migration).
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "pc20260714_meetings"
down_revision = "pc20260713_projects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # cross-dialect JSONB on Postgres, JSON on the sqlite test shim.
    json_type = (
        postgresql.JSONB(astext_type=sa.Text())
        if bind.dialect.name == "postgresql"
        else sa.JSON()
    )

    if "meetings" not in existing_tables:
        op.create_table(
            "meetings",
            sa.Column("id", sa.Integer(), primary_key=True),
            # SET NULL so deleting a user demotes the meeting to unowned rather
            # than blocking the delete (mirrors projects.owner_id semantics).
            sa.Column(
                "owner_user_id", sa.Integer(),
                sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
            ),
            sa.Column("circle_tier", sa.Integer(), nullable=False, server_default="2"),
            sa.Column("title", sa.String(255), nullable=True),
            sa.Column("date", sa.Date(), nullable=True),
            sa.Column("status", sa.String(50), nullable=False, server_default="pending"),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
            sa.Column("segments", json_type, nullable=True),
            # SET NULL so dropping the transcript Document out of band never
            # blocks; the meeting keeps a stable id across re-attribution.
            sa.Column(
                "transcript_document_id", sa.Integer(),
                sa.ForeignKey("documents.id", ondelete="SET NULL"), nullable=True,
            ),
            sa.Column("consent_confirmed", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column("consent_note", sa.Text(), nullable=True),
            sa.Column("retention_until", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )

    def _has_idx(idx_name: str) -> bool:
        if "meetings" not in existing_tables:
            return False
        return idx_name in {ix["name"] for ix in inspector.get_indexes("meetings")}

    if not _has_idx("ix_meetings_owner_user_id"):
        op.create_index("ix_meetings_owner_user_id", "meetings", ["owner_user_id"])
    if not _has_idx("ix_meetings_status"):
        op.create_index("ix_meetings_status", "meetings", ["status"])
    if not _has_idx("ix_meetings_transcript_document_id"):
        op.create_index("ix_meetings_transcript_document_id", "meetings", ["transcript_document_id"])


def downgrade() -> None:
    op.drop_index("ix_meetings_transcript_document_id", table_name="meetings")
    op.drop_index("ix_meetings_status", table_name="meetings")
    op.drop_index("ix_meetings_owner_user_id", table_name="meetings")
    op.drop_table("meetings")
