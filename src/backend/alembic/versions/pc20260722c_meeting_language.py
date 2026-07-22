"""meetings.language — per-meeting ASR language (§2 language fix)

Revision ID: pc20260722c_meeting_language
Revises: pc20260722_user_token_epoch
Create Date: 2026-07-22

Additive nullable ``language`` column on ``meetings``. The meeting ASR path
hardcoded the household default (``de``), so English recordings were transcribed
as hallucinated German — this column carries the caller's per-meeting choice
(NULL = voice-server default, "auto" = whisper auto-detect, "en"/"de" = forced)
through the worker to ``/transcribe-meeting``.

Idempotent: ``Base.metadata.create_all`` (backend boot) adds this column on a
fresh install, so the op is inspector-guarded (the notes/fingerprint pattern).
Fully transactional.
"""
import sqlalchemy as sa
from alembic import op

revision = "pc20260722c_meeting_language"
down_revision = "pc20260722_user_token_epoch"
branch_labels = None
depends_on = None

_TABLE = "meetings"
_COLUMN = "language"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return  # fresh install without the meetings table yet; create_all owns it
    cols = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _COLUMN not in cols:
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(8), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE in inspector.get_table_names():
        cols = {c["name"] for c in inspector.get_columns(_TABLE)}
        if _COLUMN in cols:
            op.drop_column(_TABLE, _COLUMN)
