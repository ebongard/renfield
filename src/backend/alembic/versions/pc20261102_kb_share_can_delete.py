"""kb_shares.can_delete: deleting in a shared KB is a separate switch of the owner.

Until now ``check_kb_access`` derived the right to delete from the share LEVEL --
first by accident from READ (``perm_levels.get("delete", 1)``), then briefly from
ADMIN. Deleting destroys the owner's data, so it is no level: the KB owner (or
``kb.all``) grants it explicitly per share. ``can_delete`` defaults to FALSE and
is NOT backfilled -- today's admin grantees lose delete on purpose.

Only the KB owner or ``kb.all`` may set or clear it (``api/routes/knowledge.py``
share route); an admin grantee keeps managing shares but never this switch.

Downgrade: the column is dropped; how many shares carried the switch is logged
(counts only). The old code had no such switch -- nothing else to restore.

Fresh installs: the baseline builds the CURRENT model, which already declares the
column -- both directions are guarded.

Revision ID: pc20261102_kb_share_can_delete
Revises: pc20261101_kb_shares
Create Date: 2026-11-02
"""
import sqlalchemy as sa
from alembic import op

revision: str = "pc20261102_kb_share_can_delete"
down_revision: str | None = "pc20261101_kb_shares"
branch_labels: None = None
depends_on: None = None

TABLE = "kb_shares"
COLUMN = "can_delete"


def _column_exists(bind) -> bool:
    return bool(bind.execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = :t AND column_name = :c)"
    ), {"t": TABLE, "c": COLUMN}).scalar())


def upgrade() -> None:
    bind = op.get_bind()
    if _column_exists(bind):
        return
    op.add_column(
        TABLE,
        sa.Column(COLUMN, sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not _column_exists(bind):
        return
    granted = bind.execute(sa.text(f"SELECT COUNT(*) FROM {TABLE} WHERE {COLUMN}")).scalar()
    if granted:
        from loguru import logger
        logger.warning(
            f"pc20261102_kb_share_can_delete downgrade: {granted} share(s) carried the "
            f"delete switch; it is dropped with the column (the old code has none)."
        )
    op.drop_column(TABLE, COLUMN)
