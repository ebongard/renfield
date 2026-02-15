"""add first_name/last_name to users

Revision ID: f6g7h8i9j0k2
Revises: e5f6g7h8i9j1
Create Date: 2026-02-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision: str = 'f6g7h8i9j0k2'
down_revision: Union[str, None] = 'e5f6g7h8i9j1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = inspect(conn)
    columns = [c['name'] for c in inspector.get_columns('users')]
    if 'first_name' not in columns:
        op.add_column('users', sa.Column('first_name', sa.String(100), nullable=True))
    if 'last_name' not in columns:
        op.add_column('users', sa.Column('last_name', sa.String(100), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'last_name')
    op.drop_column('users', 'first_name')
