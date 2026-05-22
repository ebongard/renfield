"""Schema + service regression tests for the wissensbasis longitudinal substrate.

Four tables introduced by migration `pc20260511_wb_long`:
  - wb_field_provenance          (live snapshots)
  - wb_field_provenance_archive  (legal_hold rows that outlived their atom)
  - wb_event_log
  - wb_retrospective_annotation

Legal_hold discrimination is enforced by ``services/atom_purge_service.py``
in pure Python (portable across postgres / MySQL / MSSQL / sqlite). The
``test_atom_purge_archives_legal_hold_rows`` test below exercises the
full flow against an in-memory sqlite engine — no postgres required.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session

from models.database import (
    Atom,
    Base,
    Role,
    User,
    WBEventLog,
    WBFieldProvenance,
    WBFieldProvenanceArchive,
    WBRetrospectiveAnnotation,
)


# ---------------------------------------------------------------------------
# Unit: model declarations match design
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_wb_field_provenance_atom_id_fk_is_cascade():
    """FK pattern: ON DELETE CASCADE. The archive table preserves legal_hold."""
    fk = next(iter(WBFieldProvenance.__table__.c.atom_id.foreign_keys))
    assert fk.ondelete == "CASCADE"
    assert fk.column.table.name == "atoms"


@pytest.mark.unit
def test_wb_field_provenance_atom_id_is_nullable():
    """atom_id is nullable — supports rows orphaned by edge cases."""
    assert WBFieldProvenance.__table__.c.atom_id.nullable is True


@pytest.mark.unit
def test_wb_field_provenance_atom_id_has_index():
    """idx_wb_fp_atom_id covers the purge service's SELECT-by-atom-id path."""
    idx_columns = {idx.name: [c.name for c in idx.columns] for idx in WBFieldProvenance.__table__.indexes}
    assert "idx_wb_fp_atom_id" in idx_columns
    assert idx_columns["idx_wb_fp_atom_id"] == ["atom_id"]


@pytest.mark.unit
def test_wb_field_provenance_lookup_index_is_composite():
    """The focus-resolver hot path queries (source_type, source_id, field_path)."""
    idx_names = {idx.name: [c.name for c in idx.columns] for idx in WBFieldProvenance.__table__.indexes}
    assert "idx_wb_fp_lookup" in idx_names
    assert idx_names["idx_wb_fp_lookup"] == ["source_type", "source_id", "field_path"]


@pytest.mark.unit
def test_wb_field_provenance_legal_hold_defaults_false():
    col = WBFieldProvenance.__table__.c.legal_hold
    assert col.nullable is False
    assert col.default is not None or col.server_default is not None


@pytest.mark.unit
def test_wb_archive_has_no_fk_to_atoms():
    """Archive must NOT reference atoms — the atom is gone by the time we land here."""
    fks = list(WBFieldProvenanceArchive.__table__.c.original_atom_id.foreign_keys)
    assert fks == []


@pytest.mark.unit
def test_wb_event_log_dedup_unique_constraint():
    ucs = [c for c in WBEventLog.__table__.constraints if c.__class__.__name__ == "UniqueConstraint"]
    assert any(c.name == "uq_wb_el_dedup" for c in ucs)


@pytest.mark.unit
def test_wb_retrospective_annotation_cascade_on_atom_delete():
    fk = next(iter(WBRetrospectiveAnnotation.__table__.c.atom_id.foreign_keys))
    assert fk.ondelete == "CASCADE"


@pytest.mark.unit
def test_wb_tables_registered_on_metadata():
    table_names = set(Base.metadata.tables.keys())
    assert "wb_field_provenance" in table_names
    assert "wb_field_provenance_archive" in table_names
    assert "wb_event_log" in table_names
    assert "wb_retrospective_annotation" in table_names


# ---------------------------------------------------------------------------
# Service: AtomPurgeService archives legal_hold + CASCADEs the rest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_atom_purge_archives_legal_hold_rows():
    """End-to-end: purge an atom with one legal_hold=TRUE and one =FALSE row.

    After purge:
      - legal_hold=TRUE  row moved to wb_field_provenance_archive
      - legal_hold=FALSE row deleted by CASCADE
      - atoms row gone
      - returned count == 1 (the archived row)
    """
    from services.atom_purge_service import AtomPurgeService, ARCHIVE_REASON_GDPR_PURGE

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSession(engine) as s:
        now = datetime.now(UTC).replace(tzinfo=None)
        # Need a user for atom FK.
        s.add(Role(id=1, name="member"))
        await s.flush()
        s.add(User(id=1, username="t", email="t@t", password_hash="x", role_id=1))
        await s.flush()
        s.add(Atom(
            atom_id="atom-1", atom_type="test", source_table="tests",
            source_id="t1", owner_user_id=1, policy={"tier": 0},
            created_at=now, updated_at=now,
        ))
        await s.flush()
        s.add_all([
            WBFieldProvenance(
                atom_id="atom-1", source_type="release", source_id="REL-100",
                field_path="status", snapshot_value_json={"v": "ACTIVE"},
                fetched_at=now, legal_hold=True,
            ),
            WBFieldProvenance(
                atom_id="atom-1", source_type="release", source_id="REL-100",
                field_path="owner", snapshot_value_json={"v": "alice"},
                fetched_at=now, legal_hold=False,
            ),
        ])
        await s.commit()

        archived_count = await AtomPurgeService.purge(
            s, atom_id="atom-1", reason=ARCHIVE_REASON_GDPR_PURGE,
        )

    assert archived_count == 1

    async with AsyncSession(engine) as s:
        live = (await s.execute(select(WBFieldProvenance))).scalars().all()
        archive = (await s.execute(select(WBFieldProvenanceArchive))).scalars().all()
        atoms = (await s.execute(select(Atom))).scalars().all()

    assert live == []
    assert len(archive) == 1
    assert archive[0].original_atom_id == "atom-1"
    assert archive[0].snapshot_value_json == {"v": "ACTIVE"}
    assert archive[0].archive_reason == ARCHIVE_REASON_GDPR_PURGE
    assert atoms == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_atom_purge_with_no_legal_hold_rows():
    """Purge an atom whose provenance is all legal_hold=FALSE → archive stays empty."""
    from services.atom_purge_service import AtomPurgeService

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSession(engine) as s:
        now = datetime.now(UTC).replace(tzinfo=None)
        s.add(Role(id=1, name="member"))
        await s.flush()
        s.add(User(id=1, username="t", email="t@t", password_hash="x", role_id=1))
        await s.flush()
        s.add(Atom(
            atom_id="atom-2", atom_type="test", source_table="tests",
            source_id="t2", owner_user_id=1, policy={"tier": 0},
            created_at=now, updated_at=now,
        ))
        await s.flush()
        s.add(WBFieldProvenance(
            atom_id="atom-2", source_type="release", source_id="REL-200",
            field_path="status", snapshot_value_json={"v": "DONE"},
            fetched_at=now, legal_hold=False,
        ))
        await s.commit()

        archived = await AtomPurgeService.purge(s, atom_id="atom-2", reason="cleanup")

    assert archived == 0

    async with AsyncSession(engine) as s:
        assert (await s.execute(select(WBFieldProvenanceArchive))).scalars().all() == []
