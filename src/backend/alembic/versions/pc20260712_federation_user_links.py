"""federation_user_links — cross-instance person map for person-scoped federation (F-ID-1)

Revision ID: pc20260712_federation_user_links
Revises: pc20260707_speaker_candidates
Create Date: 2026-07-12

Maps an opaque per-(peer, person) ``querier_ref`` token to a LOCAL user so a
federated query carrying that ref is served AS the mapped user (full circle
reach) instead of the peer-scoped public/guest fallback. Same table serves both
perspectives of a pairing (responder: ref->local user; asker: local user->ref).
Design: docs/design/federation-identity-mapping.md.

Idempotency: every DDL op is guarded — Base.metadata.create_all (backend boot)
creates this table for a new model, so re-running this migration must be a no-op
(same pattern as pc20260421_peer_users).
"""
import sqlalchemy as sa
from alembic import op

revision = "pc20260712_federation_user_links"
down_revision = "pc20260707_speaker_candidates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "federation_user_links" not in existing_tables:
        op.create_table(
            "federation_user_links",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "peer_id", sa.Integer(),
                sa.ForeignKey("peer_users.id", ondelete="CASCADE"), nullable=False,
            ),
            # Opaque per-(peer, person) token agreed at link time. NOT the raw
            # remote user id (avoids the multi-peer integer-collision class).
            sa.Column("querier_ref", sa.String(128), nullable=False),
            # SET NULL so deleting a local user demotes the link to unmapped
            # (fail-closed → fallback) instead of blocking the delete.
            sa.Column(
                "local_user_id", sa.Integer(),
                sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
            ),
            sa.Column(
                "created_by", sa.Integer(),
                sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
            ),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )

    def _has_idx(idx_name: str) -> bool:
        if "federation_user_links" not in existing_tables:
            return False
        return idx_name in {ix["name"] for ix in inspector.get_indexes("federation_user_links")}

    if not _has_idx("uq_fed_user_links_peer_ref"):
        op.create_index(
            "uq_fed_user_links_peer_ref", "federation_user_links",
            ["peer_id", "querier_ref"], unique=True,
        )
    if not _has_idx("ix_fed_user_links_peer_local"):
        op.create_index(
            "ix_fed_user_links_peer_local", "federation_user_links",
            ["peer_id", "local_user_id"],
        )
    if not _has_idx("ix_federation_user_links_peer_id"):
        op.create_index("ix_federation_user_links_peer_id", "federation_user_links", ["peer_id"])


def downgrade() -> None:
    op.drop_index("ix_federation_user_links_peer_id", table_name="federation_user_links")
    op.drop_index("ix_fed_user_links_peer_local", table_name="federation_user_links")
    op.drop_index("uq_fed_user_links_peer_ref", table_name="federation_user_links")
    op.drop_table("federation_user_links")
