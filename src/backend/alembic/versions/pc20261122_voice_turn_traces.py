"""voice_turn_traces -- one content-free row per voice turn end (#1613).

Backs ``GET /api/admin/voice-turns/stats``. Filled only while
``VOICE_TURN_TRACES_ENABLED`` is on; rows hold no user, no text and no audio
(device id, room, turn-end reason, recording length, the device's VAD verdict,
end-of-recording and session-end times, a resumed flag) and are purged after
``VOICE_TURN_TRACES_RETENTION_DAYS``. Written at the satellite SESSION end.
New table, no backfill.

Revision ID: pc20261122_voice_turn_traces
Revises: pc20261119_fk_rules_to_model
Create Date: 2026-11-22
"""
import sqlalchemy as sa
from alembic import op

revision: str = "pc20261122_voice_turn_traces"
down_revision: str | None = "pc20261119_fk_rules_to_model"
branch_labels: None = None
depends_on: None = None

TABLE = "voice_turn_traces"


def _has_table() -> bool:
    """A fresh install's baseline runs ``create_all`` from the CURRENT models and
    already built it. Qualified with current_schema(), as in
    ``pc20261110_routing_traces``."""
    return bool(op.get_bind().execute(sa.text(
        "SELECT to_regclass(quote_ident(current_schema()) || '.' || :t) IS NOT NULL"
    ), {"t": TABLE}).scalar())


def upgrade() -> None:
    if _has_table():
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer, primary_key=True),
        # End of the recording (audio_end), and the later of the session end
        # and the expected end of the answer's playback.
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("finished_at", sa.DateTime, nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("device_key", sa.String(64), nullable=True),
        sa.Column("room", sa.String(64), nullable=True),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("audio_seconds", sa.Float, nullable=True),
        sa.Column("vad_speech_seen", sa.Boolean, nullable=True),
        sa.Column("resumed", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("turn_score", sa.Float, nullable=True),
    )
    # Declared in the ORM as well, so a fresh install's create_all builds them.
    op.create_index("idx_voice_turn_traces_created", TABLE, ["created_at"])
    op.create_index("idx_voice_turn_traces_source_created", TABLE, ["source", "created_at"])


def downgrade() -> None:
    # The indexes go with the table.
    if _has_table():
        op.drop_table(TABLE)
