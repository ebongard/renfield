"""Add parent_chunk_id for parent-child chunking

Revision ID: pc20260331a1
Revises: z9a0b1c2d3e4
Create Date: 2026-03-31
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "pc20260331a1"
down_revision = "z9a0b1c2d3e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "document_chunks",
        sa.Column("parent_chunk_id", sa.Integer(), sa.ForeignKey("document_chunks.id"), nullable=True),
    )
    op.create_index("idx_document_chunks_parent", "document_chunks", ["parent_chunk_id"])


def downgrade() -> None:
    op.drop_index("idx_document_chunks_parent", table_name="document_chunks")
    op.drop_column("document_chunks", "parent_chunk_id")
