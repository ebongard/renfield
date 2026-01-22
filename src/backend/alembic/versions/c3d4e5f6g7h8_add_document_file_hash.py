"""add file_hash column to documents table for duplicate detection

Revision ID: c3d4e5f6g7h8
Revises: b2c3d4e5f6g7
Create Date: 2026-01-22 10:00:00.000000

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
    # Add file_hash column for duplicate detection (SHA256 = 64 hex characters)
    op.add_column(
        'documents',
        sa.Column('file_hash', sa.String(length=64), nullable=True)
    )
    # Create index for fast duplicate lookups
    op.create_index('ix_documents_file_hash', 'documents', ['file_hash'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_documents_file_hash', table_name='documents')
    op.drop_column('documents', 'file_hash')
