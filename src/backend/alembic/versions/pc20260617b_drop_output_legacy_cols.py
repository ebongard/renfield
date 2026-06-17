"""room_output_devices: DROP the three legacy brand-identity columns

Revision ID: pc20260617b_drop_outlegacy
Revises: pc20260617_msg_search
Create Date: 2026-06-17

Destructive cleanup step of the generic output-provider architecture
(docs/design/output-providers.md, rollout phase 5). The additive migration
``pc20260610_output_target`` added the ``(output_provider, output_target_id)``
pair and backfilled it from the three brand-specific identity columns; reads
have dual-read ever since (prefer the pair, fall back to the legacy columns).
That schema has now soaked in prod (verified: every ``room_output_devices`` row
is dual-written, ``output_provider`` + ``output_target_id`` NOT NULL, zero
legacy-only rows), so the legacy columns are dead and safe to drop.

This migration drops:
  * ``renfield_device_id`` (+ its FK ``room_output_devices_renfield_device_id_fkey``
    to ``room_devices.device_id``)
  * ``ha_entity_id``
  * ``dlna_renderer_name``

The ``RoomOutputDevice`` model + every reader (``OutputRoutingService``,
``AudioOutputService``, ``InternalToolService`` shims, the rooms API) are
migrated in the same PR to read ONLY the ``(output_provider, output_target_id)``
pair via the ``target_type`` / ``target_id`` / ``is_*_device`` properties.

This is the irreversible step deliberately split out of the feature PR — a flag
can't protect a ``DROP COLUMN``.

Downgrade RE-ADDS the three columns (nullable) + the FK so the schema shape is
recoverable, but DOES NOT restore the dropped data: the brand-identity values
are gone after the drop. A downgraded row still resolves correctly because the
runtime reads the ``(output_provider, output_target_id)`` pair, which is
untouched — the re-added columns simply come back NULL. (Reverting the code to a
dual-read build after a downgrade would read those columns as NULL and fall
through to the pair, which is still correct.)

Transaction model: ``env.py`` runs ``transaction_per_migration=True``. This
migration is fully transactional — three ``drop_column`` and, on downgrade,
``add_column`` + ``create_foreign_key``. No ``CONCURRENTLY`` / no autocommit
block needed; a failure rolls the whole migration back cleanly.
"""
from alembic import op
import sqlalchemy as sa


revision = "pc20260617b_drop_outlegacy"
down_revision = "pc20260617_msg_search"
branch_labels = None
depends_on = None


_FK_NAME = "room_output_devices_renfield_device_id_fkey"


def upgrade() -> None:
    # Dropping renfield_device_id drops its FK with it on Postgres; drop the FK
    # explicitly first so the intent is clear and the migration is portable.
    # Guard with IF EXISTS (op.drop_constraint has no if_exists kwarg on older
    # alembic) so the migration is re-runnable if the FK is already gone.
    op.execute(
        f"ALTER TABLE room_output_devices DROP CONSTRAINT IF EXISTS {_FK_NAME}"
    )
    op.drop_column("room_output_devices", "renfield_device_id")
    op.drop_column("room_output_devices", "ha_entity_id")
    op.drop_column("room_output_devices", "dlna_renderer_name")


def downgrade() -> None:
    # Re-add the columns (nullable) and the FK so the schema shape is recoverable.
    # NOTE: the dropped DATA is not restored — these come back NULL. The runtime
    # reads the (output_provider, output_target_id) pair, which is untouched, so
    # rows still resolve correctly.
    op.add_column(
        "room_output_devices",
        sa.Column("renfield_device_id", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "room_output_devices",
        sa.Column("ha_entity_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "room_output_devices",
        sa.Column("dlna_renderer_name", sa.String(length=255), nullable=True),
    )
    op.create_foreign_key(
        _FK_NAME,
        "room_output_devices",
        "room_devices",
        ["renfield_device_id"],
        ["device_id"],
    )
