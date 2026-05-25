"""Wissensbasis longitudinal substrate — field provenance, event log, retrospective annotations.

Revision ID: pc20260511_wb_long
Revises: a0b1c2d3e4f5
Create Date: 2026-05-11 18:15:23.000000

Adds three platform-level tables that turn the existing in-memory
``FieldProvenance`` accumulator (currently a ContextVar in Reva's truth
engine) into a durable substrate:

    wb_field_provenance
        Snapshot-at-observation rows for every field the agent reads from
        an external system. Each row pins the exact JSON value the source
        returned at ``fetched_at``, so audit replay can answer
        "what did we know about X on date Y" even after the upstream value
        has changed or the upstream record has been deleted.

    wb_event_log
        Ordered event stream extracted from tool results that carry
        activity logs / phase histories. Substrate for vN+1 process
        conformance evaluation; ships empty until ingestion lands.

    wb_retrospective_annotation
        Per-(atom, key, value) annotations for the "what went well /
        what should improve" retrospective loop. Substrate for vN+1
        retrospective aggregation; ships empty until capture surface lands.

Application-layer purge + legal_hold discrimination:

    ``wb_field_provenance.atom_id`` references ``atoms.atom_id`` with
    ``ON DELETE CASCADE`` — the FK alone wipes every row when an atom is
    deleted. The legal_hold discrimination is enforced in Python by
    ``services/atom_purge_service.py``, which:

      1. Copies legal_hold=TRUE rows from ``wb_field_provenance`` to
         ``wb_field_provenance_archive`` (atom_id stripped, snapshot kept).
      2. Issues ``DELETE FROM atoms WHERE atom_id = ?`` which cascades
         through the FK and wipes the live wb_field_provenance rows
         (including the legal_hold ones — but they're already preserved
         in the archive).

    Net behavior:
      - legal_hold = TRUE  → snapshot moved to archive, survives forever
      - legal_hold = FALSE → snapshot deleted by CASCADE with the atom

    Why application-layer instead of a DB trigger:
      - Portable across postgres, MySQL, MSSQL, sqlite (no dialect-specific
        trigger DDL).
      - Testable via standard unit tests, no postgres fixture required.
      - The lint ``tests/backend/test_no_direct_atom_delete.py`` blocks
        any code path that bypasses the service.

Index strategy:

    Primary B-tree (source_type, source_id, field_path) covers focus-resolver
    hot path. Partial index on fetched_at WHERE > NOW() - 90d covers freshness
    queries. Audit queries (rare, against older data) fall back to sequential
    scan on the B-tree. Estimated 8-12 GB at 100k atoms × 30 fields × 3 snapshots
    average.

Why this lives in Renfield, not Reva:

    Provenance is generic platform substrate. Any plugin that observes
    external state needs the same primitives — Reva is the first consumer
    but not the architectural owner. Locating the schema here:
      - lets future plugins read provenance without a Reva dependency,
      - keeps the alembic chain unified (no Reva-owned alembic),
      - matches the existing pattern for atoms / kg_entities (platform tables,
        Reva consumes via SQLAlchemy ORM).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "pc20260511_wb_long"
down_revision = "a0b1c2d3e4f5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # =========================================================================
    # wb_field_provenance — snapshot-at-observation per (source, field, time)
    # =========================================================================
    if "wb_field_provenance" not in existing_tables:
        op.create_table(
            "wb_field_provenance",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column(
                "atom_id",
                sa.String(36),
                sa.ForeignKey("atoms.atom_id", ondelete="CASCADE"),
                nullable=True,
            ),
            sa.Column("source_type", sa.String(32), nullable=False),
            sa.Column("source_id", sa.String(512), nullable=False),
            sa.Column("field_path", sa.String(256), nullable=False),
            sa.Column("snapshot_value_json", sa.JSON(), nullable=False),
            sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("req_id", sa.String(8), nullable=True),
            sa.Column("legal_hold", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.CheckConstraint(
                "source_type IN ('release','jira','confluence','itsm','memory','derived')",
                name="ck_wb_fp_source_type",
            ),
            sa.CheckConstraint("length(source_id) > 0", name="ck_wb_fp_source_id_nonempty"),
            sa.CheckConstraint("length(field_path) > 0", name="ck_wb_fp_field_path_nonempty"),
        )

    # Primary lookup index for focus-resolver and audit-replay hot paths.
    if not _has_idx(inspector, "wb_field_provenance", "idx_wb_fp_lookup"):
        op.create_index(
            "idx_wb_fp_lookup",
            "wb_field_provenance",
            ["source_type", "source_id", "field_path"],
        )

    # atom_id index: backs both the FK's ON DELETE CASCADE on atom DELETE
    # and AtomPurgeService.purge()'s SELECT-by-atom_id when copying
    # legal_hold rows to the archive. Without this index, every atom
    # deletion becomes a seq scan over an 8-12 GB table — bulk GDPR purges
    # would stall for minutes per atom. Cardinality is high (one atom maps
    # to ~30-90 provenance rows), so a B-tree is the right shape.
    if not _has_idx(inspector, "wb_field_provenance", "idx_wb_fp_atom_id"):
        op.create_index("idx_wb_fp_atom_id", "wb_field_provenance", ["atom_id"])

    # Index for freshness/staleness queries on fetched_at. Originally written
    # as a partial index with `WHERE fetched_at > NOW() - INTERVAL '90 days'`,
    # but postgres rejects that — partial-index predicates must be IMMUTABLE
    # and NOW() is STABLE. The rolling-window intent was also moot: NOW() is
    # evaluated once at CREATE INDEX time, so the cutoff would have been a
    # static deploy-time snapshot, not a rolling window. A full B-tree on
    # fetched_at serves the same freshness queries and the table ships empty.
    if not _has_idx(inspector, "wb_field_provenance", "idx_wb_fp_recent"):
        op.create_index("idx_wb_fp_recent", "wb_field_provenance", ["fetched_at"])

    # =========================================================================
    # wb_field_provenance_archive — destination for legal_hold rows that
    # outlive their atom. Same shape as wb_field_provenance minus atom_id
    # (the atom is gone by the time a row lands here). The archive table
    # is append-only; nothing ever deletes from it except a future GDPR
    # legal-hold-release path (out of scope here).
    # =========================================================================
    if "wb_field_provenance_archive" not in existing_tables:
        op.create_table(
            "wb_field_provenance_archive",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("original_atom_id", sa.String(36), nullable=True),  # at archive time
            sa.Column("source_type", sa.String(32), nullable=False),
            sa.Column("source_id", sa.String(512), nullable=False),
            sa.Column("field_path", sa.String(256), nullable=False),
            sa.Column("snapshot_value_json", sa.JSON(), nullable=False),
            sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("req_id", sa.String(8), nullable=True),
            sa.Column("archived_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("archive_reason", sa.String(64), nullable=False),  # e.g. 'gdpr_purge', 'cleanup'
            sa.CheckConstraint(
                "source_type IN ('release','jira','confluence','itsm','memory','derived')",
                name="ck_wb_fpa_source_type",
            ),
        )

    if not _has_idx(inspector, "wb_field_provenance_archive", "idx_wb_fpa_lookup"):
        op.create_index(
            "idx_wb_fpa_lookup",
            "wb_field_provenance_archive",
            ["source_type", "source_id", "field_path"],
        )

    # =========================================================================
    # wb_event_log — ordered event stream from tool result histories
    # =========================================================================
    if "wb_event_log" not in existing_tables:
        op.create_table(
            "wb_event_log",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("source_type", sa.String(32), nullable=False),
            sa.Column("source_id", sa.String(512), nullable=False),
            sa.Column("event_type", sa.String(64), nullable=False),
            sa.Column("event_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("payload_json", sa.JSON(), nullable=False),
            sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("req_id", sa.String(8), nullable=True),
            sa.CheckConstraint(
                "source_type IN ('release','jira','confluence','itsm')",
                name="ck_wb_el_source_type",
            ),
            sa.UniqueConstraint(
                "source_type", "source_id", "event_type", "event_at",
                name="uq_wb_el_dedup",
            ),
        )

    if not _has_idx(inspector, "wb_event_log", "idx_wb_el_source_order"):
        op.create_index(
            "idx_wb_el_source_order",
            "wb_event_log",
            ["source_type", "source_id", "event_at"],
        )

    # =========================================================================
    # wb_retrospective_annotation — per-atom retrospective notes
    # =========================================================================
    if "wb_retrospective_annotation" not in existing_tables:
        op.create_table(
            "wb_retrospective_annotation",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column(
                "atom_id",
                sa.String(36),
                sa.ForeignKey("atoms.atom_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("annotation_key", sa.String(64), nullable=False),
            sa.Column("annotation_value", sa.Text(), nullable=False),
            sa.Column("author_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "annotation_key IN ('went_well','improvement','blocker','followup')",
                name="ck_wb_ra_key",
            ),
        )

    if not _has_idx(inspector, "wb_retrospective_annotation", "idx_wb_ra_atom"):
        op.create_index(
            "idx_wb_ra_atom",
            "wb_retrospective_annotation",
            ["atom_id"],
        )


def downgrade() -> None:
    op.drop_table("wb_retrospective_annotation")
    op.drop_table("wb_event_log")
    op.drop_table("wb_field_provenance_archive")
    op.drop_table("wb_field_provenance")


def _has_idx(inspector, table: str, index_name: str) -> bool:
    try:
        return any(idx["name"] == index_name for idx in inspector.get_indexes(table))
    except Exception:
        return False
