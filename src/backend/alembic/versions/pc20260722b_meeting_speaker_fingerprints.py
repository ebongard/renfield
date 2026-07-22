"""meeting_speaker_fingerprints — §2 redesign Track A (Phase 1)

Revision ID: pc20260722b_meeting_fingerprints
Revises: pc20260722_user_token_epoch
Create Date: 2026-07-22

Adds ``meeting_speaker_fingerprints`` — owner-scoped, circle-tiered ECAPA
centroids that give a diarized speaker cluster a STABLE cross-meeting identity
("Speaker A1B2") without knowing who they are, optionally bound to a real
``Speaker`` on human enroll (models.database.MeetingSpeakerFingerprint). This is
the schema half of Track A; the matching/merge logic + the calibration-spike
go/no-go gate ship separately (docs/design/meeting-kg-and-speaker-identity.md).

Idempotent: ``Base.metadata.create_all`` (backend boot) creates this table for a
fresh install, so every DDL op is inspector/IF-EXISTS-guarded (the
pc20260720_notes + pc20260721 patterns). The ``centroid`` pgvector column + its
halfvec HNSW index + the per-owner unique-label index are Postgres-only (the
sqlite test harness has no pgvector; ``centroid`` is Text there and matching
falls back to base64 cosine). Fully transactional except the CONCURRENTLY index
builds (autocommit_block, allowed under transaction_per_migration).
"""
import sqlalchemy as sa
from alembic import op

revision = "pc20260722b_meeting_fingerprints"
down_revision = "pc20260722_user_token_epoch"
branch_labels = None
depends_on = None

_TABLE = "meeting_speaker_fingerprints"
_HNSW_INDEX = "idx_msf_centroid_hnsw"
_UNIQUE_LABEL = "uq_msf_owner_label"
# ECAPA-TDNN embedding dimension (voice-server speaker_service).
_DIM = 192


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    dialect = bind.dialect.name if bind is not None else "postgresql"
    tables = set(inspector.get_table_names())

    if _TABLE not in tables:
        # ``centroid`` (pgvector) is added below via raw SQL — keeps create_table
        # portable (sqlite has no vector type) and mirrors create_all parity.
        op.create_table(
            _TABLE,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column(
                "owner_user_id", sa.Integer,
                sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
            ),
            sa.Column("label", sa.String(64), nullable=False),
            sa.Column("centroid_b64", sa.Text, nullable=False),
            sa.Column(
                "speaker_id", sa.Integer,
                sa.ForeignKey("speakers.id", ondelete="SET NULL"), nullable=True,
            ),
            sa.Column("sample_count", sa.Integer, nullable=False, server_default="1"),
            sa.Column("circle_tier", sa.Integer, nullable=False, server_default="2"),
            sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
        )

    # FK indexes — names match SQLAlchemy's ``index=True`` auto-naming so a
    # fresh-install create_all (which built them) and this migration never make
    # a duplicate; guarded so each is created at most once.
    inspector = sa.inspect(bind)  # re-read after a possible create
    idx = {ix["name"] for ix in inspector.get_indexes(_TABLE)}
    for name, cols in (
        (f"ix_{_TABLE}_owner_user_id", ["owner_user_id"]),
        (f"ix_{_TABLE}_speaker_id", ["speaker_id"]),
    ):
        if name not in idx:
            op.create_index(name, _TABLE, cols)

    if dialect != "postgresql":
        return  # sqlite: no pgvector; centroid stays Text, no HNSW/unique index.

    # pgvector centroid column (create_all already adds it on a fresh DB) + HNSW
    # (halfvec cast — regular vector HNSW caps at 2000 dims; halfvec → 4096).
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS centroid vector({_DIM})")
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_HNSW_INDEX}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY {_HNSW_INDEX} ON {_TABLE} "
            f"USING hnsw ((centroid::halfvec({_DIM})) halfvec_cosine_ops)"
        )
        # One label per owner (service get-or-create is the primary guard; this
        # is the DB backstop). NULL-owner auth-off rows are distinct in Postgres.
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_UNIQUE_LABEL}")
        op.execute(
            f"CREATE UNIQUE INDEX CONCURRENTLY {_UNIQUE_LABEL} "
            f"ON {_TABLE} (owner_user_id, label)"
        )
    op.execute(f"ANALYZE {_TABLE}")


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name if bind is not None else "postgresql"
    if dialect == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_UNIQUE_LABEL}")
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_HNSW_INDEX}")
    op.drop_index(f"ix_{_TABLE}_speaker_id", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_owner_user_id", table_name=_TABLE)
    op.drop_table(_TABLE)
