"""add notifications table

Revision ID: k1l2m3n4o5p6
Revises: j0k1l2m3n4o5
Create Date: 2026-02-05
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'k1l2m3n4o5p6'
down_revision: Union[str, None] = 'j0k1l2m3n4o5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'notifications',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('event_type', sa.String(100), nullable=False, index=True),
        sa.Column('title', sa.String(255), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('urgency', sa.String(20), server_default='info'),
        sa.Column('room_id', sa.Integer(), sa.ForeignKey('rooms.id'), nullable=True, index=True),
        sa.Column('room_name', sa.String(100), nullable=True),
        sa.Column('source', sa.String(50), server_default='ha_automation'),
        sa.Column('source_data', sa.JSON(), nullable=True),
        sa.Column('status', sa.String(20), server_default='pending', index=True),
        sa.Column('delivered_to', sa.JSON(), nullable=True),
        sa.Column('acknowledged_by', sa.String(100), nullable=True),
        sa.Column('tts_delivered', sa.Boolean(), server_default='false'),
        sa.Column('dedup_key', sa.String(255), nullable=True, index=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), index=True),
        sa.Column('delivered_at', sa.DateTime(), nullable=True),
        sa.Column('acknowledged_at', sa.DateTime(), nullable=True),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('notifications')
