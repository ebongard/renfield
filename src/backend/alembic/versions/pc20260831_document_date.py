"""documents.document_date — the document's own date, for sorting.

Revision ID: pc20260831_document_date
Revises: pc20260830_document_dedupe
Create Date: 2026-08-31 00:00:00.000000

A nullable Date holding the document's OWN date (invoice/letter date), derived at
Schicht-A extraction from the facts (rechnungsdatum → other date facts → the
generated title's date), distinct from ``created_at`` (the import timestamp). Lets
``/wissen/dokumente`` sort by document date. NULL for docs with no derivable date
(sorted last); backfilled from stored facts by bin/backfill_document_dates.py.

PG-only (sqlite create_all builds it from the ORM model). Rerunnable.
"""
from alembic import op


revision = "pc20260831_document_date"
down_revision = "pc20260830_document_dedupe"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if (bind.dialect.name if bind is not None else "postgresql") != "postgresql":
        return
    op.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS document_date date")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_documents_document_date "
        "ON documents (document_date)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if (bind.dialect.name if bind is not None else "postgresql") != "postgresql":
        return
    op.execute("DROP INDEX IF EXISTS ix_documents_document_date")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS document_date")
