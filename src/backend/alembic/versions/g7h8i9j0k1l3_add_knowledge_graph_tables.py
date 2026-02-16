"""add knowledge graph tables

Revision ID: g7h8i9j0k1l3
Revises: f6g7h8i9j0k2
Create Date: 2026-02-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision: str = 'g7h8i9j0k1l3'
down_revision: Union[str, None] = 'f6g7h8i9j0k2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = inspect(conn)
    existing_tables = inspector.get_table_names()

    if 'kg_entities' not in existing_tables:
        op.create_table(
            'kg_entities',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True, index=True),
            sa.Column('name', sa.String(255), nullable=False),
            sa.Column('entity_type', sa.String(50), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('embedding', sa.Text(), nullable=True),  # Will be cast to vector
            sa.Column('mention_count', sa.Integer(), server_default='1'),
            sa.Column('first_seen_at', sa.DateTime(), server_default=sa.func.now()),
            sa.Column('last_seen_at', sa.DateTime(), server_default=sa.func.now()),
            sa.Column('is_active', sa.Boolean(), server_default='true', index=True),
        )
        op.create_index('ix_kg_entities_user_active', 'kg_entities', ['user_id', 'is_active'])

        # Convert embedding column to vector type (pgvector)
        op.execute('ALTER TABLE kg_entities ALTER COLUMN embedding TYPE vector(768) USING embedding::vector(768)')

        # HNSW index for vector similarity search
        op.execute(
            'CREATE INDEX ix_kg_entities_embedding ON kg_entities '
            'USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)'
        )

    if 'kg_relations' not in existing_tables:
        op.create_table(
            'kg_relations',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True, index=True),
            sa.Column('subject_id', sa.Integer(), sa.ForeignKey('kg_entities.id'), nullable=False, index=True),
            sa.Column('predicate', sa.String(100), nullable=False),
            sa.Column('object_id', sa.Integer(), sa.ForeignKey('kg_entities.id'), nullable=False, index=True),
            sa.Column('confidence', sa.Float(), server_default='0.8'),
            sa.Column('source_session_id', sa.String(255), nullable=True),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
            sa.Column('is_active', sa.Boolean(), server_default='true', index=True),
        )


def downgrade() -> None:
    op.drop_table('kg_relations')
    op.drop_table('kg_entities')
