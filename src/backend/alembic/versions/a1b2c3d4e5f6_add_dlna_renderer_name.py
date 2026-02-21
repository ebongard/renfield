"""Add dlna_renderer_name column to room_output_devices.

Revision ID: a1b2c3d4e5f6
Revises: q2r3s4t5u6v7
Create Date: 2026-02-21

Nullable column — no data migration needed.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'q2r3s4t5u6v7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('room_output_devices', sa.Column('dlna_renderer_name', sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column('room_output_devices', 'dlna_renderer_name')
