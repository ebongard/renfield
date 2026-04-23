"""Paperless extraction examples — add doc_text embedding for retrieval

Revision ID: pc20260425_paperless_examples_embedding
Revises: pc20260424_paperless_metadata_tables
Create Date: 2026-04-25

Design rationale: docs/design/paperless-llm-metadata.md (PR 3 — prompt
augmentation from corrections).

PR 2 ships the table; PR 3 turns on consumption. The retriever fetches
the top-N most similar past confirm-diffs by document similarity and
prepends them to future extraction prompts as in-context learning
examples.

Why a separate column instead of an existing embedding service:
The example rows are short, bounded (≤ 8 KB doc_text via the
PR-2 truncation), and queried in a tight retrieval loop during every
extraction. A dedicated column with a vector index keeps that hot path
single-table — no joins, no cross-service round trips.

Dim 2560 matches the production embedding stack (qwen3-embedding:4b,
locked by cce1984705df). HNSW via halfvec cast is the same workaround
used everywhere else in this codebase to clear the 2000-dim regular-vector
index ceiling.
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
    # Column. Nullable: rows written before PR 3 (none in production yet,
    # but defensive) carry NULL; the retriever simply ignores rows with
    # NULL embeddings.
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
    op.drop_column("paperless_extraction_examples", "doc_text_embedding")
