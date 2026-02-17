"""Fix KB performance: HNSW vector indexes and composite lookup index

Revision ID: p1q2r3s4t5u6
Revises: ab4bb605dc07
Create Date: 2026-02-17

The embedding resize migration (cce1984705df) dropped the HNSW index on
document_chunks.embedding via ALTER COLUMN TYPE but never recreated it,
causing a full table scan on every dense vector search. This migration
restores all missing performance indexes for the KB and Knowledge Graph.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'p1q2r3s4t5u6'
down_revision: Union[str, None] = 'ab4bb605dc07'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- HNSW vector index on document_chunks.embedding ---
    # Lost when cce1984705df resized the column from vector(768) to vector(2560).
    # Both HNSW and IVFFlat have a 2000-dim limit for regular vector type.
    # pgvector 0.8.0+ supports HNSW via halfvec cast for up to 4096 dimensions.
    # Uses IF NOT EXISTS — safe to run on instances where the index already exists.
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_document_chunks_embedding_hnsw
        ON document_chunks
        USING hnsw ((embedding::halfvec(2560)) halfvec_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """)

    # --- HNSW vector index on kg_entities.embedding ---
    # Entity similarity search for deduplication and context retrieval.
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_kg_entities_embedding_hnsw
        ON kg_entities
        USING hnsw ((embedding::halfvec(2560)) halfvec_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """)

    # --- Composite index for context window expansion ---
    # RAGService fetches adjacent chunks by (document_id, chunk_index ±N).
    # Without this index, every context window expansion is a full doc scan.
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_document_chunks_doc_chunk
        ON document_chunks (document_id, chunk_index)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_document_chunks_embedding_hnsw")
    op.execute("DROP INDEX IF EXISTS idx_kg_entities_embedding_hnsw")
    op.execute("DROP INDEX IF EXISTS idx_document_chunks_doc_chunk")
