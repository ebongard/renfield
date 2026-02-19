"""Add Paperless Audit V2 columns: date, language, storage path, custom fields,
missing metadata, content completeness, duplicate detection, content hash.

Revision ID: q2r3s4t5u6v7
Revises: p1q2r3s4t5u6
Create Date: 2026-02-19

All columns are nullable — no data migration needed.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'q2r3s4t5u6v7'
down_revision: Union[str, None] = 'p1q2r3s4t5u6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Document date
    op.add_column('paperless_audit_results', sa.Column('current_date', sa.String(), nullable=True))
    op.add_column('paperless_audit_results', sa.Column('suggested_date', sa.String(), nullable=True))

    # Missing metadata detection
    op.add_column('paperless_audit_results', sa.Column('missing_fields', sa.JSON(), nullable=True))

    # Duplicate detection
    op.add_column('paperless_audit_results', sa.Column('duplicate_group_id', sa.String(), nullable=True))
    op.add_column('paperless_audit_results', sa.Column('duplicate_score', sa.Float(), nullable=True))
    op.create_index('ix_paperless_audit_results_duplicate_group_id', 'paperless_audit_results', ['duplicate_group_id'])

    # Custom fields
    op.add_column('paperless_audit_results', sa.Column('current_custom_fields', sa.JSON(), nullable=True))
    op.add_column('paperless_audit_results', sa.Column('suggested_custom_fields', sa.JSON(), nullable=True))

    # Language detection
    op.add_column('paperless_audit_results', sa.Column('detected_language', sa.String(10), nullable=True))

    # Storage path
    op.add_column('paperless_audit_results', sa.Column('current_storage_path', sa.String(), nullable=True))
    op.add_column('paperless_audit_results', sa.Column('suggested_storage_path', sa.String(), nullable=True))

    # Content completeness
    op.add_column('paperless_audit_results', sa.Column('content_completeness', sa.Integer(), nullable=True))
    op.add_column('paperless_audit_results', sa.Column('completeness_issues', sa.String(), nullable=True))

    # Content hash for duplicate detection
    op.add_column('paperless_audit_results', sa.Column('content_hash', sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column('paperless_audit_results', 'content_hash')
    op.drop_column('paperless_audit_results', 'completeness_issues')
    op.drop_column('paperless_audit_results', 'content_completeness')
    op.drop_column('paperless_audit_results', 'suggested_storage_path')
    op.drop_column('paperless_audit_results', 'current_storage_path')
    op.drop_column('paperless_audit_results', 'detected_language')
    op.drop_column('paperless_audit_results', 'suggested_custom_fields')
    op.drop_column('paperless_audit_results', 'current_custom_fields')
    op.drop_index('ix_paperless_audit_results_duplicate_group_id', 'paperless_audit_results')
    op.drop_column('paperless_audit_results', 'duplicate_score')
    op.drop_column('paperless_audit_results', 'duplicate_group_id')
    op.drop_column('paperless_audit_results', 'missing_fields')
    op.drop_column('paperless_audit_results', 'suggested_date')
    op.drop_column('paperless_audit_results', 'current_date')
