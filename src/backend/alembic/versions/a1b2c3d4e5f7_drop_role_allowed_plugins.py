"""drop role allowed_plugins

Revision ID: a1b2c3d4e5f7
Revises: cce1984705df
Create Date: 2026-02-11 12:00:00.000000

Remove allowed_plugins column from roles table.
The YAML plugin system has been replaced by MCP servers.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f7'
down_revision: Union[str, None] = 'cce1984705df'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column('roles', 'allowed_plugins')


def downgrade() -> None:
    op.add_column(
        'roles',
        sa.Column('allowed_plugins', sa.JSON(), nullable=False, server_default='[]')
    )
