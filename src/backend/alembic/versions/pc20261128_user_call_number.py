"""users.call_number + call_number_verified_at: the user's own number Renfield may call (#1683, Telefonie Phase 4).

Additive, nullable, no backfill: NULL = Renfield never calls this user.

Revision ID: pc20261128_user_call_number
Revises: pc20261127b_telephony_faxes
"""
import sqlalchemy as sa
from alembic import op

revision = "pc20261128_user_call_number"
down_revision = "pc20261127b_telephony_faxes"
branch_labels = None
depends_on = None


def _has_column(name: str) -> bool:
    insp = sa.inspect(op.get_bind())
    return name in {c["name"] for c in insp.get_columns("users")}


def upgrade() -> None:
    if not _has_column("call_number"):
        op.add_column("users", sa.Column("call_number", sa.String(20), nullable=True))
    if not _has_column("call_number_verified_at"):
        op.add_column("users", sa.Column("call_number_verified_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    if _has_column("call_number_verified_at"):
        op.drop_column("users", "call_number_verified_at")
    if _has_column("call_number"):
        op.drop_column("users", "call_number")
