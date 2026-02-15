"""add presence_events table

Revision ID: d4e5f6g7h8i0
Revises: c3d4e5f6g7h9
Create Date: 2026-02-14

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision: str = 'd4e5f6g7h8i0'
down_revision: Union[str, None] = 'c3d4e5f6g7h9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Idempotent: table may already exist via init_db (Base.metadata.create_all)
    conn = op.get_bind()
    inspector = inspect(conn)
    if 'presence_events' not in inspector.get_table_names():
        op.create_table(
            'presence_events',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False, index=True),
            sa.Column('room_id', sa.Integer(), sa.ForeignKey('rooms.id'), nullable=False, index=True),
            sa.Column('event_type', sa.String(20), nullable=False),
            sa.Column('source', sa.String(20), server_default='ble'),
            sa.Column('confidence', sa.Float(), nullable=True),
            sa.Column('created_at', sa.DateTime(), index=True),
        )
        op.create_index(
            'ix_presence_events_analytics',
            'presence_events',
            ['user_id', 'room_id', 'created_at'],
        )


def downgrade() -> None:
    op.drop_index('ix_presence_events_analytics', table_name='presence_events')
    op.drop_table('presence_events')
