"""Agent trajectories table — self-learning Phase 2 (training-data capture).

Revision ID: pc20260524_trajectories
Revises: pc20260523_skills
Create Date: 2026-05-24 09:30:00.000000

Adds the ``agent_trajectories`` table that captures the full
{user message, tool calls, tool results, final answer} trace of every
agent turn so the corpus can be exported as JSONL for downstream LoRA
fine-tuning. See ``services.trajectory_service`` for the producer and
``/api/trajectories`` for the export route.

Retention is bounded by ``settings.trajectory_retention_days``; the
cleanup scheduler in ``lifecycle.py`` deletes older rows unless
``flagged_for_retention=True`` (set by the post-turn task when the turn
produced an auto-extracted skill — those are kept as gold examples).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "pc20260524_trajectories"
down_revision = "pc20260523_skills"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name if bind is not None else "postgresql"
    json_type = postgresql.JSONB(astext_type=sa.Text()) if dialect == "postgresql" else sa.JSON

    op.create_table(
        "agent_trajectories",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "conversation_id",
            sa.Integer,
            sa.ForeignKey("conversations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("raw_payload", json_type, nullable=False),
        sa.Column("redacted_payload", json_type, nullable=True),
        sa.Column(
            "outcome",
            sa.String(20),
            nullable=False,
            server_default="success",
        ),
        sa.Column("tool_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("distinct_tool_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("token_count", sa.Integer, nullable=True),
        sa.Column(
            "extracted_skill_id",
            sa.Integer,
            sa.ForeignKey("procedural_skills.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("used_skill_ids", json_type, nullable=True),
        sa.Column(
            "flagged_for_retention",
            sa.Boolean,
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_index(
        "idx_trajectories_user_created",
        "agent_trajectories",
        ["user_id", "created_at"],
    )
    op.create_index(
        "idx_trajectories_outcome_created",
        "agent_trajectories",
        ["outcome", "created_at"],
    )
    op.create_index(
        "idx_trajectories_flagged",
        "agent_trajectories",
        ["flagged_for_retention"],
    )
    # Cleanup-job query: oldest non-flagged rows past retention.
    op.create_index(
        "idx_trajectories_created_at",
        "agent_trajectories",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_trajectories_created_at", table_name="agent_trajectories")
    op.drop_index("idx_trajectories_flagged", table_name="agent_trajectories")
    op.drop_index("idx_trajectories_outcome_created", table_name="agent_trajectories")
    op.drop_index("idx_trajectories_user_created", table_name="agent_trajectories")
    op.drop_table("agent_trajectories")
