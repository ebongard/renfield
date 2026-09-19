"""room_output_devices: per-device TTS sound profile.

A spoken answer is synthesized once and played on very different speakers. The
household voice carries ~80 % of its energy between 80 and 300 Hz; a satellite's
small speaker cannot reproduce that, so it sounds balanced there, while a hi-fi
system plays it in full and the same file sounds flat and muffled. The fix is an
equalizer applied per OUTPUT DEVICE — hence a column here, next to ``tts_volume``.

``tts_eq_profile`` holds the NAME of a profile defined in
``ha_glue/services/tts_equalizer.py`` (NULL = off). A name rather than filter
parameters: a second profile is a code entry, never another migration.

Additive + fully transactional. No backfill and no server default — NULL is the
correct value for every existing row (behaviour unchanged until an admin opts a
device in), and a row written by an older pod during a rolling deploy is valid.

Revision ID: pc20260920_output_tts_eq
Revises: pc20260912_taskalert
"""
from alembic import op
import sqlalchemy as sa

revision = "pc20260920_output_tts_eq"
down_revision = "pc20260912_taskalert"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "room_output_devices",
        sa.Column("tts_eq_profile", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("room_output_devices", "tts_eq_profile")
