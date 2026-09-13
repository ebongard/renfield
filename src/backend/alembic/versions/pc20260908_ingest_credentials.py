"""ingest_credentials — per-integration machine credentials for the push routes.

Revision ID: pc20260908_ingest_credentials
Revises: pc20260901_doc_content_emb
Create Date: 2026-09-08 00:00:00.000000

Replaces the single shared SystemSetting token (``folder_ingest.token`` /
``email_ingest.token``) with one credential per pushing integration. The shared
token was stored in the CLEAR, could not be revoked per client, and left the
backend unable to tell WHICH client pushed — the last of which is exactly what
server-authoritative sphere routing needs.

Mirrors ``pc20260624_satellite_enrollment``: bcrypt ``token_hash`` only,
``revoked_at`` + ``is_enabled`` for independent revocation, and
``last_authenticated_at`` so retiring the legacy token is an observation rather
than a guess.

ADDITIVE ONLY. The legacy SystemSetting tokens keep working; nothing is dropped
here. The legacy row is removed in a later migration, once
``last_authenticated_at`` shows no client still using it.

See docs/design/ingest-credentials.md.
"""
import sqlalchemy as sa
from alembic import op

revision = "pc20260908_ingest_credentials"
down_revision = "pc20260901_doc_content_emb"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ingest_credentials",
        sa.Column("id", sa.Integer(), primary_key=True),
        # Rides INSIDE the token (rfi.<client_id>.<secret>) so verification is
        # one lookup + one bcrypt, not a bcrypt round per credential.
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("route", sa.String(length=32), nullable=False),
        sa.Column("token_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "created_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("rotated_at", sa.DateTime(), nullable=True),
        sa.Column("last_authenticated_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column(
            "is_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
    )
    # UNIQUE, not just indexed: client_id is parsed out of the presented token
    # and must resolve to exactly one row, or verification is ambiguous.
    op.create_index(
        "ix_ingest_credentials_client_id",
        "ingest_credentials",
        ["client_id"],
        unique=True,
    )
    op.create_index("ix_ingest_credentials_route", "ingest_credentials", ["route"])


def downgrade() -> None:
    op.drop_index("ix_ingest_credentials_route", table_name="ingest_credentials")
    op.drop_index("ix_ingest_credentials_client_id", table_name="ingest_credentials")
    op.drop_table("ingest_credentials")
