"""users.is_device_account — a device identity is not a person (D-4b).

The household auth-on cutover gives satellites (and later the kiosk) their own
account so an unrecognised voice can still read household knowledge and act,
without every voice in the room writing into one person's memory. The flag is
the contract — no code keys on a username.

Additive and nullable-free: Boolean NOT NULL with a server default, so an old
pod writing a row before the rollout stays valid, and every existing account
reads as "a person".

Revision ID: pc20260921b_user_is_device_account
Revises: pc20260921_tool_outcome_system_bucket
"""
import sqlalchemy as sa
from alembic import op

revision = "pc20260921b_user_is_device_account"
down_revision = "pc20260921_tool_outcome_system_bucket"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "is_device_account",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "is_device_account")
