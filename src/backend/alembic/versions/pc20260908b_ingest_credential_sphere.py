"""ingest_credentials: per-client sphere routing (owner / tier / kb).

Revision ID: pc20260908b_cred_sphere
Revises: pc20260908_ingest_credentials
Create Date: 2026-09-08 01:00:00.000000

Phase 4 of docs/design/ingest-credentials.md. Folder-ingest files everything into
ONE globally configured destination because it could not tell its clients apart.
Now that a push resolves to a credential, the destination can be a property of
WHO pushed — which is what email-ingest already does per mailbox.

All three columns are NULLABLE and NULL means "use the global
folder_ingest_* configuration", so existing credentials keep behaving exactly as
they do today. Nothing is backfilled.

The client never SENDS these. They are read from the authenticated credential
row, so a stolen token cannot pick an owner, raise a tier, or file into another
knowledge base.
"""
import sqlalchemy as sa
from alembic import op

revision = "pc20260908b_cred_sphere"
down_revision = "pc20260908_ingest_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ingest_credentials", sa.Column("owner", sa.String(length=100), nullable=True))
    op.add_column("ingest_credentials", sa.Column("tier", sa.SmallInteger(), nullable=True))
    op.add_column("ingest_credentials", sa.Column("kb_name", sa.String(length=200), nullable=True))


def downgrade() -> None:
    op.drop_column("ingest_credentials", "kb_name")
    op.drop_column("ingest_credentials", "tier")
    op.drop_column("ingest_credentials", "owner")
