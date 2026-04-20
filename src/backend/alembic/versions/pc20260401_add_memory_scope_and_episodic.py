"""Add memory scope/source/confidence columns and episodic_memories table

Revision ID: pc20260401a1
Revises: pc20260331a1
Create Date: 2026-04-01

Idempotent on UPGRADE: each op checks the live schema first. Some early
deployments materialised parts of this schema via Base.metadata.create_all()
before the migration shipped (notably the episodic_memories table on the
.159 test box). Fresh-DB upgrades still hit every branch.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "pc20260401a1"
down_revision = "pc20260331a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # --- ConversationMemory: add provenance, scoping, confidence columns ---
    existing_cols = {col["name"] for col in inspector.get_columns("conversation_memories")}
    new_cols = [
        ("source", sa.String(20), {"nullable": False, "server_default": "llm_inferred"}),
        ("scope", sa.String(10), {"nullable": False, "server_default": "user"}),
        ("team_id", sa.String(100), {"nullable": True}),
        ("confidence", sa.Float(), {"nullable": False, "server_default": "1.0"}),
        ("trigger_pattern", sa.String(255), {"nullable": True}),
    ]
    for name, type_, kwargs in new_cols:
        if name not in existing_cols:
            op.add_column("conversation_memories", sa.Column(name, type_, **kwargs))

    # --- EpisodicMemory: create table only if missing ---
    if "episodic_memories" not in inspector.get_table_names():
        op.create_table(
            "episodic_memories",
            sa.Column("id", sa.Integer(), primary_key=True, index=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True, index=True),
            sa.Column("session_id", sa.String(255), nullable=True, index=True),
            sa.Column("summary", sa.Text(), nullable=False),
            sa.Column("topic", sa.String(50), nullable=True, index=True),
            sa.Column("entities", sa.JSON(), nullable=True),
            sa.Column("tools_used", sa.JSON(), nullable=True),
            sa.Column("outcome", sa.String(20), nullable=True),
            sa.Column("embedding", sa.Text(), nullable=True),  # Vector type added by pgvector if available
            sa.Column("importance", sa.Float(), default=0.5),
            sa.Column("access_count", sa.Integer(), default=0),
            sa.Column("last_accessed_at", sa.DateTime(), nullable=True),
            sa.Column("is_active", sa.Boolean(), default=True, index=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )

    existing_indexes = {ix["name"] for ix in inspector.get_indexes("episodic_memories")}
    if "ix_episodic_user_active" not in existing_indexes:
        op.create_index("ix_episodic_user_active", "episodic_memories", ["user_id", "is_active"])
    if "ix_episodic_user_topic" not in existing_indexes:
        op.create_index("ix_episodic_user_topic", "episodic_memories", ["user_id", "topic"])


def downgrade() -> None:
    # --- EpisodicMemory: drop table ---
    op.drop_index("ix_episodic_user_topic", table_name="episodic_memories")
    op.drop_index("ix_episodic_user_active", table_name="episodic_memories")
    op.drop_table("episodic_memories")

    # --- ConversationMemory: drop added columns ---
    op.drop_column("conversation_memories", "trigger_pattern")
    op.drop_column("conversation_memories", "confidence")
    op.drop_column("conversation_memories", "team_id")
    op.drop_column("conversation_memories", "scope")
    op.drop_column("conversation_memories", "source")
