"""Unique constraint on (file_hash, knowledge_base_id) for documents

Revision ID: pc20260419_uniq_doc_hash
Revises: pc20260402a1
Create Date: 2026-04-19 14:20:00.000000

Closes the concurrent-upload race: two simultaneous uploads of the same
bytes to the same KB used to slip past the SELECT-based duplicate check
and both commit, leaving two `documents` rows with identical file_hash.
After this migration the second INSERT raises IntegrityError which the
upload route converts to 409.

Design:
- Partial unique index `WHERE file_hash IS NOT NULL` so legacy rows
  with a NULL file_hash (pre-existing before the hash column was
  introduced in c3d4e5f6g7h8) don't collide with each other.
- `NULLS NOT DISTINCT` on Postgres 15+ so a NULL knowledge_base_id
  (global RAG, doc belongs to no specific KB) is still covered — two
  global uploads of the same bytes still produce 409.
- SQLite fallback uses a plain composite unique constraint; NULLs are
  always distinct there, which is fine for unit-test coverage.

Dedupe step runs before the index add so pre-existing hash duplicates
don't break the ALTER. Keeps oldest row per (file_hash, kb_id),
reassigns both document_chunks.document_id AND chat_uploads.document_id
from losers to the keeper, then deletes losers. The chat_uploads FK
has no ON DELETE rule — without the reassignment the migration would
fail with a FK violation on any chat upload that referenced a loser.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'pc20260419_uniq_doc_hash'
down_revision: Union[str, None] = 'pc20260402a1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_INDEX_NAME = "uq_documents_file_hash_kb"


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        # Step 1 — consolidate any pre-existing non-NULL-hash dupes.
        bind.exec_driver_sql(
            """
            WITH ranked AS (
              SELECT
                id,
                file_hash,
                knowledge_base_id,
                ROW_NUMBER() OVER (
                  PARTITION BY file_hash, knowledge_base_id
                  ORDER BY created_at ASC, id ASC
                ) AS rn,
                FIRST_VALUE(id) OVER (
                  PARTITION BY file_hash, knowledge_base_id
                  ORDER BY created_at ASC, id ASC
                ) AS keeper_id
              FROM documents
              WHERE file_hash IS NOT NULL
            )
            UPDATE document_chunks c
               SET document_id = r.keeper_id
              FROM ranked r
             WHERE c.document_id = r.id
               AND r.rn > 1;
            """
        )
        bind.exec_driver_sql(
            """
            WITH ranked AS (
              SELECT
                id,
                ROW_NUMBER() OVER (
                  PARTITION BY file_hash, knowledge_base_id
                  ORDER BY created_at ASC, id ASC
                ) AS rn,
                FIRST_VALUE(id) OVER (
                  PARTITION BY file_hash, knowledge_base_id
                  ORDER BY created_at ASC, id ASC
                ) AS keeper_id
              FROM documents
              WHERE file_hash IS NOT NULL
            )
            UPDATE chat_uploads u
               SET document_id = r.keeper_id
              FROM ranked r
             WHERE u.document_id = r.id
               AND r.rn > 1;
            """
        )
        bind.exec_driver_sql(
            """
            DELETE FROM documents
             WHERE id IN (
               SELECT id FROM (
                 SELECT
                   id,
                   ROW_NUMBER() OVER (
                     PARTITION BY file_hash, knowledge_base_id
                     ORDER BY created_at ASC, id ASC
                   ) AS rn
                 FROM documents
                 WHERE file_hash IS NOT NULL
               ) t
               WHERE t.rn > 1
             );
            """
        )

        # Step 2 — partial unique index with NULLS NOT DISTINCT.
        # Postgres 15+ required for the NULLS clause; pg16 on this stack.
        bind.exec_driver_sql(
            f"""
            CREATE UNIQUE INDEX {_INDEX_NAME}
                ON documents (file_hash, knowledge_base_id)
                NULLS NOT DISTINCT
             WHERE file_hash IS NOT NULL;
            """
        )
    else:
        # SQLite test harness. Plain unique constraint via a unique index
        # so the SQLAlchemy metadata reflect() sees the same name.
        op.create_index(
            _INDEX_NAME,
            "documents",
            ["file_hash", "knowledge_base_id"],
            unique=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql(f"DROP INDEX IF EXISTS {_INDEX_NAME};")
    else:
        op.drop_index(_INDEX_NAME, table_name="documents")
