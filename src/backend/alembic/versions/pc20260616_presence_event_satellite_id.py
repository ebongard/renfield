"""presence_events.satellite_id + history index — persistent presence history

Revision ID: pc20260616_sat_id
Revises: pc20260614_email_ingest
Create Date: 2026-06-14

Persistent presence history is mostly ADDITIVE on top of the already-persisted
``presence_events`` table (written on every room enter/leave by
``presence_analytics._persist_event``). This migration adds:

  * ``satellite_id`` (nullable String(100)) — which satellite produced the
    detection on an ``enter`` event (NULL for ``leave``/voice/web events).
  * ``ix_presence_events_history`` (user_id, created_at) — backs the timeline
    + last-seen queries that read a single user's events chronologically (the
    existing ``ix_presence_events_analytics`` leads with room_id and is a poor
    match for that access pattern).

A nullable ADD COLUMN is metadata-only on Postgres and the index build is
ordinary DDL, so this migration is fully transactional.
"""
from alembic import op
import sqlalchemy as sa


revision = "pc20260616_sat_id"
down_revision = "pc20260614_email_ingest"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "presence_events",
        sa.Column("satellite_id", sa.String(100), nullable=True),
    )
    op.create_index(
        "ix_presence_events_history",
        "presence_events",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_presence_events_history", table_name="presence_events")
    op.drop_column("presence_events", "satellite_id")
