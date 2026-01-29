"""add intent_corrections table for feedback learning

Revision ID: h8i9j0k1l2m3
Revises: g7h8i9j0k1l2
Create Date: 2026-01-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

try:
    from pgvector.sqlalchemy import Vector
    PGVECTOR_AVAILABLE = True
except ImportError:
    PGVECTOR_AVAILABLE = False

# revision identifiers, used by Alembic.
revision: str = 'h8i9j0k1l2m3'
down_revision: Union[str, None] = 'g7h8i9j0k1l2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Ensure pgvector extension exists
    op.execute('CREATE EXTENSION IF NOT EXISTS vector')

    op.create_table(
        'intent_corrections',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('message_text', sa.Text(), nullable=False),
        sa.Column('feedback_type', sa.String(length=20), nullable=False),
        sa.Column('original_value', sa.String(length=100), nullable=False),
        sa.Column('corrected_value', sa.String(length=100), nullable=False),
        sa.Column('embedding', Vector(768) if PGVECTOR_AVAILABLE else sa.Text(), nullable=True),
        sa.Column('context', sa.JSON(), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_intent_corrections_id', 'intent_corrections', ['id'])
    op.create_index('ix_intent_corrections_feedback_type', 'intent_corrections', ['feedback_type'])
    op.create_index('ix_intent_corrections_user_id', 'intent_corrections', ['user_id'])

    # pgvector IVFFlat index for cosine similarity search
    # lists=20 is appropriate for small tables (hundreds of rows)
    if PGVECTOR_AVAILABLE:
        op.execute("""
            CREATE INDEX idx_intent_corrections_embedding
            ON intent_corrections
            USING ivfflat (embedding vector_cosine_ops) WITH (lists = 20)
        """)


def downgrade() -> None:
    op.execute('DROP INDEX IF EXISTS idx_intent_corrections_embedding')
    op.drop_index('ix_intent_corrections_user_id', table_name='intent_corrections')
    op.drop_index('ix_intent_corrections_feedback_type', table_name='intent_corrections')
    op.drop_index('ix_intent_corrections_id', table_name='intent_corrections')
    op.drop_table('intent_corrections')
