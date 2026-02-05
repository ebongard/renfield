"""add notification intelligence tables and columns

Revision ID: l2m3n4o5p6q7
Revises: k1l2m3n4o5p6
Create Date: 2026-02-05
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'l2m3n4o5p6q7'
down_revision: Union[str, None] = 'k1l2m3n4o5p6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -- New columns on notifications --
    op.add_column('notifications', sa.Column('enriched', sa.Boolean(), server_default='false'))
    op.add_column('notifications', sa.Column('original_message', sa.Text(), nullable=True))
    op.add_column('notifications', sa.Column('urgency_auto', sa.Boolean(), server_default='false'))

    # Embedding column (pgvector) — added via raw SQL for Vector type
    try:
        op.execute("ALTER TABLE notifications ADD COLUMN embedding vector(768)")
    except Exception:
        # Fallback if pgvector not available — use TEXT
        op.add_column('notifications', sa.Column('embedding', sa.Text(), nullable=True))

    # -- notification_suppressions --
    op.create_table(
        'notification_suppressions',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('event_pattern', sa.String(255), nullable=False, index=True),
        sa.Column('source_notification_id', sa.Integer(), sa.ForeignKey('notifications.id'), nullable=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('reason', sa.String(255), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default='true'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
    )

    # Embedding on suppressions
    try:
        op.execute("ALTER TABLE notification_suppressions ADD COLUMN embedding vector(768)")
    except Exception:
        op.add_column('notification_suppressions', sa.Column('embedding', sa.Text(), nullable=True))

    # -- scheduled_jobs --
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

    # -- reminders --
    op.create_table(
        'reminders',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('trigger_at', sa.DateTime(), nullable=False, index=True),
        sa.Column('room_id', sa.Integer(), sa.ForeignKey('rooms.id'), nullable=True),
        sa.Column('room_name', sa.String(100), nullable=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('session_id', sa.String(255), nullable=True),
        sa.Column('status', sa.String(20), server_default='pending'),
        sa.Column('notification_id', sa.Integer(), sa.ForeignKey('notifications.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('fired_at', sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('reminders')
    op.drop_table('scheduled_jobs')
    op.drop_table('notification_suppressions')

    op.drop_column('notifications', 'embedding')
    op.drop_column('notifications', 'enriched')
    op.drop_column('notifications', 'original_message')
    op.drop_column('notifications', 'urgency_auto')
