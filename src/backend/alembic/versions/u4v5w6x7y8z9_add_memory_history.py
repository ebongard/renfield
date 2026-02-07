"""add memory_history table

Revision ID: u4v5w6x7y8z9
Revises: t3u4v5w6x7y8
Create Date: 2026-02-07
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'u4v5w6x7y8z9'
down_revision: Union[str, None] = 't3u4v5w6x7y8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'memory_history',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('memory_id', sa.Integer(), sa.ForeignKey('conversation_memories.id'), nullable=False),
        sa.Column('action', sa.String(20), nullable=False),
        sa.Column('old_content', sa.Text(), nullable=True),
        sa.Column('old_category', sa.String(20), nullable=True),
        sa.Column('old_importance', sa.Float(), nullable=True),
        sa.Column('new_content', sa.Text(), nullable=True),
        sa.Column('new_category', sa.String(20), nullable=True),
        sa.Column('new_importance', sa.Float(), nullable=True),
        sa.Column('changed_by', sa.String(30), nullable=False, server_default='system'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_memory_history_memory_id', 'memory_history', ['memory_id'])
    op.create_index('ix_memory_history_action', 'memory_history', ['action'])


def downgrade() -> None:
    op.drop_index('ix_memory_history_action', table_name='memory_history')
    op.drop_index('ix_memory_history_memory_id', table_name='memory_history')
    op.drop_table('memory_history')
