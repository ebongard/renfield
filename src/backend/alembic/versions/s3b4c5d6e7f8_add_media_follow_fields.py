"""add media follow fields

Revision ID: s3b4c5d6e7f8
Revises: r2a3d4i5o6f7
Create Date: 2026-02-22 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 's3b4c5d6e7f8'
down_revision: Union[str, None] = 'r2a3d4i5o6f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Room owner for Media Follow Me conflict resolution
    op.add_column('rooms', sa.Column('owner_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True))

    # Per-user Media Follow Me opt-out
    op.add_column('users', sa.Column('media_follow_enabled', sa.Boolean(), nullable=False, server_default='true'))

    # Role priority for conflict resolution (lower = higher priority)
    op.add_column('roles', sa.Column('priority', sa.Integer(), nullable=False, server_default='100'))

    # Set default priorities for existing system roles
    op.execute("UPDATE roles SET priority = 10 WHERE name = 'Admin'")
    op.execute("UPDATE roles SET priority = 50 WHERE name = 'Familie'")
    op.execute("UPDATE roles SET priority = 90 WHERE name = 'Gast'")


def downgrade() -> None:
    op.drop_column('roles', 'priority')
    op.drop_column('users', 'media_follow_enabled')
    op.drop_column('rooms', 'owner_id')
