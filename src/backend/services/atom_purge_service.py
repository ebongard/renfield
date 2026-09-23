"""AtomPurgeService — GDPR Art. 17 atom deletion with audit-trail preservation.

Single entry point for deleting atoms when ``wb_field_provenance`` may
hold snapshots that must outlive the atom for legally-required audit.

The procedure (atomic per atom_id):
    1. SELECT legal_hold=TRUE rows from wb_field_provenance for the atom.
    2. INSERT them into wb_field_provenance_archive (atom_id stripped).
    3. DELETE FROM atoms WHERE atom_id = ? — FK CASCADE wipes
       wb_field_provenance, kg_entities, document_chunks, etc.
       Since auth-on §8.1 that "etc." includes WHOLE user-facing rows:
       `conversations` (and every message under it) and `meetings` (with its
       audio + transcript pointers) carry the same CASCADE. That is the point,
       not an accident — an Art. 17 erasure that left the conversation standing
       would not be an erasure — but the blast radius is larger than it was.
    4. COMMIT.

Why this isn't a database trigger:
    - Portable across postgres, MySQL, MSSQL, sqlite (the trigger DDL
      would be three different dialects).
    - Testable in pure Python without spinning up postgres.
    - The behavior is auditable in code, not buried in plpgsql.

Direct ``DELETE FROM atoms`` calls bypass this service and are blocked
by ``tests/backend/test_no_direct_atom_delete.py``. Routes that need to
delete atoms must call ``AtomPurgeService.purge(...)``.

Usage:
    await AtomPurgeService.purge(
        session,
        atom_id="atom-uuid-here",
        reason="gdpr_art17_request",  # short stable identifier
    )
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Atom, WBFieldProvenance, WBFieldProvenanceArchive

# Valid archive reasons. Open enum (lint-checked, not DB-enforced)
# so callers know the standard values without breaking on new ones.
ARCHIVE_REASON_GDPR_PURGE: Final = "gdpr_purge"
ARCHIVE_REASON_USER_REQUEST: Final = "user_request"
ARCHIVE_REASON_CLEANUP: Final = "cleanup"


class AtomPurgeService:
    """Sole authorized path for deleting atoms."""

    @staticmethod
    async def purge(session: AsyncSession, *, atom_id: str, reason: str) -> int:
        """Archive legal_hold provenance + delete the atom.

        Returns the count of provenance rows moved to the archive.
        Raises if the atom doesn't exist (caller's bug, not ours to mask).
        """
        if not atom_id:
            raise ValueError("atom_id is required")
        if not reason:
            raise ValueError("reason is required for audit trail")

        # 1. Find all legal_hold rows for this atom.
        hold_rows = (
            await session.execute(
                select(WBFieldProvenance).where(
                    WBFieldProvenance.atom_id == atom_id,
                    WBFieldProvenance.legal_hold.is_(True),
                )
            )
        ).scalars().all()

        archived_at = datetime.now(UTC).replace(tzinfo=None)

        # 2. Copy them to the archive. atom_id is preserved as
        # original_atom_id for forensic traceability, but the FK is
        # severed — the archive does not reference atoms.
        if hold_rows:
            archive_rows = [
                {
                    "original_atom_id": row.atom_id,
                    "source_type": row.source_type,
                    "source_id": row.source_id,
                    "field_path": row.field_path,
                    "snapshot_value_json": row.snapshot_value_json,
                    "fetched_at": row.fetched_at,
                    "req_id": row.req_id,
                    "archived_at": archived_at,
                    "archive_reason": reason,
                }
                for row in hold_rows
            ]
            await session.execute(
                insert(WBFieldProvenanceArchive.__table__), archive_rows
            )

        # 3. Delete the atom. CASCADE wipes wb_field_provenance (including
        # the legal_hold rows we just copied — that's fine, the archive
        # has them now), wb_retrospective_annotation, document_chunks.atom_id,
        # kg_entities.atom_id, conversation_memories.atom_id, etc.
        await session.execute(delete(Atom).where(Atom.atom_id == atom_id))

        await session.commit()
        return len(hold_rows)
