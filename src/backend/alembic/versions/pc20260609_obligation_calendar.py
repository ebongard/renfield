"""obligation calendar sync (per-user pref + event ledger)

Revision ID: pc20260609_oblig_cal
Revises: pc20260608_fact_tier_ov
Create Date: 2026-06-06

Two tables for the obligation→calendar reconciler (see models.database):
- ``obligation_calendar_pref``: per-user opt-in (which calendar to mirror into).
- ``obligation_calendar_events``: sync ledger (fact → calendar event_id). The FK
  to document_facts is ON DELETE SET NULL (not cascade) so a purged fact leaves
  an orphan row carrying the event_id, which the next reconcile deletes from the
  calendar and removes.
"""
from alembic import op
import sqlalchemy as sa


revision = "pc20260609_oblig_cal"
down_revision = "pc20260608_fact_tier_ov"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "obligation_calendar_pref",
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("calendar_name", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_table(
        "obligation_calendar_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "document_fact_id",
            sa.Integer,
            sa.ForeignKey("document_facts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("calendar", sa.String(64), nullable=False),
        sa.Column("event_id", sa.String(256), nullable=False),
        sa.Column("synced_obligation_date", sa.Date, nullable=True),
        sa.Column("synced_summary", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
        sa.UniqueConstraint("document_fact_id", "user_id", name="uq_obligation_calevent_fact_user"),
    )
    op.create_index(
        "ix_obligation_calendar_events_document_fact_id",
        "obligation_calendar_events", ["document_fact_id"],
    )
    op.create_index(
        "ix_obligation_calendar_events_user_id",
        "obligation_calendar_events", ["user_id"],
    )
    op.create_index(
        "idx_obligation_calevent_user", "obligation_calendar_events", ["user_id"]
    )


def downgrade() -> None:
    op.drop_index("idx_obligation_calevent_user", table_name="obligation_calendar_events")
    op.drop_index("ix_obligation_calendar_events_user_id", table_name="obligation_calendar_events")
    op.drop_index("ix_obligation_calendar_events_document_fact_id", table_name="obligation_calendar_events")
    op.drop_table("obligation_calendar_events")
    op.drop_table("obligation_calendar_pref")
