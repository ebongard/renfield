"""document_facts.tier_overridden (per-fact tier override)

Revision ID: pc20260608_fact_tier_ov
Revises: pc20260607_oblig_digest
Create Date: 2026-06-06

Adds ``document_facts.tier_overridden`` — when True, the fact's circle_tier was
set independently (e.g. a public issuer on an otherwise-private document) and the
parent-document tier cascade (AtomService.update_tier) must NOT overwrite it. The
override is sticky in both directions until explicitly reset to the document tier.
See models.database.DocumentFact.
"""
from alembic import op
import sqlalchemy as sa


revision = "pc20260608_fact_tier_ov"
down_revision = "pc20260607_oblig_digest"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "document_facts",
        sa.Column("tier_overridden", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("document_facts", "tier_overridden")
