"""Placement v2 calibration in tables instead of JSON settings (#1551, stage 2 part B).

``presence_receivers`` holds the per-receiver numbers (offset, in-room bound,
cutoff), ``presence_zones`` + ``presence_zone_rooms`` the open-plan zones. Until
now both lived in ``PRESENCE_PLACEMENT_V2_RECEIVERS`` / ``…_ZONES`` (JSON in the
ConfigMap). The tables start EMPTY: on its first start with empty tables the
backend copies the JSON values in (``presence_calibration.load_calibration``),
from then on the tables are the source and the JSON is ignored with a WARNING.
No data is moved here, so the migration needs no knowledge of the settings.

Revision ID: pc20261125_presence_calibration
Revises: pc20261124_kg_edge_tier_cap
Create Date: 2026-11-25
"""
import sqlalchemy as sa
from alembic import op

revision: str = "pc20261125_presence_calibration"
down_revision: str | None = "pc20261124_kg_edge_tier_cap"
branch_labels: None = None
depends_on: None = None


def _has_table(name: str) -> bool:
    """True when the table exists already (a fresh install's ``create_all``
    built it from the models). Qualified with ``current_schema()``, as in
    ``pc20261111_push_subscriptions``: the inspector's default schema would
    look at ``public`` instead of the schema the migration runs in."""
    return bool(op.get_bind().execute(
        sa.text("SELECT to_regclass(quote_ident(current_schema()) || '.' || :t) IS NOT NULL"),
        {"t": name},
    ).scalar())


def upgrade() -> None:
    if not _has_table("presence_receivers"):
        op.create_table(
            "presence_receivers",
            sa.Column("satellite_id", sa.String(100), primary_key=True),
            sa.Column("offset_db", sa.Float(), nullable=False),
            sa.Column("in_room_dbm", sa.Float(), nullable=False),
            sa.Column("cutoff_dbm", sa.Float(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column(
                "updated_by_user_id", sa.Integer(),
                sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
            ),
            sa.CheckConstraint(
                "in_room_dbm > cutoff_dbm", name="ck_presence_receivers_bound_above_cutoff"
            ),
        )
    if not _has_table("presence_zones"):
        op.create_table(
            "presence_zones",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(100), nullable=False, unique=True),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
    if not _has_table("presence_zone_rooms"):
        op.create_table(
            "presence_zone_rooms",
            sa.Column(
                "room_id", sa.Integer(),
                sa.ForeignKey("rooms.id", ondelete="CASCADE"), primary_key=True,
            ),
            sa.Column(
                "zone_id", sa.Integer(),
                sa.ForeignKey("presence_zones.id", ondelete="CASCADE"), nullable=False,
            ),
        )
        op.create_index(
            "ix_presence_zone_rooms_zone_id", "presence_zone_rooms", ["zone_id"]
        )


def downgrade() -> None:
    for table in ("presence_zone_rooms", "presence_zones", "presence_receivers"):
        if _has_table(table):
            op.drop_table(table)
