"""add role allowed_plugins

Revision ID: e5f6g7h8i9j0
Revises: d4e5f6g7h8i9
Create Date: 2026-01-23 10:00:00.000000

Adds allowed_plugins column to roles table for granular plugin access control.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5f6g7h8i9j0'
down_revision: Union[str, None] = 'd4e5f6g7h8i9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add allowed_plugins column to roles table
    # Empty list means all plugins are allowed
    op.add_column(
        'roles',
        sa.Column('allowed_plugins', sa.JSON(), nullable=False, server_default='[]')
    )


def downgrade() -> None:
    op.drop_column('roles', 'allowed_plugins')
