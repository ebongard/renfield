"""add FK indexes and upgrade to HNSW vector indexes

Revision ID: j0k1l2m3n4o5
Revises: i9j0k1l2m3n4
Create Date: 2026-02-04
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'j0k1l2m3n4o5'
down_revision: Union[str, None] = 'i9j0k1l2m3n4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- FK Indexes ---
    op.create_index('ix_messages_conversation_id', 'messages', ['conversation_id'])
    op.create_index('ix_speaker_embeddings_speaker_id', 'speaker_embeddings', ['speaker_id'])
    op.create_index('ix_room_devices_room_id', 'room_devices', ['room_id'])
    op.create_index('ix_users_role_id', 'users', ['role_id'])

    # --- HNSW Indexes (replace IVFFlat if exists) ---
    # Drop old IVFFlat indexes (IF EXISTS — safe if they were never created)
    op.execute("DROP INDEX IF EXISTS idx_document_chunks_embedding")
    op.execute("DROP INDEX IF EXISTS idx_intent_corrections_embedding")

    # Create HNSW indexes with cosine similarity
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_document_chunks_embedding_hnsw
        ON document_chunks
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_intent_corrections_embedding_hnsw
        ON intent_corrections
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """)


def downgrade() -> None:
    # Remove FK indexes
    op.drop_index('ix_messages_conversation_id', table_name='messages')
    op.drop_index('ix_speaker_embeddings_speaker_id', table_name='speaker_embeddings')
    op.drop_index('ix_room_devices_room_id', table_name='room_devices')
    op.drop_index('ix_users_role_id', table_name='users')

    # Drop HNSW indexes
    op.execute("DROP INDEX IF EXISTS idx_document_chunks_embedding_hnsw")
    op.execute("DROP INDEX IF EXISTS idx_intent_corrections_embedding_hnsw")

    # Recreate IVFFlat indexes
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_document_chunks_embedding
        ON document_chunks
        USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_intent_corrections_embedding
        ON intent_corrections
        USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)
    """)
