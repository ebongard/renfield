"""drop scheduled_jobs table

Internal scheduler removed — scheduling is handled externally
by n8n workflows and Home Assistant automations via webhook.

Revision ID: r1s2t3u4v5w6
Revises: l2m3n4o5p6q7
Create Date: 2026-02-05
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'r1s2t3u4v5w6'
down_revision: Union[str, None] = 'l2m3n4o5p6q7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table('scheduled_jobs')


def downgrade() -> None:
    op.create_table(
        'scheduled_jobs',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('name', sa.String(100), unique=True, nullable=False),
        sa.Column('schedule_cron', sa.String(100), nullable=False),
        sa.Column('job_type', sa.String(50), nullable=False),
        sa.Column('config', sa.JSON(), nullable=True),
        sa.Column('is_enabled', sa.Boolean(), server_default='true'),
        sa.Column('last_run_at', sa.DateTime(), nullable=True),
        sa.Column('next_run_at', sa.DateTime(), nullable=True),
        sa.Column('room_id', sa.Integer(), sa.ForeignKey('rooms.id'), nullable=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
    )
