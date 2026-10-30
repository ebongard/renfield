"""federation_query_log: record the direction and the identity basis.

Until today the audit log recorded status, signature, query text and an excerpt
of the answer -- but NOT which identity a federated answer rested on. For a
distinction that decides the REACH of an access, that is too little:

  mapped   -> the far side answers with the linked person's own full reach
  fallback -> it answers as "the other instance", only at the granted tier

On 2026-10-29 the first successful federated turn in the project's history ran.
Which of the two cases it was could NOT be established afterwards -- only
inferred from the same `querier_ref` on both sides and from the code. The
pending record that carries `mapped` lives in memory and is gone after the
turn.

🛑 And: so far only the ASKING side wrote at all (`mcp_client.py`, a single
caller). But the decision about the identity is made by the ANSWERING side --
so that side has to record it. A note on the asking side would be the far
side's claim about its own access decision. Hence `direction`: the same table
now carries both sides.

`user_id` stays NOT NULL and means throughout "the local person whose reach was
used": on the asking side the person asking, on the answering side the linked
person (mapped) or the owner of the pairing (fallback). Both cases have one.

Revision ID: pc20261029_audit_basis
Revises: pc20261027_peer_tier
Create Date: 2026-10-29
"""
import sqlalchemy as sa
from alembic import op

revision: str = "pc20261029_audit_basis"
down_revision: str | None = "pc20261027_peer_tier"
branch_labels: None = None
depends_on: None = None


def _has_column(table: str, column: str) -> bool:
    """Development checkouts run `create_all` and already have the column."""
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    if not _has_column("federation_query_log", "direction"):
        op.add_column(
            "federation_query_log",
            # Existing rows are all from the asking side -- there was no
            # other. So `asked` is not a default but the truth.
            sa.Column("direction", sa.String(8), nullable=False,
                      server_default="asked"),
        )
    if not _has_column("federation_query_log", "identity_basis"):
        op.add_column(
            "federation_query_log",
            # Nullable: existing rows do not KNOW, and that should be
            # visible. A default would pass ignorance off as fact.
            sa.Column("identity_basis", sa.String(16), nullable=True),
        )


def downgrade() -> None:
    # 🛑 The way back LOSES the record of which identity was answered on --
    # it is held nowhere else. "Before" is reconstructible from "after" here
    # (the columns simply go away), but the answering side's log rows stay
    # behind as `asked` and are then indistinguishable from genuine asking-side
    # rows.
    if _has_column("federation_query_log", "identity_basis"):
        op.drop_column("federation_query_log", "identity_basis")
    if _has_column("federation_query_log", "direction"):
        op.drop_column("federation_query_log", "direction")
