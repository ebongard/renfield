"""resize_embedding_vectors_768_to_2560

Revision ID: cce1984705df
Revises: w6x7y8z9a0b1
Create Date: 2026-02-10 13:34:52.595443

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'cce1984705df'
down_revision: Union[str, None] = 'w6x7y8z9a0b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# All tables with embedding Vector columns
TABLES = [
    "document_chunks",
    "intent_corrections",
    "notifications",
    "notification_suppressions",
    "conversation_memories",
]


def upgrade() -> None:
    # Resize all embedding columns from vector(768) to vector(2560).
    # Existing embeddings become invalid and must be re-generated via /admin/reembed.
    for table in TABLES:
        # Drop old data — dimensions are incompatible, re-embed is required anyway
        op.execute(f"UPDATE {table} SET embedding = NULL WHERE embedding IS NOT NULL")
        op.execute(f"ALTER TABLE {table} ALTER COLUMN embedding TYPE vector(2560)")

    # Note: ALTER COLUMN TYPE automatically drops any vector index (ivfflat/hnsw)
    # on the affected column. Recreate an HNSW index for document_chunks so that
    # dense vector search doesn't fall back to a full table scan.
    # Both regular HNSW and IVFFlat have a 2000-dim limit; use halfvec cast
    # (pgvector 0.8.0+ supports HNSW via halfvec for up to 4096 dimensions).
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_document_chunks_embedding_hnsw
        ON document_chunks
        USING hnsw ((embedding::halfvec(2560)) halfvec_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """)


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"UPDATE {table} SET embedding = NULL WHERE embedding IS NOT NULL")
        op.execute(f"ALTER TABLE {table} ALTER COLUMN embedding TYPE vector(768)")
