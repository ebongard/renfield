"""Add speaker_vocabulary_corpus + speaker_vocabulary tables.

Phase B-3 follow-up: per-user frequency-ranked vocabulary for STT bias.
- `speaker_vocabulary_corpus`: raw confirmed-speaker transcripts. Privacy
  tier defaults to 0 (self) — never leaks across users.
- `speaker_vocabulary`: computed term frequencies, periodically rebuilt
  by the batch tokenizer.

Revision ID: a0b1c2d3e4f5
Revises: z9a0b1c2d3e4
Create Date: 2026-05-02
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "a0b1c2d3e4f5"
down_revision = "z9a0b1c2d3e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "speaker_vocabulary_corpus",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=10), nullable=False, server_default="de"),
        sa.Column("circle_tier", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now(), index=True),
    )

    op.create_table(
        "speaker_vocabulary",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("term", sa.String(length=100), nullable=False),
        sa.Column("frequency", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("language", sa.String(length=10), nullable=False, server_default="de"),
        sa.Column("circle_tier", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("last_updated", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "term", "language", name="uq_speaker_vocab_user_term_lang"),
    )
    op.create_index(
        "ix_speaker_vocab_user_lang_freq",
        "speaker_vocabulary",
        ["user_id", "language", sa.text("frequency DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_speaker_vocab_user_lang_freq", table_name="speaker_vocabulary")
    op.drop_table("speaker_vocabulary")
    op.drop_table("speaker_vocabulary_corpus")
