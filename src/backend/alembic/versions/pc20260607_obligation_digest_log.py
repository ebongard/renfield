"""obligation_digest_log (weekly obligation digest sent-marker)

Revision ID: pc20260607_oblig_digest
Revises: pc20260606_oblig_acks
Create Date: 2026-06-06

Per-(user, ISO-week) dedup marker for the weekly obligation digest (the safety
floor under the per-milestone notifier). One row per ``(user_id, period_key)``
(period_key = ISO ``YYYY-Www``) so a pod restart mid-week never re-sends. A
dedicated table rather than the ``notifications`` row because notifications carry
a ~24h TTL and are reaped by the cleanup scheduler — the digest marker must
outlive the notification. See models.database.ObligationDigestLog.
"""
from alembic import op
import sqlalchemy as sa


revision = "pc20260607_oblig_digest"
down_revision = "pc20260606_oblig_acks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "obligation_digest_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("period_key", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "period_key", name="uq_obligation_digest_user_period"),
    )
    op.create_index(
        "ix_obligation_digest_log_user_id", "obligation_digest_log", ["user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_obligation_digest_log_user_id", table_name="obligation_digest_log")
    op.drop_table("obligation_digest_log")
