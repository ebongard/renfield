"""Tool outcome stats table — self-learning Phase 3.

Revision ID: pc20260525_tool_stats
Revises: pc20260524_trajectories
Create Date: 2026-05-25 11:00:00.000000

Adds the ``tool_outcome_stats`` table. Every ``tool_result`` step in
the agent loop bumps the per-(user, tool) counter via
``services.tool_outcome_service``. At prompt-build time the agent reads
the asker's stats and injects a ``{tool_health_warnings}`` block when
a tool's rolling success rate has dropped below the configured
threshold — keeps the LLM from confidently picking a tool that's been
broken in production.
"""
from alembic import op
import sqlalchemy as sa


revision = "pc20260525_tool_stats"
down_revision = "pc20260524_trajectories"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tool_outcome_stats",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("tool_name", sa.String(128), nullable=False),
        sa.Column("success_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("failure_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_used_at", sa.DateTime, nullable=True),
        sa.Column("last_failure_at", sa.DateTime, nullable=True),
        sa.Column("last_failure_summary", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "tool_name", name="uq_tool_outcome_user_tool"),
    )
    op.create_index(
        "idx_tool_outcome_user_tool", "tool_outcome_stats",
        ["user_id", "tool_name"],
    )


def downgrade() -> None:
    op.drop_index("idx_tool_outcome_user_tool", table_name="tool_outcome_stats")
    op.drop_table("tool_outcome_stats")
