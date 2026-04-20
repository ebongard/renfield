"""Add federation_query_log table (F4d of v2 federation)

Revision ID: pc20260422_federation_audit
Revises: pc20260421_peer_users
Create Date: 2026-04-22

Asker-side audit row for each federated query. One row per
`FederationQueryAsker.query_peer` lifecycle. Kept separate from
`peer_users` (audit-vs-config separation, different retention).

FK on peer_users uses ON DELETE SET NULL so unpairing doesn't
cascade-delete historical rows; we also denormalize the pubkey +
display name as snapshots for cases where the peer row was deleted
before we render the audit page.

Idempotency: guarded like pc20260421 — tolerates Base.metadata
create_all having already made the table on a dev box.
"""
import sqlalchemy as sa
from alembic import op


# revision identifiers
revision = "pc20260422_federation_audit"
down_revision = "pc20260421_peer_users"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "federation_query_log" not in existing_tables:
        op.create_table(
            "federation_query_log",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id"),
                nullable=False,
            ),
            sa.Column(
                "peer_user_id",
                sa.Integer(),
                sa.ForeignKey("peer_users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("peer_pubkey_snapshot", sa.String(64), nullable=False),
            sa.Column("peer_display_name_snapshot", sa.String(255), nullable=False),
            sa.Column("request_id", sa.String(64), nullable=True),
            sa.Column("query_text", sa.Text(), nullable=False),
            sa.Column(
                "initiated_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("finalized_at", sa.DateTime(), nullable=True),
            sa.Column("final_status", sa.String(16), nullable=False),
            sa.Column(
                "verified_signature",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column("answer_excerpt", sa.Text(), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
        )

    def _has_idx(idx_name: str) -> bool:
        if "federation_query_log" not in existing_tables:
            return False
        return idx_name in {
            ix["name"] for ix in inspector.get_indexes("federation_query_log")
        }

    # Index choices:
    #   idx_fed_audit_user_initiated covers the primary list query
    #     (user_id = ? ORDER BY initiated_at DESC) AND any WHERE user_id = ?
    #     by leading-column rule. No separate single-column user_id index.
    #   idx_fed_audit_user_peer covers the ?peer= filter in the API.
    #   ix_federation_query_log_request_id serves debug lookups by
    #     request_id (cross-user — admin-tool query).
    # The retention prune (WHERE initiated_at < cutoff) has no user_id
    # prefix and will scan; at expected row counts (<10k per user,
    # 90-day retention) the scan cost is negligible. If that changes,
    # add a dedicated single-column `initiated_at` index then.
    if not _has_idx("ix_federation_query_log_request_id"):
        op.create_index(
            "ix_federation_query_log_request_id",
            "federation_query_log",
            ["request_id"],
        )
    if not _has_idx("idx_fed_audit_user_initiated"):
        op.create_index(
            "idx_fed_audit_user_initiated",
            "federation_query_log",
            ["user_id", "initiated_at"],
        )
    if not _has_idx("idx_fed_audit_user_peer"):
        op.create_index(
            "idx_fed_audit_user_peer",
            "federation_query_log",
            ["user_id", "peer_pubkey_snapshot"],
        )


def downgrade() -> None:
    op.drop_index("idx_fed_audit_user_peer", table_name="federation_query_log")
    op.drop_index("idx_fed_audit_user_initiated", table_name="federation_query_log")
    op.drop_index("ix_federation_query_log_request_id", table_name="federation_query_log")
    op.drop_table("federation_query_log")
