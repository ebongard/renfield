"""kb_shares: a knowledge-base share becomes a row of its own.

Until now a KB share existed ONLY as one ``atom_explicit_grants`` row per
document that was in the KB at share time (``kb_shares_service.share_kb``).
Consequences: sharing an EMPTY KB stored nothing, a document added later was
never covered (the grantee could not read their own upload into the shared
KB), and the share vanished with the KB's last document. The KB route ACL was
derived from the same grants, so the grantee of an empty KB could not even
upload into it.

``kb_shares (kb_id, user_id)`` is now the source of truth. It is evaluated at
query time (``services/circle_sql.py`` document filters,
``CircleResolver.can_access_atom``), so it covers every document in the KB.

The upgrade is SCHEMA ONLY -- no data is changed. The existing per-document grants stay and
still count (access and ACL = ``kb_shares`` OR a grant); the missing
``kb_shares`` rows are derived from them by the owner-run
``bin/backfill_kb_shares.py`` (dry run by default).

Downgrade: every share is written back as per-document grants (the old
representation) before the table is dropped; shares of EMPTY KBs cannot be
represented that way and are reported as lost (counts only).

Fresh installs: the baseline builds the CURRENT model, which already declares
the table -- both directions are guarded.

Revision ID: pc20261101_kb_shares
Revises: pc20261031_kb_name_per_owner
Create Date: 2026-11-01
"""
import sqlalchemy as sa
from alembic import op

revision: str = "pc20261101_kb_shares"
down_revision: str | None = "pc20261031_kb_name_per_owner"
branch_labels: None = None
depends_on: None = None

TABLE = "kb_shares"


def _table_exists(bind) -> bool:
    # Qualified with current_schema(): a bare to_regclass() would also find a
    # same-named table further down the search_path.
    return bool(bind.execute(sa.text(
        "SELECT to_regclass(quote_ident(current_schema()) || '.' || :t) IS NOT NULL"
    ), {"t": TABLE}).scalar())


def upgrade() -> None:
    bind = op.get_bind()
    if _table_exists(bind):
        return
    op.create_table(
        TABLE,
        sa.Column(
            "kb_id", sa.Integer(),
            sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"), primary_key=True,
        ),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("permission_level", sa.String(16), nullable=False),
        sa.Column("granted_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("granted_at", sa.DateTime(), nullable=False),
    )
    op.create_index("idx_kb_shares_user", TABLE, ["user_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind):
        return
    # A share made after the upgrade lives ONLY in kb_shares (share_kb no longer
    # writes per-document grants). Dropping the table bare would revoke it
    # silently, so convert every share back into the OLD representation first:
    # one grant per document currently in the KB, at the share's level. That is
    # exactly what the old code reads. What the old representation cannot hold
    # -- a share of a KB with no documents -- is lost, and said so (counts only).
    lost = bind.execute(sa.text(
        f"SELECT COUNT(*) FROM {TABLE} ks WHERE NOT EXISTS ("
        "  SELECT 1 FROM documents d JOIN atoms a "
        "    ON a.source_table = 'documents' AND a.source_id = d.id::text "
        "  WHERE d.knowledge_base_id = ks.kb_id)"
    )).scalar()
    written = bind.execute(sa.text(
        "INSERT INTO atom_explicit_grants "
        "  (atom_id, granted_to_user_id, permission_level, granted_by, granted_at) "
        "SELECT a.atom_id, ks.user_id, ks.permission_level, "
        # atom_explicit_grants.granted_by is NOT NULL; an auth-off share has none.
        "       COALESCE(ks.granted_by, kb.owner_id, ks.user_id), ks.granted_at "
        f"FROM {TABLE} ks "
        "JOIN knowledge_bases kb ON kb.id = ks.kb_id "
        "JOIN documents d ON d.knowledge_base_id = ks.kb_id "
        "JOIN atoms a ON a.source_table = 'documents' AND a.source_id = d.id::text "
        "ON CONFLICT (atom_id, granted_to_user_id) DO UPDATE "
        "  SET permission_level = EXCLUDED.permission_level"
    )).rowcount
    from loguru import logger
    logger.info(
        f"pc20261101_kb_shares downgrade: {written} per-document grant(s) written "
        f"from {TABLE}"
    )
    if lost:
        logger.warning(
            f"pc20261101_kb_shares downgrade: {lost} share(s) of knowledge bases "
            f"without documents cannot be represented as per-document grants and "
            f"are dropped with {TABLE}; re-share them after the KB has a document."
        )
    op.drop_index("idx_kb_shares_user", table_name=TABLE)
    op.drop_table(TABLE)
