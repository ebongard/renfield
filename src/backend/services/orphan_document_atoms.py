"""Find and purge document atoms whose document no longer exists (#1461).

Until #1461 ``RAGService.delete_document`` deleted the ``documents`` row but not
its ``kb_document`` atom, and (before that) not the facts' ``document_fact``
atoms either: the atom FK runs atom → source row, so nothing cascades from the
source side. Such an atom keeps a dangling ``source_id`` and its
``atom_explicit_grants`` rows. Readers JOIN ``documents``, so it is inert for
retrieval — but it is personal data that the deletion promised to remove and
it inflates atom counts.

An orphan is an atom of one of the two types whose source row is gone AND that
no live row still references through its ``atom_id`` (a mismatched ``source_id``
on an atom a document still uses is a different defect, never a purge). A
``__pending__`` placeholder (an in-flight create) is never an orphan here — the
placeholder reaper (``atom_service.reap_orphan_placeholder_atoms``) owns those,
with its age floor that keeps it from racing an upload.

Every purge goes through ``AtomPurgeService`` (the sanctioned path; the
grants go with the atom via FK CASCADE). Called by
``bin/purge_orphan_document_atoms.py`` — the owner runs it, dry run first.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import ATOM_TYPE_DOCUMENT_FACT, ATOM_TYPE_KB_DOCUMENT
from services.atom_purge_service import AtomPurgeService

PURGE_REASON = "document_deleted_orphan"

# atom type → the table its source_id points into (which also carries an
# ``atom_id`` column referencing the atom). Fixed literals: they are interpolated
# into the SQL below.
_SOURCES: dict[str, str] = {
    ATOM_TYPE_KB_DOCUMENT: "documents",
    ATOM_TYPE_DOCUMENT_FACT: "document_facts",
}


@dataclass
class OrphanPurgeResult:
    document_atoms: int = 0
    fact_atoms: int = 0
    grants: int = 0
    # One entry per purged atom: the atom row and its grants, so the run log
    # records exactly what was erased (ids, owner, tier, grantees — no content).
    purged: list[dict] = field(default_factory=list)


async def _find_orphans(db: AsyncSession) -> list[dict]:
    rows: list[dict] = []
    for atom_type, table in _SOURCES.items():
        result = await db.execute(
            text(
                "SELECT a.atom_id, a.atom_type, a.source_id, a.owner_user_id, "
                "       (a.policy->>'tier')::int AS tier, a.created_at "
                "FROM atoms a "
                "WHERE a.atom_type = :atom_type "
                r"  AND a.source_id NOT LIKE '\_\_pending\_\_%' ESCAPE '\' "
                f"  AND NOT EXISTS (SELECT 1 FROM {table} s WHERE s.id::text = a.source_id) "
                f"  AND NOT EXISTS (SELECT 1 FROM {table} r WHERE r.atom_id = a.atom_id) "
                "ORDER BY a.created_at, a.atom_id"
            ),
            {"atom_type": atom_type},
        )
        rows += [dict(r._mapping) for r in result]
    for row in rows:
        grants = await db.execute(
            text(
                "SELECT granted_to_user_id, permission_level FROM atom_explicit_grants "
                "WHERE atom_id = :a ORDER BY granted_to_user_id"
            ),
            {"a": row["atom_id"]},
        )
        row["grants"] = [dict(g._mapping) for g in grants]
    return rows


def _log_entry(orphan: dict) -> dict:
    created = orphan["created_at"]
    return {**orphan, "created_at": created.isoformat() if created else None}


async def purge_orphan_document_atoms(
    db: AsyncSession,
    *,
    dry_run: bool = True,
    on_before_commit: Callable[[list[dict]], Awaitable[None] | None] | None = None,
) -> OrphanPurgeResult:
    """Count (``dry_run``) or purge the orphaned document / fact atoms.

    The write run purges them all in one transaction and returns what it purged.
    ``on_before_commit`` receives the full list of what is about to be erased
    BEFORE the commit — the script writes its log there, so an erasure that
    commits is always named in a log (a failing callback aborts the purge).
    Idempotent: a second run finds nothing.
    """
    orphans = await _find_orphans(db)
    result = OrphanPurgeResult(
        document_atoms=sum(o["atom_type"] == ATOM_TYPE_KB_DOCUMENT for o in orphans),
        fact_atoms=sum(o["atom_type"] == ATOM_TYPE_DOCUMENT_FACT for o in orphans),
        grants=sum(len(o["grants"]) for o in orphans),
    )
    if dry_run or not orphans:
        return result

    entries = [_log_entry(o) for o in orphans]
    for orphan in orphans:
        await AtomPurgeService.purge(
            db, atom_id=orphan["atom_id"], reason=PURGE_REASON, commit=False,
        )
    if on_before_commit is not None:
        pending = on_before_commit(entries)
        if pending is not None:
            await pending
    await db.commit()

    from services.circle_resolver import CircleResolver

    for orphan in orphans:
        CircleResolver.invalidate_for_atom(orphan["atom_id"])
    result.purged = entries
    return result
