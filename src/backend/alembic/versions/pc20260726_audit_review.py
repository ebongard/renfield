"""paperless_audit_results: user review overlay (manual edits + field selection)

Revision ID: pc20260726_audit_review
Revises: pc20260722d_fingerprint_person_name
Create Date: 2026-07-26

Two additive nullable JSON columns on ``paperless_audit_results`` backing the
audit-flow review overlay:
  - ``user_overrides``  {field: edited_value} — manual edits to the suggested_*
    values (kept separate from suggested_* so the LLM proposal stays as
    provenance and "reset to suggestion" is trivial).
  - ``field_selection`` [field, ...] — which fields to apply; NULL = all changed
    (legacy behavior, byte-identical when no review was made).

Idempotent + inspector-guarded (``Base.metadata.create_all`` adds the columns on
a fresh install, so upgrade guards against them already existing). Fully
transactional.
"""
import sqlalchemy as sa
from alembic import op

revision = "pc20260726_audit_review"
down_revision = "pc20260722d_fingerprint_person_name"
branch_labels = None
depends_on = None

_TABLE = "paperless_audit_results"
_COLUMNS = ("user_overrides", "field_selection")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return  # fresh install without the table yet; create_all owns it
    cols = {c["name"] for c in inspector.get_columns(_TABLE)}
    for name in _COLUMNS:
        if name not in cols:
            op.add_column(_TABLE, sa.Column(name, sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns(_TABLE)}
    for name in _COLUMNS:
        if name in cols:
            op.drop_column(_TABLE, name)
