"""KB near-duplicate document detection: document_duplicate_proposals + supersede column (#1170).

Revision ID: pc20260830_document_dedupe
Revises: pc20260829_documents_fts
Create Date: 2026-08-30 00:00:00.000000

Adds the review queue for KB near-duplicate DOCUMENTS (two docs that byte-hash
dedup can't catch — different file_hash — yet share a document-unique identifier
fact, e.g. the same invoice number). Mirrors kg_merge_proposals / pdf_split_proposals:
propose-only, one PENDING per pair, owner-resolved on /brain/review.

Also adds documents.superseded_by_document_id — set on the LOSER when the owner
resolves a pair with resolution='supersede' (recoverable; the doc is excluded
from retrieval WHERE superseded_by_document_id IS NULL).

PG-only (the sqlite test harness builds both via create_all from the ORM model).
Rerunnable: ADD COLUMN / CREATE TABLE/INDEX IF NOT EXISTS.
"""
from alembic import op


revision = "pc20260830_document_dedupe"
down_revision = "pc20260829_documents_fts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if (bind.dialect.name if bind is not None else "postgresql") != "postgresql":
        return

    op.execute(
        "ALTER TABLE documents "
        "ADD COLUMN IF NOT EXISTS superseded_by_document_id integer "
        "REFERENCES documents(id) ON DELETE SET NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_documents_superseded_by "
        "ON documents (superseded_by_document_id)"
    )

    op.execute("""
        CREATE TABLE IF NOT EXISTS document_duplicate_proposals (
            id                    serial PRIMARY KEY,
            user_id               integer REFERENCES users(id),
            document_a_id         integer NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            document_b_id         integer NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            signal                varchar(30) NOT NULL DEFAULT 'shared_identifier',
            shared_key            text,
            similarity            double precision NOT NULL DEFAULT 1.0,
            suggested_survivor_id integer REFERENCES documents(id) ON DELETE SET NULL,
            status                varchar(20) NOT NULL DEFAULT 'pending',
            resolution            varchar(16),
            created_at            timestamp without time zone DEFAULT now(),
            resolved_at           timestamp without time zone,
            resolved_by_user_id   integer REFERENCES users(id)
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_document_duplicate_proposals_user_status "
        "ON document_duplicate_proposals (user_id, status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_document_duplicate_proposals_status "
        "ON document_duplicate_proposals (status)"
    )
    # At most one PENDING proposal per ordered (a, b) pair — the detector stores
    # a<b so this covers the unordered pair and keeps runs idempotent.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_document_duplicate_proposals_pending_pair "
        "ON document_duplicate_proposals (document_a_id, document_b_id) "
        "WHERE status = 'pending'"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if (bind.dialect.name if bind is not None else "postgresql") != "postgresql":
        return
    op.execute("DROP TABLE IF EXISTS document_duplicate_proposals")
    op.execute("DROP INDEX IF EXISTS ix_documents_superseded_by")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS superseded_by_document_id")
