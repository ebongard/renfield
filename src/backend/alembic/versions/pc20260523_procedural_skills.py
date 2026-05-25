"""Procedural skills table — self-learning Phase 1.

Revision ID: pc20260523_skills
Revises: q1r2s3t4u5v6
Create Date: 2026-05-23 14:00:00.000000

Adds the ``procedural_skills`` source table for the new ``procedural_skill``
atom type. Stores agent-learned how-to recipes (markdown body + trigger
examples + tool sequence + outcome counters) so the agent can recall and
reuse successful multi-step tool chains on similar future requests.

Schema follows the conversation_memories template (atoms-per-source pattern
established in pc20260420_circles_v1_schema):
  - id (integer PK)
  - user_id (nullable — NULL for system seed skills loaded from .md files)
  - title, body_md, trigger_examples, tool_sequence
  - source ("auto_extracted" | "seed" | "user_created")
  - learned_from_conversation_id (FK, nullable)
  - success_count, failure_count, last_used_at — outcome tracking
  - pinned (protect from curator consolidation)
  - is_active (soft delete)
  - embedding (pgvector, EMBEDDING_DIMENSION dims) for similarity retrieval
  - atom_id + circle_tier — circles v1 integration. atom_id is nullable
    because seed skills loaded from on-disk .md files bypass the atom
    registry (no per-user owner). Auto-extracted + user-created skills
    MUST go through AtomService.upsert_atom.
  - version (bumped on each curator patch / manual edit)

HNSW index on embedding matches the pattern used by conversation_memories
and episodic_memories.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "pc20260523_skills"
down_revision = "q1r2s3t4u5v6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Embedding dimension matches every other vector column in this DB.
    # Read from settings so a future bump to a larger embedding model
    # only needs the setting flip (same approach as the Lane-A retrieval
    # tables in pc20260420).
    from utils.config import settings

    embed_dim = settings.embedding_dimension

    bind = op.get_bind()
    dialect = bind.dialect.name if bind is not None else "postgresql"

    # Build the column list. On postgres we add the embedding column with
    # the real pgvector type via raw DDL after table creation; the sqlite
    # test harness gets a Text fallback so the schema is at least nominally
    # there (similarity queries never run on sqlite).
    op.create_table(
        "procedural_skills",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("body_md", sa.Text, nullable=False),
        sa.Column(
            "trigger_examples",
            postgresql.JSONB(astext_type=sa.Text())
            if dialect == "postgresql" else sa.JSON,
            nullable=False,
            server_default=(
                sa.text("'[]'::jsonb") if dialect == "postgresql"
                else sa.text("'[]'")
            ),
        ),
        sa.Column(
            "tool_sequence",
            postgresql.JSONB(astext_type=sa.Text())
            if dialect == "postgresql" else sa.JSON,
            nullable=False,
            server_default=(
                sa.text("'[]'::jsonb") if dialect == "postgresql"
                else sa.text("'[]'")
            ),
        ),
        sa.Column(
            "source",
            sa.String(20),
            nullable=False,
            server_default="auto_extracted",
        ),
        sa.Column(
            "learned_from_conversation_id",
            sa.Integer,
            sa.ForeignKey("conversations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("success_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("failure_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_used_at", sa.DateTime, nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("pinned", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column(
            "atom_id",
            sa.String(36),
            sa.ForeignKey("atoms.atom_id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "circle_tier",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
        ),
        # Sqlite test harness: store embedding as JSON-encoded text so the
        # column exists. Postgres production gets the real Vector below.
        sa.Column(
            "embedding",
            sa.Text if dialect != "postgresql" else sa.Text,
            nullable=True,
        ),
    )

    if dialect == "postgresql":
        # Swap the Text placeholder for the real pgvector column. Table is
        # empty so this is safe (no data conversion).
        op.execute("ALTER TABLE procedural_skills DROP COLUMN embedding")
        op.execute(
            f"ALTER TABLE procedural_skills ADD COLUMN embedding vector({embed_dim})"
        )
        # HNSW index via halfvec cast — same trick as cce1984705df /
        # episodic / paperless_examples / kb_performance_indexes. Regular
        # `vector` type has a 2000-dim hard limit for HNSW in pgvector
        # 0.8.x; production runs 2560-dim (qwen3-embedding:4b), so the
        # cast through halfvec is mandatory. See
        # memory/reference_pgvector_index_limits.md for the project rule.
        op.execute(
            f"CREATE INDEX idx_procedural_skills_embedding "
            f"ON procedural_skills "
            f"USING hnsw ((embedding::halfvec({embed_dim})) halfvec_cosine_ops) "
            f"WITH (m = 16, ef_construction = 64)"
        )

    # Frequent-access composite indexes.
    op.create_index(
        "idx_procedural_skills_active_user",
        "procedural_skills",
        ["is_active", "user_id"],
    )
    op.create_index(
        "idx_procedural_skills_tier_active",
        "procedural_skills",
        ["circle_tier", "is_active"],
    )
    op.create_index(
        "idx_procedural_skills_atom_id",
        "procedural_skills",
        ["atom_id"],
    )
    # Curator query: find candidates for review/consolidation by usage recency.
    op.create_index(
        "idx_procedural_skills_last_used",
        "procedural_skills",
        ["last_used_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_procedural_skills_last_used", table_name="procedural_skills")
    op.drop_index("idx_procedural_skills_atom_id", table_name="procedural_skills")
    op.drop_index("idx_procedural_skills_tier_active", table_name="procedural_skills")
    op.drop_index("idx_procedural_skills_active_user", table_name="procedural_skills")

    bind = op.get_bind()
    dialect = bind.dialect.name if bind is not None else "postgresql"
    if dialect == "postgresql":
        op.execute("DROP INDEX IF EXISTS idx_procedural_skills_embedding")

    op.drop_table("procedural_skills")
