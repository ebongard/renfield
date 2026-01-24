"""add user preferred_language

Revision ID: f6g7h8i9j0k1
Revises: e5f6g7h8i9j0
Create Date: 2026-01-24 10:00:00.000000

Adds preferred_language column to users table for multi-language support.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f6g7h8i9j0k1'
down_revision: Union[str, None] = 'e5f6g7h8i9j0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add preferred_language column to users table
    op.add_column(
        'users',
        sa.Column('preferred_language', sa.String(10), nullable=False, server_default='de')
    )


def downgrade() -> None:
    # Remove preferred_language column from users table
    op.drop_column('users', 'preferred_language')
