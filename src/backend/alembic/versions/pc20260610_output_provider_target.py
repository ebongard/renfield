"""room_output_devices.(output_provider, output_target_id) — generic output routing

Revision ID: pc20260610_output_target
Revises: pc20260609_oblig_cal
Create Date: 2026-06-07

Additive step of the generic output-provider architecture
(docs/design/output-providers.md). Adds the ``(output_provider, output_target_id)``
pair that replaces the three brand-specific identity columns
(``renfield_device_id`` / ``ha_entity_id`` / ``dlna_renderer_name``), and backfills
it from them. The legacy columns are KEPT this migration — reads dual-read (prefer
the new pair, fall back to the old columns), so nothing breaks during the soak.
The destructive DROP COLUMN of the three legacy columns is a separate follow-up
PR after prod soak (TODOS.md), because a feature flag can't protect a column drop.

Fully transactional: plain add_column + a single backfill UPDATE, no CONCURRENTLY.
"""
from alembic import op
import sqlalchemy as sa


revision = "pc20260610_output_target"
down_revision = "pc20260609_oblig_cal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "room_output_devices",
        sa.Column("output_provider", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "room_output_devices",
        sa.Column("output_target_id", sa.String(length=255), nullable=True),
    )
    # Backfill the pair from the legacy three columns. Exactly one of them is set
    # per row (a CHECK the app enforces), so CASE + COALESCE is unambiguous.
    op.execute(
        """
        UPDATE room_output_devices
        SET output_provider = CASE
                WHEN renfield_device_id IS NOT NULL THEN 'renfield'
                WHEN ha_entity_id      IS NOT NULL THEN 'homeassistant'
                WHEN dlna_renderer_name IS NOT NULL THEN 'dlna'
            END,
            output_target_id = COALESCE(renfield_device_id, ha_entity_id, dlna_renderer_name)
        WHERE output_provider IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("room_output_devices", "output_target_id")
    op.drop_column("room_output_devices", "output_provider")
