"""documents.generated_title — LLM-synthesized human title from Schicht A facts

Revision ID: pc20260611_gen_title
Revises: pc20260610_output_target
Create Date: 2026-06-07

Adds ``documents.generated_title``: a short, human-meaningful title the Schicht A
extractor synthesizes from the document's extracted facts (issuer + document type
+ date) at ingest time. Surfaced as the Wissen/Dokumente display name when set,
falling back to the metadata ``title`` then the filename. Additive + nullable;
existing rows stay NULL until the one-off backfill (bin/backfill_document_titles.py)
runs over their already-extracted facts. Fully transactional.
"""
from alembic import op
import sqlalchemy as sa


revision = "pc20260611_gen_title"
down_revision = "pc20260610_output_target"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("generated_title", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("documents", "generated_title")
