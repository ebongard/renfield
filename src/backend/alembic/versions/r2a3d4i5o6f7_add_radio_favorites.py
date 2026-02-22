"""add radio_favorites

Revision ID: r2a3d4i5o6f7
Revises: 1a054148bfb1
Create Date: 2026-02-22 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'r2a3d4i5o6f7'
down_revision: Union[str, None] = '1a054148bfb1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'radio_favorites',
        sa.Column('id', sa.Integer(), primary_key=True, index=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True, index=True),
        sa.Column('station_id', sa.String(50), nullable=False),
        sa.Column('station_name', sa.String(255), nullable=False),
        sa.Column('station_image', sa.String(512), nullable=True),
        sa.Column('genre', sa.String(100), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )
    op.create_index(
        'ix_radio_favorites_user_station',
        'radio_favorites',
        ['user_id', 'station_id'],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index('ix_radio_favorites_user_station', table_name='radio_favorites')
    op.drop_table('radio_favorites')
