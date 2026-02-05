"""add conversation_memory table

Long-term memory for conversations — stores facts, preferences,
instructions, and context with pgvector embeddings for semantic retrieval.

Revision ID: s2t3u4v5w6x7
Revises: r1s2t3u4v5w6
Create Date: 2026-02-05
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

try:
    from pgvector.sqlalchemy import Vector
    PGVECTOR_AVAILABLE = True
except ImportError:
    PGVECTOR_AVAILABLE = False

# revision identifiers, used by Alembic.
revision: str = 's2t3u4v5w6x7'
down_revision: Union[str, None] = 'r1s2t3u4v5w6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Ensure pgvector extension exists
    op.execute('CREATE EXTENSION IF NOT EXISTS vector')

    op.create_table(
        'conversation_memories',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('category', sa.String(length=20), nullable=False, server_default='fact'),
        sa.Column('source_session_id', sa.String(length=255), nullable=True),
        sa.Column('source_message_id', sa.Integer(), nullable=True),
        sa.Column('embedding', Vector(768) if PGVECTOR_AVAILABLE else sa.Text(), nullable=True),
        sa.Column('importance', sa.Float(), server_default='0.5'),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.Column('access_count', sa.Integer(), server_default='0'),
        sa.Column('last_accessed_at', sa.DateTime(), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default='true'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['source_message_id'], ['messages.id']),
        sa.PrimaryKeyConstraint('id'),
    )

    # Indexes
    op.create_index('ix_conversation_memories_id', 'conversation_memories', ['id'])
    op.create_index('ix_conversation_memories_user_id', 'conversation_memories', ['user_id'])
    op.create_index('ix_conversation_memories_category', 'conversation_memories', ['category'])
    op.create_index('ix_conversation_memories_source_session_id', 'conversation_memories', ['source_session_id'])
    op.create_index('ix_conversation_memories_is_active', 'conversation_memories', ['is_active'])

    # HNSW index for vector similarity search
    if PGVECTOR_AVAILABLE:
        op.execute(
            'CREATE INDEX ix_conversation_memories_embedding_hnsw '
            'ON conversation_memories '
            'USING hnsw (embedding vector_cosine_ops) '
            'WITH (m = 16, ef_construction = 64)'
        )


def downgrade() -> None:
    op.execute('DROP INDEX IF EXISTS ix_conversation_memories_embedding_hnsw')
    op.drop_index('ix_conversation_memories_is_active', table_name='conversation_memories')
    op.drop_index('ix_conversation_memories_source_session_id', table_name='conversation_memories')
    op.drop_index('ix_conversation_memories_category', table_name='conversation_memories')
    op.drop_index('ix_conversation_memories_user_id', table_name='conversation_memories')
    op.drop_index('ix_conversation_memories_id', table_name='conversation_memories')
    op.drop_table('conversation_memories')
