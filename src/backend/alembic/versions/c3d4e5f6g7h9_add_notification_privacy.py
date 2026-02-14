"""add notification privacy columns

Revision ID: c3d4e5f6g7h9
Revises: b2c3d4e5f6g8
Create Date: 2026-02-13 18:00:00.000000

Privacy-aware TTS delivery: privacy level and target_user_id on notifications.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6g7h9'
down_revision: Union[str, None] = 'b2c3d4e5f6g8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('notifications', sa.Column('privacy', sa.String(20), server_default='public'))
    op.add_column('notifications', sa.Column('target_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True))
    op.create_index('ix_notifications_target_user_id', 'notifications', ['target_user_id'])


def downgrade() -> None:
    op.drop_index('ix_notifications_target_user_id', table_name='notifications')
    op.drop_column('notifications', 'target_user_id')
    op.drop_column('notifications', 'privacy')
