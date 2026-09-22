"""conversations + meetings become atoms with a circle tier (auth-on §8.1).

A kitchen is a multi-person room: A asks at the satellite, B follows up, and
that is ONE thread. With ownership as equality B's follow-up either failed or
opened a context-less new conversation, and no member saw the room history in
their list. Conversations get `circle_tier` + an atom, so `circle_sql` can let
tier reach decide instead.

Meetings ride along: their tier has defaulted to 2 ("a meeting is a shared
artifact") since they were introduced, while their read paths kept filtering on
owner equality — the declared tier was never true. The atom is what makes it so.

Additive and reversible. `atom_id` is NULLABLE on both tables, unlike on notes:
rows that predate any owner have nobody to hang an atom on, and an atom's owner
is NOT NULL. A row gets its atom when it gets an owner — at creation, or in the
P2 backfill. The backfill below therefore covers only rows that already have
one; an ownerless row is refused under auth-on regardless.

Revision ID: pc20260922_conv_meeting_atoms
Revises: pc20260921b_user_is_device_account
"""
import sqlalchemy as sa
from alembic import op

revision = "pc20260922_conv_meeting_atoms"
down_revision = "pc20260921b_user_is_device_account"
branch_labels = None
depends_on = None


def _backfill_atoms(table: str, atom_type: str, owner_col: str, tier_expr: str) -> None:
    """One atoms row per owned source row, then point the source row at it.

    Idempotent: only rows whose `atom_id IS NULL` are touched, and the INSERT is
    keyed on the same (atom_type, source_table, source_id) triple the service
    uses, so a re-run after a partial failure adds nothing twice.
    """
    op.execute(
        sa.text(
            f"""
            INSERT INTO atoms (atom_id, atom_type, source_table, source_id,
                               owner_user_id, policy, created_at, updated_at)
            SELECT gen_random_uuid()::text, :atom_type, :src, t.id::text,
                   t.{owner_col}, json_build_object('tier', {tier_expr}),
                   NOW(), NOW()
              FROM {table} t
             WHERE t.{owner_col} IS NOT NULL
               AND t.atom_id IS NULL
               AND NOT EXISTS (
                     SELECT 1 FROM atoms a
                      WHERE a.atom_type = :atom_type
                        AND a.source_table = :src
                        AND a.source_id = t.id::text
                   )
            """
        ).bindparams(atom_type=atom_type, src=table)
    )
    op.execute(
        sa.text(
            f"""
            UPDATE {table} t
               SET atom_id = a.atom_id
              FROM atoms a
             WHERE a.atom_type = :atom_type
               AND a.source_table = :src
               AND a.source_id = t.id::text
               AND t.atom_id IS NULL
            """
        ).bindparams(atom_type=atom_type, src=table)
    )


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("circle_tier", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "conversations",
        sa.Column("atom_id", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "ix_conversations_atom_id", "conversations", ["atom_id"], unique=False
    )
    op.create_foreign_key(
        "fk_conversations_atom_id", "conversations", "atoms",
        ["atom_id"], ["atom_id"], ondelete="CASCADE",
    )

    op.add_column(
        "meetings",
        sa.Column("atom_id", sa.String(length=36), nullable=True),
    )
    op.create_index("ix_meetings_atom_id", "meetings", ["atom_id"], unique=False)
    op.create_foreign_key(
        "fk_meetings_atom_id", "meetings", "atoms",
        ["atom_id"], ["atom_id"], ondelete="CASCADE",
    )

    # Existing rows keep exactly the reach they have today: a conversation is
    # tier 0 (personal) until somebody raises it — the design's deliberate
    # exception for legacy rows — while a meeting keeps the tier it already
    # carries in its own column.
    _backfill_atoms("conversations", "conversation", "user_id", "0")
    _backfill_atoms("meetings", "meeting", "owner_user_id", "t.circle_tier")


def downgrade() -> None:
    # ORDER MATTERS, and getting it wrong destroys user data: the `atom_id`
    # columns carry ON DELETE CASCADE, so deleting the atoms while those
    # columns still reference them takes every conversation and meeting with
    # them. Drop the columns FIRST, then remove the now-unreferenced registry
    # rows. (Found by running the round trip: the first cut deleted the atoms
    # up front and the conversation count went from 2 to 1.)
    op.drop_constraint("fk_meetings_atom_id", "meetings", type_="foreignkey")
    op.drop_index("ix_meetings_atom_id", table_name="meetings")
    op.drop_column("meetings", "atom_id")

    op.drop_constraint("fk_conversations_atom_id", "conversations", type_="foreignkey")
    op.drop_index("ix_conversations_atom_id", table_name="conversations")
    op.drop_column("conversations", "atom_id")
    op.drop_column("conversations", "circle_tier")

    # Now nothing references them: remove the registry rows this migration
    # created, so a re-upgrade rebuilds them instead of finding stale ones.
    op.execute(
        sa.text("DELETE FROM atoms WHERE atom_type IN ('conversation', 'meeting')")
    )
