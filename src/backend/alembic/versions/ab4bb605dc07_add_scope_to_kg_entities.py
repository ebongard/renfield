"""Add scope to kg_entities

Revision ID: ab4bb605dc07
Revises: g7h8i9j0k1l3
Create Date: 2026-02-16 10:24:13.437035

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ab4bb605dc07'
down_revision: Union[str, None] = 'g7h8i9j0k1l3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add scope column to kg_entities with default value 'personal'
    op.add_column('kg_entities', sa.Column('scope', sa.String(length=50), nullable=False, server_default='personal'))

    # Create indexes
    op.create_index(op.f('ix_kg_entities_scope'), 'kg_entities', ['scope'], unique=False)
    op.create_index('ix_kg_entities_scope_active', 'kg_entities', ['scope', 'is_active'], unique=False)


def downgrade() -> None:
    # Drop indexes
    op.drop_index('ix_kg_entities_scope_active', table_name='kg_entities')
    op.drop_index(op.f('ix_kg_entities_scope'), table_name='kg_entities')

    # Drop column
    op.drop_column('kg_entities', 'scope')
