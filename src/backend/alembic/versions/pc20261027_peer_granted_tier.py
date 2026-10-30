"""peer_users.granted_tier -- a peer's tier belongs to the pairing (F-ID-3, BL-0135).

Until today `pairing_service._upsert_circle_membership` wrote the REMOTE user
id into `circle_memberships.member_user_id` -- a column whose foreign key
points at the LOCAL `users.id`. Two consequences, both observed live on
2026-10-27:

  - x-idra (user 1 only) got `responder_user_id=2` at completion -> the foreign
    key broke, the pairing died with a 500.
  - The household (users 1..8) had accepted `initiator_user_id=1` before -> the
    number existed, so NO error -- instead an access row `owner=2 member=1` for
    its own local account 1 rather than for the peer. The same number, a
    different person.

The tier therefore moves to the pairing. After that there is no place left
where a foreign number is taken for a local person.

🛑 NO backfill from the old memberships. Existing peers get 4, the NARROWEST
tier. Whether a row `(owner=1, member=1, tier=2)` is a real membership or a
pairing leftover is recorded nowhere -- and precisely that indistinguishability
IS the defect. Making it the basis of a data migration would mean carrying it
forward. Whoever wants to keep an existing pairing open sets the tier again
visibly instead of inheriting it.

🛑 The ladder runs against intuition: `granted_tier <= atom.circle_tier`, so a
SMALLER number grants MORE. The default of 4 is therefore the safe one, not the
generous one.

Revision ID: pc20261027_peer_tier
Revises: pc20260929_kg_validity
Create Date: 2026-10-27
"""
import sqlalchemy as sa
from alembic import op

revision: str = "pc20261027_peer_tier"
down_revision: str | None = "pc20260929_kg_validity"
branch_labels: None = None
depends_on: None = None


def _has_column(table: str, column: str) -> bool:
    """Development checkouts run `Base.metadata.create_all` and already have
    the column. Without this guard the migration fails there."""
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    if _has_column("peer_users", "granted_tier"):
        return
    op.add_column(
        "peer_users",
        sa.Column(
            "granted_tier",
            sa.Integer(),
            nullable=False,
            # Existing rows inherit the narrowest tier -- see the docstring.
            server_default="4",
        ),
    )


def downgrade() -> None:
    # 🛑 The way back LOSES the grants: before this they lived in
    # `circle_memberships`, and this migration writes nothing back there. After
    # a downgrade every pairing stands without a tier and the peer-scoped
    # filter falls back to "public only" -- fail-closed, but the tiers are gone
    # and have to be set again.
    if _has_column("peer_users", "granted_tier"):
        op.drop_column("peer_users", "granted_tier")
