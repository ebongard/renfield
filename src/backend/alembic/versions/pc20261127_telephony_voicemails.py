"""telephony_voicemails: one transcript job per voicemail (#1669).

docs/design/voicemail-transcript.md §6. New table, no backfill. The transcript
itself is not stored here, only the job state.

Revision ID: pc20261127_telephony_voicemails
Revises: pc20261126b_merge_heads
Create Date: 2026-11-27
"""
import sqlalchemy as sa
from alembic import op

revision: str = "pc20261127_telephony_voicemails"
down_revision: str | None = "pc20261126b_merge_heads"
branch_labels: None = None
depends_on: None = None

TABLE = "telephony_voicemails"


def _has_table(name: str) -> bool:
    """A fresh install's baseline runs ``create_all`` and already has the table."""
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table(TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("call_id", sa.String(64), nullable=False),
        sa.Column("recording", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime, nullable=False, server_default=sa.text("now()")),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("call_id", name="telephony_voicemails_call_id_key"),
    )
    op.create_index("ix_telephony_voicemails_due", TABLE, ["status", "next_attempt_at"])


def downgrade() -> None:
    op.drop_table(TABLE)
