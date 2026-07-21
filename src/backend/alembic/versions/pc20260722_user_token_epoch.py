"""users.token_epoch — session-revocation epoch (security audit H3/H4)

Revision ID: pc20260722_user_token_epoch
Revises: pc20260721_notes_embedding
Create Date: 2026-07-20

Adds a NOT NULL ``users.token_epoch`` integer (server_default "0"). Access +
refresh JWTs carry this value as an ``epoch`` claim; ``get_current_user`` / the
refresh route reject a token whose epoch is older than the DB value, so bumping
it (password change / admin revoke-all) instantly invalidates every outstanding
token for that user. Default 0 for all existing rows, and pre-existing tokens
without the claim read as 0 → 0 >= 0 keeps them valid until natural expiry (no
mass logout on deploy).

Idempotency: guarded by an inspector check — Base.metadata.create_all (backend
boot) already adds this column for the model, so re-running is a no-op. Fully
transactional (transaction_per_migration).
"""
import sqlalchemy as sa
from alembic import op

revision = "pc20260722_user_token_epoch"
down_revision = "pc20260721_notes_embedding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("users")}
    if "token_epoch" not in cols:
        op.add_column(
            "users",
            sa.Column(
                "token_epoch",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )


def downgrade() -> None:
    op.drop_column("users", "token_epoch")
