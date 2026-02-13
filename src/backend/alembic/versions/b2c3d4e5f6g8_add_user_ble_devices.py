"""add user_ble_devices table

Revision ID: b2c3d4e5f6g8
Revises: a1b2c3d4e5f7
Create Date: 2026-02-13 12:00:00.000000

BLE device registry for room-level presence detection.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6g8'
down_revision: Union[str, None] = 'a1b2c3d4e5f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'user_ble_devices',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('mac_address', sa.String(17), nullable=False),
        sa.Column('device_name', sa.String(100), nullable=False),
        sa.Column('device_type', sa.String(50), server_default='phone'),
        sa.Column('is_enabled', sa.Boolean(), server_default='true'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index('ix_user_ble_devices_user_id', 'user_ble_devices', ['user_id'])
    op.create_index('ix_user_ble_devices_mac_address', 'user_ble_devices', ['mac_address'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_user_ble_devices_mac_address', table_name='user_ble_devices')
    op.drop_index('ix_user_ble_devices_user_id', table_name='user_ble_devices')
    op.drop_table('user_ble_devices')
