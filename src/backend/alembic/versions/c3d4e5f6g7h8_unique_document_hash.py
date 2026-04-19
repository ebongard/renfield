"""Unique constraint on (file_hash, knowledge_base_id) for documents

Revision ID: c3d4e5f6g7h8
Revises: b2c3d4e5f6g7
Create Date: 2026-04-19 14:00:00.000000

Closes the concurrent-upload race: two simultaneous uploads of the same
bytes to the same KB used to slip past the SELECT-based duplicate check
and both commit successfully, leaving two `documents` rows with identical
`file_hash`. After this migration, the second INSERT raises
IntegrityError → the route converts it into a 409 with the winning row.

PostgreSQL 15+ supports NULLS NOT DISTINCT, which makes the constraint
catch the case where both rows have `knowledge_base_id IS NULL` (the
global RAG case). Older PG versions treat NULLs as always-distinct, so
the constraint silently skips those rows — on our pg16 stack this is
fine. SQLite test harness accepts the clause but treats NULLs as
distinct; tests that need the strict semantics run against Postgres.

Dedupe step runs first so the constraint add doesn't fail on existing
dupes. Keeps the oldest row per (file_hash, kb) group; reassigns any
chunks from the losing rows to the winner before deleting them.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6g7h8'
down_revision: Union[str, None] = 'b2c3d4e5f6g7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # Step 1 — consolidate any pre-existing dupes. Group by hash + kb,
    # keep the oldest id, reassign chunks from losers to the winner,
    # then delete losers.
    if dialect == "postgresql":
        bind.exec_driver_sql(
            """
            WITH dupes AS (
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
            UPDATE document_chunks
               SET document_id = d.keeper_id
              FROM dupes d
             WHERE document_chunks.document_id = d.id
               AND d.rn > 1;
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

        # Step 2 — add the constraint with NULLS NOT DISTINCT.
        bind.exec_driver_sql(
            """
            ALTER TABLE documents
              ADD CONSTRAINT uq_documents_file_hash_kb
              UNIQUE NULLS NOT DISTINCT (file_hash, knowledge_base_id);
            """
        )
    else:
        # SQLite test path — plain unique constraint. NULLs are distinct
        # in SQLite, so the "null kb, null kb" edge isn't enforced here,
        # but real duplicate races (same kb_id, same hash) are caught.
        op.create_unique_constraint(
            "uq_documents_file_hash_kb",
            "documents",
            ["file_hash", "knowledge_base_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "postgresql":
        bind.exec_driver_sql(
            "ALTER TABLE documents DROP CONSTRAINT IF EXISTS uq_documents_file_hash_kb;"
        )
    else:
        op.drop_constraint("uq_documents_file_hash_kb", "documents", type_="unique")
