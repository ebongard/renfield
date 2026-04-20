"""Add peer_users table for federation pairing (F2 of v2 federation)

Revision ID: pc20260421_peer_users
Revises: pc20260420_circles_v1
Create Date: 2026-04-21

One row per paired remote Renfield peer. Ed25519 `remote_pubkey` (64-char
hex) is the stable identity; display_name is local-cosmetic. transport_config
carries {endpoint_url, transport, tls_fingerprint, relay_via}.

Idempotency: every DDL op is guarded — if the table was created by
Base.metadata.create_all on a dev box (same pattern that bit us on
.159 in pc20260420), re-running this is a no-op.
"""
import sqlalchemy as sa
from alembic import op


# revision identifiers
revision = "pc20260421_peer_users"
down_revision = "pc20260420_circles_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "peer_users" not in existing_tables:
        op.create_table(
            "peer_users",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("circle_owner_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("remote_pubkey", sa.String(64), nullable=False),
            sa.Column("remote_display_name", sa.String(255), nullable=False),
            sa.Column("remote_user_id", sa.Integer(), nullable=True),
            sa.Column("transport_config", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("paired_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("last_seen_at", sa.DateTime(), nullable=True),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
        )

    def _has_idx(idx_name: str) -> bool:
        if "peer_users" not in existing_tables:
            return False  # fresh table, no indexes yet
        return idx_name in {ix["name"] for ix in inspector.get_indexes("peer_users")}

    if not _has_idx("uq_peer_users_owner_pubkey"):
        op.create_index(
            "uq_peer_users_owner_pubkey",
            "peer_users",
            ["circle_owner_id", "remote_pubkey"],
            unique=True,
        )
    if not _has_idx("idx_peer_users_pubkey"):
        op.create_index("idx_peer_users_pubkey", "peer_users", ["remote_pubkey"])
    if not _has_idx("ix_peer_users_circle_owner_id"):
        op.create_index("ix_peer_users_circle_owner_id", "peer_users", ["circle_owner_id"])
    if not _has_idx("ix_peer_users_revoked_at"):
        op.create_index("ix_peer_users_revoked_at", "peer_users", ["revoked_at"])


def downgrade() -> None:
    # Drop indexes first to avoid dependency errors.
    op.drop_index("ix_peer_users_revoked_at", table_name="peer_users")
    op.drop_index("ix_peer_users_circle_owner_id", table_name="peer_users")
    op.drop_index("idx_peer_users_pubkey", table_name="peer_users")
    op.drop_index("uq_peer_users_owner_pubkey", table_name="peer_users")
    op.drop_table("peer_users")
