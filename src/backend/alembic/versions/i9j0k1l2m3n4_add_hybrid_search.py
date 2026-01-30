"""add hybrid search (tsvector + GIN index + composite index)

Revision ID: i9j0k1l2m3n4
Revises: h8i9j0k1l2m3
Create Date: 2026-01-30
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'i9j0k1l2m3n4'
down_revision: Union[str, None] = 'h8i9j0k1l2m3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add tsvector column for Full-Text Search
    op.execute("""
        ALTER TABLE document_chunks
        ADD COLUMN IF NOT EXISTS search_vector tsvector
    """)

    # 2. GIN Index for Full-Text Search (fast tsvector lookups)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_document_chunks_search_vector
        ON document_chunks USING gin(search_vector)
    """)

    # 3. Composite Index for Context Window (adjacent chunk lookups)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_document_chunks_doc_chunk
        ON document_chunks (document_id, chunk_index)
    """)

    # 4. Backfill: populate search_vector for existing chunks
    op.execute("""
        UPDATE document_chunks
        SET search_vector = to_tsvector('simple', content)
        WHERE search_vector IS NULL AND content IS NOT NULL
    """)


def downgrade() -> None:
    op.execute('DROP INDEX IF EXISTS idx_document_chunks_doc_chunk')
    op.execute('DROP INDEX IF EXISTS idx_document_chunks_search_vector')
    op.execute('ALTER TABLE document_chunks DROP COLUMN IF EXISTS search_vector')
