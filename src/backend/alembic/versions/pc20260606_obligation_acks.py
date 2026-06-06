"""obligation_acknowledgements (deadline notifier ledger + agenda Bestätigt)

Revision ID: pc20260606_oblig_acks
Revises: pc20260604b_kgmp
Create Date: 2026-06-06

Adds ``obligation_acknowledgements`` — one table serving two row shapes
discriminated by ``milestone`` (see models.database.ObligationAcknowledgement):

  * notifier ledger — ``milestone`` ∈ {14d,7d,3d,1d,due,overdue}, ``user_id`` =
    the obligation OWNER. The obligation-deadline notifier inserts a row after
    firing a lead-time reminder so the daily scan never re-fires the same bucket
    (idempotent across pod restarts — the missed-deadline safety property).
  * user confirmation — ``milestone`` = ``"confirmed"``, ``user_id`` = whoever
    clicked Bestätigen in the agenda. Server home for the agenda's former
    per-device localStorage state; a confirmed ack also tells the notifier the
    obligation is handled.

Keyed on ``document_facts.id`` (always present) with ON DELETE CASCADE from both
parents. Unique ``(document_fact_id, user_id, milestone)`` backs the notifier's
insert-or-skip dedup and the confirm route's idempotency.
"""
from alembic import op
import sqlalchemy as sa


revision = "pc20260606_oblig_acks"
down_revision = "pc20260604b_kgmp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "obligation_acknowledgements",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "document_fact_id",
            sa.Integer,
            sa.ForeignKey("document_facts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("milestone", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "document_fact_id", "user_id", "milestone",
            name="uq_obligation_ack_fact_user_milestone",
        ),
    )
    op.create_index(
        "ix_obligation_acknowledgements_document_fact_id",
        "obligation_acknowledgements", ["document_fact_id"],
    )
    op.create_index(
        "ix_obligation_acknowledgements_user_id",
        "obligation_acknowledgements", ["user_id"],
    )
    op.create_index(
        "idx_obligation_ack_user_fact",
        "obligation_acknowledgements", ["user_id", "document_fact_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_obligation_ack_user_fact", table_name="obligation_acknowledgements")
    op.drop_index("ix_obligation_acknowledgements_user_id", table_name="obligation_acknowledgements")
    op.drop_index("ix_obligation_acknowledgements_document_fact_id", table_name="obligation_acknowledgements")
    op.drop_table("obligation_acknowledgements")
