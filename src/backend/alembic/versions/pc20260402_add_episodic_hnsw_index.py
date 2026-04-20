"""Add HNSW vector index on episodic_memories.embedding

The pc20260401 migration created the embedding column as sa.Text(),
but SQLAlchemy create_all() may have already created it as vector(768).
This migration:
1. Converts the column to vector(768) if it's still text (idempotent)
2. Creates the HNSW index for cosine similarity search

Revision ID: pc20260402a1
Revises: pc20260401a1
Create Date: 2026-04-02
"""
from alembic import op
from sqlalchemy import text

# revision identifiers
revision = "pc20260402a1"
down_revision = "pc20260401a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # Check if pgvector extension is available
    result = conn.execute(
        text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
    )
    if result.scalar() is None:
        return  # pgvector not installed, skip

    # Check current column type
    result = conn.execute(text(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name = 'episodic_memories' AND column_name = 'embedding'"
    ))
    row = result.first()
    if row is None:
        return  # table or column doesn't exist

    # Convert from text to vector if needed
    if row[0] == "text":
        op.execute(text(
            "ALTER TABLE episodic_memories "
            "ALTER COLUMN embedding TYPE vector(768) "
            "USING embedding::vector(768)"
        ))

    # Create HNSW index if it doesn't exist.
    # pgvector HNSW caps vector_cosine_ops at 2000 dims; for high-dim
    # embeddings (qwen3-embedding:4b = 2560) cast to halfvec on the
    # index side. Detect actual dim from the column metadata.
    result = conn.execute(
        text("SELECT 1 FROM pg_indexes WHERE indexname = 'ix_episodic_embedding_hnsw'")
    )
    if result.scalar() is None:
        # vector(N) reports its dim via atttypmod (after the +4 typmod offset).
        dim_row = conn.execute(text(
            "SELECT atttypmod - 4 AS dim FROM pg_attribute "
            "WHERE attrelid = 'episodic_memories'::regclass "
            "AND attname = 'embedding'"
        )).first()
        dim = int(dim_row.dim) if dim_row and dim_row.dim and dim_row.dim > 0 else 768

        if dim > 2000:
            op.execute(text(
                f"CREATE INDEX ix_episodic_embedding_hnsw "
                f"ON episodic_memories "
                f"USING hnsw ((embedding::halfvec({dim})) halfvec_cosine_ops) "
                f"WITH (m = 16, ef_construction = 64)"
            ))
        else:
            op.execute(text(
                "CREATE INDEX ix_episodic_embedding_hnsw "
                "ON episodic_memories "
                "USING hnsw (embedding vector_cosine_ops) "
                "WITH (m = 16, ef_construction = 64)"
            ))


def downgrade() -> None:
    op.execute(text("DROP INDEX IF EXISTS ix_episodic_embedding_hnsw"))
