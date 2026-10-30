"""voice second factor: per-user consent column

Revision ID: pc20260927_voice2fa
Revises: pc20260926_baseline
Create Date: 2026-09-27

An ECAPA voiceprint is biometric data (Art. 9 GDPR). That a LOGIN demands it is a
per-person consent and not a configuration -- hence a column on `users` and not a
flag in the ConfigMap. `speaker_id` was not fit for it: that link was made for
speaker recognition, not as consent to logging in.

🛑 Default `false` with a `server_default`, so the flag-off path stays byte-identical
and an existing account does NOT get the hurdle without being asked.

🛑 This migration MUST run before the rollout (`--migrate`): the ORM selects every
column, so a new pod before the migration fails every query on `users` -- which means
failing to log in.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "pc20260927_voice2fa"
down_revision: str | None = "pc20260926_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "users"
_COLUMN = "voice_second_factor_enabled"


def _has_column(conn) -> bool:
    # 🛑 `table_schema = current_schema()` is NOT decoration. Without the filter
    # the query sees EVERY `users` table in the database -- in an installation
    # with several schemas therefore a foreign one too, and the migration
    # returned silently without doing anything. That is exactly what happened in
    # the migration test (a throwaway schema next to `public`), and in production
    # it only goes unnoticed because there is one schema there. A check that
    # lives off how many schemas happen to exist is not a check.
    return bool(conn.execute(sa.text("""
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = :t AND column_name = :c
    """), {"t": _TABLE, "c": _COLUMN}).scalar())


def upgrade() -> None:
    conn = op.get_bind()
    # Idempotent: a fresh install already has the column from `create_all`
    # (see `pc20260926_schema_baseline`), and this migration must not fail
    # there.
    if _has_column(conn):
        return
    op.add_column(_TABLE, sa.Column(
        _COLUMN, sa.Boolean(), nullable=False, server_default=sa.text("false"),
    ))


def downgrade() -> None:
    # The way back is exercised, not asserted: the column really disappears.
    # Whoever rolls it back loses the consents -- which is intended in a
    # downgrade, because without the column there is no second factor that could
    # read them.
    conn = op.get_bind()
    if not _has_column(conn):
        return
    op.drop_column(_TABLE, _COLUMN)
