"""add detection_method to user_ble_devices

Revision ID: e5f6g7h8i9j1
Revises: d4e5f6g7h8i0
Create Date: 2026-02-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision: str = 'e5f6g7h8i9j1'
down_revision: Union[str, None] = 'd4e5f6g7h8i0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = inspect(conn)
    columns = [c['name'] for c in inspector.get_columns('user_ble_devices')]
    if 'detection_method' not in columns:
        op.add_column(
            'user_ble_devices',
            sa.Column('detection_method', sa.String(20), server_default='ble'),
        )


def downgrade() -> None:
    op.drop_column('user_ble_devices', 'detection_method')
