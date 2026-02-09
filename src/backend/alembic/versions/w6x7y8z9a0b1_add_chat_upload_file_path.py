"""add file_path to chat_uploads

Revision ID: w6x7y8z9a0b1
Revises: v5w6x7y8z9a0
Create Date: 2026-02-09
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'w6x7y8z9a0b1'
down_revision: Union[str, None] = 'v5w6x7y8z9a0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('chat_uploads', sa.Column('file_path', sa.String(512), nullable=True))


def downgrade() -> None:
    op.drop_column('chat_uploads', 'file_path')
