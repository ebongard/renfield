"""notes.embedding — Phase 4B dense-embedding slice

Revision ID: pc20260721_notes_embedding
Revises: pc20260720_notes
Create Date: 2026-07-21

Adds a dense ``embedding`` column + halfvec HNSW index to ``notes`` so note
retrieval can fuse semantic similarity with the existing FTS branch
(``NoteRetrieval``). Written by ``NoteService`` from title+body via
``get_embed_client()``; queried via a halfvec cast (the ``document_chunks``
pattern — regular vector HNSW is capped at 2000 dims, halfvec goes to 4096).

Idempotent: ``Base.metadata.create_all`` (backend boot) already adds the column
for a fresh install, so the column add is ``IF NOT EXISTS`` and the index build
is ``CONCURRENTLY`` + ``DROP … IF EXISTS`` (autocommit_block, allowed under
transaction_per_migration). Postgres-only — the sqlite test harness stores the
column as Text and has no pgvector, so the whole body is skipped there.
"""
import sqlalchemy as sa
from alembic import op

from models.database import EMBEDDING_DIMENSION

revision = "pc20260721_notes_embedding"
down_revision = "pc20260720_notes"
branch_labels = None
depends_on = None

_HNSW_INDEX = "idx_notes_embedding_hnsw"


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name if bind is not None else "postgresql"
    if dialect != "postgresql":
        return  # sqlite: no pgvector; the model column is Text, nothing to index.

    dim = EMBEDDING_DIMENSION
    # Column (IF NOT EXISTS — create_all may already have added it on a fresh DB).
    op.execute(f"ALTER TABLE notes ADD COLUMN IF NOT EXISTS embedding vector({dim})")

    # halfvec HNSW (regular vector HNSW is limited to 2000 dims; halfvec → 4096).
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_HNSW_INDEX}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY {_HNSW_INDEX} ON notes "
            f"USING hnsw ((embedding::halfvec({dim})) halfvec_cosine_ops)"
        )
    op.execute("ANALYZE notes")


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name if bind is not None else "postgresql"
    if dialect != "postgresql":
        return
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_HNSW_INDEX}")
    op.execute("ALTER TABLE notes DROP COLUMN IF EXISTS embedding")
