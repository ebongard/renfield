"""add room output devices

Revision ID: a1b2c3d4e5f6
Revises: 3e87d73fbc47
Create Date: 2026-01-20 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '3e87d73fbc47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'room_output_devices',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('room_id', sa.Integer(), nullable=False),
        sa.Column('renfield_device_id', sa.String(length=100), nullable=True),
        sa.Column('ha_entity_id', sa.String(length=255), nullable=True),
        sa.Column('output_type', sa.String(length=20), nullable=False, server_default='audio'),
        sa.Column('priority', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('allow_interruption', sa.Boolean(), nullable=True, server_default='false'),
        sa.Column('tts_volume', sa.Float(), nullable=True, server_default='0.5'),
        sa.Column('device_name', sa.String(length=255), nullable=True),
        sa.Column('is_enabled', sa.Boolean(), nullable=True, server_default='true'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['room_id'], ['rooms.id'], name='room_output_devices_room_id_fkey'),
        sa.ForeignKeyConstraint(['renfield_device_id'], ['room_devices.device_id'], name='room_output_devices_renfield_device_id_fkey'),
        sa.PrimaryKeyConstraint('id', name='room_output_devices_pkey')
    )
    op.create_index('ix_room_output_devices_id', 'room_output_devices', ['id'], unique=False)
    op.create_index('ix_room_output_devices_room_id', 'room_output_devices', ['room_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_room_output_devices_room_id', table_name='room_output_devices')
    op.drop_index('ix_room_output_devices_id', table_name='room_output_devices')
    op.drop_table('room_output_devices')
