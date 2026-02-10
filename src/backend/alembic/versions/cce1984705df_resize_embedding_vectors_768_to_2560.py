"""resize_embedding_vectors_768_to_2560

Revision ID: cce1984705df
Revises: w6x7y8z9a0b1
Create Date: 2026-02-10 13:34:52.595443

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'cce1984705df'
down_revision: Union[str, None] = 'w6x7y8z9a0b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# All tables with embedding Vector columns
TABLES = [
    "document_chunks",
    "intent_corrections",
    "notifications",
    "notification_suppressions",
    "conversation_memories",
]


def upgrade() -> None:
    # Resize all embedding columns from vector(768) to vector(2560).
    # Existing embeddings become invalid and must be re-generated via /admin/reembed.
    for table in TABLES:
        # Drop old data — dimensions are incompatible, re-embed is required anyway
        op.execute(f"UPDATE {table} SET embedding = NULL WHERE embedding IS NOT NULL")
        op.execute(f"ALTER TABLE {table} ALTER COLUMN embedding TYPE vector(2560)")


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"UPDATE {table} SET embedding = NULL WHERE embedding IS NOT NULL")
        op.execute(f"ALTER TABLE {table} ALTER COLUMN embedding TYPE vector(768)")
