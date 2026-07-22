"""meeting_speaker_fingerprints.person_name — §2 Track A merge-on-enroll

Revision ID: pc20260722d_fingerprint_person_name
Revises: pc20260722b_meeting_fingerprints
Create Date: 2026-07-22

Additive nullable ``person_name`` on ``meeting_speaker_fingerprints`` — the
human-confirmed real name attached when someone relabels the fingerprint in any
meeting (merge-on-enroll), owner-scoped (NOT the global Speaker pool). Chains
after the fingerprint table (pc20260722b), so a clean linear branch chain.

Idempotent: ``Base.metadata.create_all`` adds the column on a fresh install, so
the op is inspector-guarded (the fingerprint-table pattern). Fully transactional.
"""
import sqlalchemy as sa
from alembic import op

revision = "pc20260722d_fingerprint_person_name"
down_revision = "pc20260722b_meeting_fingerprints"
branch_labels = None
depends_on = None

_TABLE = "meeting_speaker_fingerprints"
_COLUMN = "person_name"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return  # fresh install without the table yet; create_all owns it
    cols = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _COLUMN not in cols:
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(100), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE in inspector.get_table_names():
        cols = {c["name"] for c in inspector.get_columns(_TABLE)}
        if _COLUMN in cols:
            op.drop_column(_TABLE, _COLUMN)
