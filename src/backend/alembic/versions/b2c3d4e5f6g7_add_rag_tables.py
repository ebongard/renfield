"""add RAG tables (knowledge_bases, documents, document_chunks)

Revision ID: b2c3d4e5f6g7
Revises: a1b2c3d4e5f6
Create Date: 2026-01-21 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6g7'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Enable pgvector extension
    op.execute('CREATE EXTENSION IF NOT EXISTS vector')

    # Create knowledge_bases table
    op.create_table(
        'knowledge_bases',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=True, server_default='true'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id', name='knowledge_bases_pkey'),
        sa.UniqueConstraint('name', name='knowledge_bases_name_key')
    )
    op.create_index('ix_knowledge_bases_id', 'knowledge_bases', ['id'], unique=False)

    # Create documents table
    op.create_table(
        'documents',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('knowledge_base_id', sa.Integer(), nullable=True),
        sa.Column('filename', sa.String(length=255), nullable=False),
        sa.Column('file_path', sa.String(length=512), nullable=False),
        sa.Column('file_type', sa.String(length=50), nullable=True),
        sa.Column('file_size', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=50), nullable=True, server_default='pending'),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('title', sa.String(length=512), nullable=True),
        sa.Column('author', sa.String(length=255), nullable=True),
        sa.Column('page_count', sa.Integer(), nullable=True),
        sa.Column('chunk_count', sa.Integer(), nullable=True, server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('processed_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['knowledge_base_id'], ['knowledge_bases.id'], name='documents_knowledge_base_id_fkey'),
        sa.PrimaryKeyConstraint('id', name='documents_pkey')
    )
    op.create_index('ix_documents_id', 'documents', ['id'], unique=False)
    op.create_index('ix_documents_knowledge_base_id', 'documents', ['knowledge_base_id'], unique=False)
    op.create_index('ix_documents_status', 'documents', ['status'], unique=False)

    # Create document_chunks table with vector column
    op.create_table(
        'document_chunks',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('document_id', sa.Integer(), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('chunk_index', sa.Integer(), nullable=True),
        sa.Column('page_number', sa.Integer(), nullable=True),
        sa.Column('section_title', sa.String(length=512), nullable=True),
        sa.Column('chunk_type', sa.String(length=50), nullable=True, server_default='paragraph'),
        sa.Column('chunk_metadata', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id'], name='document_chunks_document_id_fkey'),
        sa.PrimaryKeyConstraint('id', name='document_chunks_pkey')
    )
    op.create_index('ix_document_chunks_id', 'document_chunks', ['id'], unique=False)
    op.create_index('ix_document_chunks_document_id', 'document_chunks', ['document_id'], unique=False)

    # Add vector column for embeddings (768 dimensions for nomic-embed-text)
    op.execute('ALTER TABLE document_chunks ADD COLUMN embedding vector(768)')

    # Create IVFFlat index for fast similarity search
    # lists = sqrt(row_count) is recommended, starting with 100 for up to 10k chunks
    op.execute('''
        CREATE INDEX idx_document_chunks_embedding
        ON document_chunks
        USING ivfflat (embedding vector_cosine_ops)
        WITH (lists = 100)
    ''')


def downgrade() -> None:
    # Drop vector index
    op.execute('DROP INDEX IF EXISTS idx_document_chunks_embedding')

    # Drop tables in reverse order (respecting foreign keys)
    op.drop_index('ix_document_chunks_document_id', table_name='document_chunks')
    op.drop_index('ix_document_chunks_id', table_name='document_chunks')
    op.drop_table('document_chunks')

    op.drop_index('ix_documents_status', table_name='documents')
    op.drop_index('ix_documents_knowledge_base_id', table_name='documents')
    op.drop_index('ix_documents_id', table_name='documents')
    op.drop_table('documents')

    op.drop_index('ix_knowledge_bases_id', table_name='knowledge_bases')
    op.drop_table('knowledge_bases')

    # Note: We don't drop the pgvector extension as other things might use it
