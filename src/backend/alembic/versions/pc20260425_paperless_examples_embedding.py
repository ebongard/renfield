"""Paperless extraction examples — embedding column + per-user scoping

Revision ID: pc20260425_paperless_examples_embedding
Revises: pc20260424_paperless_metadata_tables
Create Date: 2026-04-25

Design rationale: docs/design/paperless-llm-metadata.md (PR 3 — prompt
augmentation from corrections).

PR 2 ships the table; PR 3 turns on consumption. The retriever fetches
the top-N most similar past confirm-diffs by document similarity and
prepends them to future extraction prompts as in-context learning
examples.

Two columns are added here:

1. ``doc_text_embedding`` — vector for cosine-similarity retrieval.
   A dedicated column with a vector index keeps the retrieval hot-path
   single-table: no joins, no cross-service round trips. Dim 2560
   matches the production embedding stack (qwen3-embedding:4b, locked
   by cce1984705df). HNSW via halfvec cast clears the 2000-dim
   regular-vector index ceiling.

2. ``user_id`` — privacy guard. Without this column every household
   user's corrections flow into every other user's extraction prompt,
   which is the same leak pattern ConversationMemoryService had before
   Circles v1. Owner-only scoping is the conservative default;
   household-tier relaxation lives with the broader Circles
   integration (deferred).
"""
import sqlalchemy as sa
from alembic import op

try:
    from pgvector.sqlalchemy import Vector
    PGVECTOR_AVAILABLE = True
except ImportError:
    PGVECTOR_AVAILABLE = False
    Vector = None


# revision identifiers
revision = "pc20260425_paperless_examples_embedding"
down_revision = "pc20260424_paperless_metadata_tables"
branch_labels = None
depends_on = None


_EMBEDDING_DIM = 2560


def upgrade() -> None:
    # Embedding column. Nullable: rows written before PR 3 (none in
    # production yet, but defensive) carry NULL; the retriever ignores
    # rows with NULL embeddings.
    if PGVECTOR_AVAILABLE:
        op.add_column(
            "paperless_extraction_examples",
            sa.Column("doc_text_embedding", Vector(_EMBEDDING_DIM), nullable=True),
        )
    else:
        # Test/dev fallback if pgvector is missing — store as text. The
        # retriever's pgvector ops will not work in this mode, which is
        # acceptable for non-production setups.
        op.add_column(
            "paperless_extraction_examples",
            sa.Column("doc_text_embedding", sa.Text(), nullable=True),
        )

    # user_id — owner-only retrieval scoping. Nullable to accommodate
    # the AUTH_ENABLED=false single-user path (same rule as
    # paperless_pending_confirms). FK with ON DELETE CASCADE so deleting
    # a user purges their correction corpus.
    op.add_column(
        "paperless_extraction_examples",
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
    )

    # HNSW index via halfvec cast (same trick as cce1984705df) — regular
    # vector indexes hit the 2000-dim ceiling at 2560. m=16 / ef=64 is
    # the same tuning every other vector index in this codebase uses.
    if PGVECTOR_AVAILABLE:
        op.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_paperless_examples_embedding_hnsw
            ON paperless_extraction_examples
            USING hnsw ((doc_text_embedding::halfvec({_EMBEDDING_DIM})) halfvec_cosine_ops)
            WITH (m = 16, ef_construction = 64)
        """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_paperless_examples_embedding_hnsw")
    op.drop_column("paperless_extraction_examples", "user_id")
    op.drop_column("paperless_extraction_examples", "doc_text_embedding")
