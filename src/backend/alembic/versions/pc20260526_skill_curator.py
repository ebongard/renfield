"""Skill curator audit column — self-learning Phase 4.

Revision ID: pc20260526_curator
Revises: pc20260525_tool_stats
Create Date: 2026-05-26 09:00:00.000000

Adds ``procedural_skills.merged_into_id`` so the curator job can mark
the loser of a duplicate-merge with a pointer to the surviving skill.
The loser stays in the table (is_active=False) for audit; the FK is
ON DELETE SET NULL so a later hard-delete of the winner doesn't cascade
and lose the historical record.

This is the only schema change for Phase 4 — the curator job is pure
service code, no new tables.
"""
from alembic import op
import sqlalchemy as sa


revision = "pc20260526_curator"
down_revision = "pc20260525_tool_stats"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "procedural_skills",
        sa.Column(
            "merged_into_id",
            sa.Integer,
            sa.ForeignKey("procedural_skills.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "idx_procedural_skills_merged_into",
        "procedural_skills",
        ["merged_into_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_procedural_skills_merged_into",
        table_name="procedural_skills",
    )
    op.drop_column("procedural_skills", "merged_into_id")
