"""add chat_uploads table

Revision ID: v5w6x7y8z9a0
Revises: u4v5w6x7y8z9
Create Date: 2026-02-09
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'v5w6x7y8z9a0'
down_revision: Union[str, None] = 'u4v5w6x7y8z9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'chat_uploads',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('session_id', sa.String(128), nullable=False, index=True),
        sa.Column('document_id', sa.Integer(), sa.ForeignKey('documents.id'), nullable=True),
        sa.Column('filename', sa.String(255), nullable=False),
        sa.Column('file_type', sa.String(50), nullable=True),
        sa.Column('file_size', sa.Integer(), nullable=True),
        sa.Column('file_hash', sa.String(64), nullable=True, index=True),
        sa.Column('extracted_text', sa.Text(), nullable=True),
        sa.Column('status', sa.String(50), server_default='processing', nullable=True, index=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('knowledge_base_id', sa.Integer(), sa.ForeignKey('knowledge_bases.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('chat_uploads')
