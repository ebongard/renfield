"""documents.content_embedding — doc-level mean-chunk embedding for KB dedupe P3.

Revision ID: pc20260901_doc_content_emb
Revises: pc20260831_document_date
Create Date: 2026-09-01 00:00:00.000000

Adds a nullable ``vector`` column = mean of a document's chunk embeddings, plus an
HNSW index on the ``::halfvec`` cast (pgvector caps regular-vector HNSW at 2000
dims; prod embeddings are 2560). Backs the #1170 Phase-3 text-similarity signal.
Mirrors ``pc20260721_notes_embedding``. NULL until populated at ingest / backfilled.

PG-only (sqlite create_all builds the column from the ORM). The CONCURRENTLY index
runs in an ``autocommit_block`` (the migration is otherwise transactional).
"""
from alembic import op

from models.database import EMBEDDING_DIMENSION

revision = "pc20260901_doc_content_emb"
down_revision = "pc20260831_document_date"
branch_labels = None
depends_on = None

_HNSW_INDEX = "idx_documents_content_embedding_hnsw"


def upgrade() -> None:
    bind = op.get_bind()
    if (bind.dialect.name if bind is not None else "postgresql") != "postgresql":
        return
    dim = EMBEDDING_DIMENSION
    op.execute(f"ALTER TABLE documents ADD COLUMN IF NOT EXISTS content_embedding vector({dim})")
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_HNSW_INDEX}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY {_HNSW_INDEX} ON documents "
            f"USING hnsw ((content_embedding::halfvec({dim})) halfvec_cosine_ops)"
        )
    op.execute("ANALYZE documents")


def downgrade() -> None:
    bind = op.get_bind()
    if (bind.dialect.name if bind is not None else "postgresql") != "postgresql":
        return
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_HNSW_INDEX}")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS content_embedding")
