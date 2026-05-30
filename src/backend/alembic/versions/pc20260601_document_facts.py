"""document_facts table (Schicht A field extractor)

Revision ID: pc20260601_document_facts
Revises: pc20260530_dph
Create Date: 2026-05-30

Adds ``document_facts`` — the storage for structured facts the Schicht A
extractor pulls from a Document's ``field_text`` (the post_document_ingest
consumer). Each row is a polymorphic atom source (``atom_type='document_fact'``,
see models.database.DocumentFact + AtomService): the fact inherits the parent
document's ``circle_tier`` and participates in cross-source retrieval.

One table, three shapes discriminated by ``category``:
  identifier  — deterministic regex hit (Steuernummer/IBAN/Rechnungsnummer);
                ``normalized_value`` carries the whitespace-collapsed form
                (poppler -layout letter-spaces wide-tracked lines).
  obligation  — LLM-extracted actionable; ``obligation_date`` (AS PRINTED),
                optional amount, ``legal_gate`` (statutory → human-confirmed).
  universal   — query-layer fact (issuer, total).

``atom_id`` is a NOT NULL FK to ``atoms`` with ON DELETE CASCADE: purging the
atom (GDPR Art. 17) drops the fact. Denormalized ``circle_tier`` mirrors the
parent document and is kept in lockstep by AtomService.update_tier's
kb_document cascade.
"""
from alembic import op
import sqlalchemy as sa


revision = "pc20260601_document_facts"
down_revision = "pc20260530_dph"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "document_facts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "document_id",
            sa.Integer,
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("normalized_value", sa.Text, nullable=True),
        sa.Column("excerpt", sa.Text, nullable=True),
        sa.Column("obligation_date", sa.Date, nullable=True),
        sa.Column("amount_value", sa.Numeric(14, 2), nullable=True),
        sa.Column("amount_currency", sa.String(8), nullable=True),
        sa.Column("legal_gate", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("payment_method", sa.String(16), nullable=True),
        sa.Column("confidence", sa.Float, nullable=True),
        sa.Column("source", sa.String(16), nullable=False, server_default="deterministic"),
        sa.Column(
            "atom_id",
            sa.String(36),
            sa.ForeignKey("atoms.atom_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("circle_tier", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index(
        "idx_document_facts_doc_category", "document_facts", ["document_id", "category"]
    )
    op.create_index(
        "idx_document_facts_doc_tier", "document_facts", ["document_id", "circle_tier"]
    )
    op.create_index(
        "ix_document_facts_document_id", "document_facts", ["document_id"]
    )
    op.create_index("ix_document_facts_atom_id", "document_facts", ["atom_id"])


def downgrade() -> None:
    op.drop_index("ix_document_facts_atom_id", table_name="document_facts")
    op.drop_index("ix_document_facts_document_id", table_name="document_facts")
    op.drop_index("idx_document_facts_doc_tier", table_name="document_facts")
    op.drop_index("idx_document_facts_doc_category", table_name="document_facts")
    op.drop_table("document_facts")
