"""kg_relations.tier_cap: an edge is never more open than its document (#1625).

Until now an edge extracted from a document took ``MIN(subject, object)`` and
nothing else. When both entities already existed at a more open tier, the edge
("who relates to whom", the private part of the document's knowledge) became
more open than the document it came from. Measured 2026-11-22: 10 edges on the
household since the auth-on cutover, 0 on xidra.

``tier_cap`` records the tier of the most private document that asserted the
edge; ``circle_tier = LEAST(subject, object, tier_cap)`` (``services/kg_edge_tier.py``).

Backfill: only edges created AFTER the household auth-on cutover
(``CUTOVER_AT``). The 3 486 document edges from before it were deliberately set
to tier 2 by cutover decision D-2a (``docs/design/household-auth-on-cutover.md``
§4.1) and stay uncapped: a cap on them would undo D-2a on the next entity tier
change. Measured gap on the household: the last over-tier edge before the cutover
was created 2026-09-08, the first after it 2026-10-11, and the backfill ran
2026-09-23 18:54 UTC. xidra has no over-tier edge at any time, so the boundary
changes nothing there; a fresh install has no data.

Revision ID: pc20261124_kg_edge_tier_cap
Revises: pc20261122_voice_turn_traces
Create Date: 2026-11-24
"""
from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision: str = "pc20261124_kg_edge_tier_cap"
down_revision: str | None = "pc20261122_voice_turn_traces"
branch_labels: None = None
depends_on: None = None

TABLE = "kg_relations"
COLUMN = "tier_cap"
# Start of the household cutover backfill (bin/backfill_household_tiers.py), UTC
# (the DB stores naive UTC). A datetime, not a string: asyncpg, which the
# migration job runs on, refuses a str for a timestamp parameter.
CUTOVER_AT = datetime(2026, 9, 23, 18, 54)


def _has_column(table: str, column: str) -> bool:
    """A fresh install's baseline runs ``create_all`` and already has the column."""
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    if not _has_column(TABLE, COLUMN):
        # Nullable, no default: NULL means "no document source", which is the
        # truth for every edge from a chat turn or a memory.
        op.add_column(TABLE, sa.Column(COLUMN, sa.Integer, nullable=True))

    bind = op.get_bind()
    changed = bind.execute(sa.text(
        "UPDATE kg_relations r "
        "SET tier_cap = d.circle_tier, circle_tier = LEAST(r.circle_tier, d.circle_tier) "
        "FROM documents d "
        "WHERE r.source_session_id = 'doc:' || d.id::text "
        "  AND r.tier_cap IS NULL "
        "  AND r.created_at >= :cutover "
        "RETURNING r.id, r.circle_tier"
    ), {"cutover": CUTOVER_AT}).fetchall()
    if changed:
        bind.execute(sa.text(
            "UPDATE atoms SET policy = json_build_object('tier', r.circle_tier), updated_at = NOW() "
            "FROM kg_relations r "
            "WHERE atoms.atom_type = 'kg_edge' AND atoms.source_id = r.id::text "
            "  AND r.id = ANY(:ids) "
            "  AND (atoms.policy->>'tier')::int IS DISTINCT FROM r.circle_tier"
        ), {"ids": [row.id for row in changed]})
    print(f"pc20261124_kg_edge_tier_cap: capped {len(changed)} document edge(s) created since {CUTOVER_AT}")


def downgrade() -> None:
    # 🛑 Only the column goes away. Edge tiers the upgrade LOWERED stay lowered:
    # the previous tier is not recorded anywhere and cannot be derived from the
    # current state, and the lowered one is the more private of the two.
    if _has_column(TABLE, COLUMN):
        op.drop_column(TABLE, COLUMN)
    print("pc20261124_kg_edge_tier_cap downgrade: tier_cap dropped; lowered edge tiers stay lowered")
