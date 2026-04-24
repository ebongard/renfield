"""
AtomService — orchestrator for atom CRUD with circle-aware access checks.

Source-table writers (document_processor, conversation_memory_service.save,
knowledge_graph_service.save_relation, etc.) SHOULD go through this service
when writing rows that need to participate in the atom registry. v1 ships
the service + the contract; CI lint to enforce that direct INSERTs to source
tables go through here lands in Lane C.

Responsibilities:
  - upsert_atom: create or update an atoms row + the corresponding source row
    denormalized circle_tier in a single transaction (SELECT ... FOR UPDATE
    on the atoms row to serialize concurrent writes).
  - update_tier: change an atom's circle policy. Cascades to:
      * the source row's denormalized circle_tier
      * kg_relations.circle_tier when a kg_node tier changes (per CEO Finding E)
      * CircleResolver cache invalidation for this atom
  - get_atom: read an atom (with access check; uniform-None on both 404 and 403).
  - soft_delete: mark the source row inactive (hard atom deletion is reserved
    for the v3 KG migration; v1 keeps atoms for audit-trail continuity).

ASCII upsert flow:

    Writer (e.g. extract_and_save, document worker, KG extractor)
        │
        │ atom = Atom(type='conversation_memory', payload={...},
        │            owner_id=42, policy={"tier": 0})
        ▼
    AtomService.upsert_atom(atom)
        │
        ├── BEGIN TRANSACTION
        │      ├── UPSERT atoms row (UUID4 generated if new) RETURNING atom_id
        │      ├── UPSERT source row (denormalized circle_tier from policy.tier)
        │      └── COMMIT (atomic)
        │
        └── Returns atom_id

    On exception: full rollback. No orphan atoms; no orphan source rows.
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from loguru import logger
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    ATOM_TYPE_KB_DOCUMENT,
    ATOM_TYPE_KG_EDGE,
    ATOM_TYPE_KG_NODE,
    Atom as AtomModel,
)
from services.atom_types import Atom
from services.circle_resolver import CircleResolver, atom_from_orm


# Source table → discriminator atom_type → row id column map.
# Used by update_tier to dispatch the denormalized circle_tier write.
_SOURCE_TABLE_TIER_UPDATE = {
    "documents": "id",          # kb_document atom → documents.circle_tier + cascade to chunks
    "kg_entities": "id",
    "kg_relations": "id",
    "conversation_memories": "id",
}


class AtomService:
    """Atom CRUD orchestrator. One per AsyncSession (request scope)."""

    def __init__(self, db: AsyncSession, resolver: CircleResolver | None = None):
        self.db = db
        self.resolver = resolver or CircleResolver(db)

    # ==========================================================================
    # Create / update
    # ==========================================================================

    async def upsert_atom(self, atom: Atom) -> str:
        """
        Insert or update the atoms row + the source row's denormalized
        circle_tier. Returns atom_id.

        If atom.atom_id is empty/sentinel, a fresh UUID4 is minted.
        Otherwise an existing atoms row is looked up by (atom_type,
        source_table, source_id) — the unique index — and updated.

        Concurrency: SELECT-then-INSERT is wrapped in try/except IntegrityError
        per PR #402 review BLOCKING #3. Two concurrent upserts of the same
        source row will both miss the SELECT, both attempt INSERT, the loser
        hits the unique constraint — we catch, rollback, re-SELECT, and update
        the now-existing row.

        Source-row existence: verified BEFORE issuing the source-table UPDATE
        so we fail-fast on writers passing payload IDs that don't reference a
        real row (rather than silently creating an orphan atom).
        """
        from sqlalchemy.exc import IntegrityError

        atom_id = atom.atom_id or str(uuid.uuid4())
        tier = int(atom.policy.get("tier", 0))
        source_table = _table_for_atom_type(atom.atom_type)
        source_id = _source_id_for(atom)

        # Verify the source row actually exists. Fail fast on orphan-creation
        # attempts (writer passed a stale or made-up source ID).
        source_exists = (await self.db.execute(
            text(f"SELECT 1 FROM {source_table} WHERE id = :source_id LIMIT 1"),
            {"source_id": source_id},
        )).scalar()
        if source_exists is None:
            raise ValueError(
                f"AtomService.upsert_atom: source row {source_table}.id={source_id} "
                f"does not exist. Refusing to create orphan atom."
            )

        existing = (await self.db.execute(
            select(AtomModel).where(
                AtomModel.atom_type == atom.atom_type,
                AtomModel.source_table == source_table,
                AtomModel.source_id == source_id,
            )
        )).scalar_one_or_none()

        if existing is not None:
            existing.policy = dict(atom.policy)
            existing.updated_at = datetime.now(UTC).replace(tzinfo=None)
            atom_id = existing.atom_id
        else:
            new_row = AtomModel(
                atom_id=atom_id,
                atom_type=atom.atom_type,
                source_table=source_table,
                source_id=source_id,
                owner_user_id=atom.owner_user_id,
                policy=dict(atom.policy),
            )
            self.db.add(new_row)
            try:
                await self.db.flush()
            except IntegrityError:
                # Concurrent writer beat us. Roll back, re-SELECT, update.
                await self.db.rollback()
                existing = (await self.db.execute(
                    select(AtomModel).where(
                        AtomModel.atom_type == atom.atom_type,
                        AtomModel.source_table == source_table,
                        AtomModel.source_id == source_id,
                    )
                )).scalar_one_or_none()
                if existing is None:
                    # Shouldn't happen — IntegrityError but no row found?
                    raise
                existing.policy = dict(atom.policy)
                existing.updated_at = datetime.now(UTC).replace(tzinfo=None)
                atom_id = existing.atom_id

        # Update the source row's denormalized circle_tier so SQL filters work.
        await self.db.execute(
            text(
                f"UPDATE {source_table} SET circle_tier = :tier, atom_id = :atom_id "
                f"WHERE id = :source_id"
            ),
            {"tier": tier, "atom_id": atom_id, "source_id": source_id},
        )
        await self.db.commit()
        return atom_id

    async def create_with_source(
        self,
        *,
        atom_type: str,
        owner_user_id: int,
        tier: int,
    ) -> str:
        """Pre-create an atoms row before the source row exists.

        Source-table writers for every type EXCEPT ``kb_document`` hit a
        chicken-and-egg: their ``atom_id`` column is NOT NULL + non-deferrable
        FK to ``atoms.atom_id`` (pc20260420_circles_v1_schema.py), but the
        source row's PK is auto-incremented and only known after flush. This
        method seeds the atoms row with a unique placeholder ``source_id``;
        the caller invokes :meth:`finalize_source_id` after flushing the
        source row to patch the real PK.

        Returns the minted atom_id. Intended call pattern:

            atom_id = await atom_svc.create_with_source(
                atom_type="conversation_memory",
                owner_user_id=42, tier=0,
            )
            memory = ConversationMemory(..., atom_id=atom_id, circle_tier=0)
            self.db.add(memory); await self.db.flush()
            await atom_svc.finalize_source_id(atom_id, memory.id)

        For ``kb_document`` atoms, Documents are registered BEFORE chunks
        and their own FK is nullable (see post-pc20260423 schema), so the
        placeholder dance still applies because Document.atom_id is set
        to the minted atom_id at creation time before the document row is
        flushed. RAGService uses this helper the same way.
        """
        atom_id = str(uuid.uuid4())
        source_table = _table_for_atom_type(atom_type)
        placeholder = f"__pending__{atom_id}"
        now = datetime.now(UTC).replace(tzinfo=None)
        atom_row = AtomModel(
            atom_id=atom_id,
            atom_type=atom_type,
            source_table=source_table,
            source_id=placeholder,
            owner_user_id=int(owner_user_id),
            policy={"tier": int(tier)},
            created_at=now,
            updated_at=now,
        )
        self.db.add(atom_row)
        await self.db.flush()
        return atom_id

    async def finalize_source_id(self, atom_id: str, source_id: int | str) -> None:
        """Replace the placeholder ``source_id`` on an atoms row with the
        real PK now that the source row has been flushed.

        Paired with :meth:`create_with_source`. Callers MUST invoke this
        after the source-row flush; skipping it leaves the atoms row
        pointing at a ``__pending__<uuid>`` placeholder that retrieval
        will never find a match for.
        """
        atom = (await self.db.execute(
            select(AtomModel).where(AtomModel.atom_id == atom_id)
        )).scalar_one()
        atom.source_id = str(source_id)
        await self.db.flush()

    async def update_tier(self, atom_id: str, new_policy: dict[str, Any]) -> None:
        """
        Update atom.policy + cascade to source row's denormalized circle_tier.

        For kg_node atoms, also recomputes circle_tier on every incident
        kg_relation (per CEO Finding E cascade rule).
        """
        atom_orm = (await self.db.execute(
            select(AtomModel).where(AtomModel.atom_id == atom_id)
        )).scalar_one_or_none()
        if atom_orm is None:
            logger.warning(f"AtomService.update_tier: atom_id {atom_id} not found")
            return

        new_tier = int(new_policy.get("tier", 0))
        atom_orm.policy = dict(new_policy)
        atom_orm.updated_at = datetime.now(UTC).replace(tzinfo=None)

        # Cascade to source row. `atom.source_id` is always stored as
        # TEXT (polymorphic across source tables), but most source tables
        # have an INTEGER `id` column — asyncpg refuses to encode a Python
        # str as int4, producing a 500. Compare via `id::text` so this
        # works for any source-table id type (int, bigint, uuid, text).
        await self.db.execute(
            text(
                f"UPDATE {atom_orm.source_table} SET circle_tier = :tier "
                f"WHERE id::text = :source_id"
            ),
            {"tier": new_tier, "source_id": atom_orm.source_id},
        )

        # kb_document cascade: propagate the new tier to every chunk of
        # this document in the SAME transaction — otherwise retrieval would
        # read stale document_chunks.circle_tier and leak chunks at the old
        # tier until someone noticed (Risk A from the design doc review).
        if atom_orm.atom_type == ATOM_TYPE_KB_DOCUMENT:
            await self.db.execute(
                text(
                    "UPDATE document_chunks SET circle_tier = :tier "
                    "WHERE document_id = :doc_id"
                ),
                {"tier": new_tier, "doc_id": int(atom_orm.source_id)},
            )

        # KG node cascade: incident relations recompute MIN(subject, object).
        if atom_orm.atom_type == ATOM_TYPE_KG_NODE:
            entity_id = int(atom_orm.source_id)
            await self.db.execute(
                text(
                    "UPDATE kg_relations r SET circle_tier = "
                    "LEAST(s.circle_tier, o.circle_tier) "
                    "FROM kg_entities s, kg_entities o "
                    "WHERE r.subject_id = s.id AND r.object_id = o.id "
                    "AND (r.subject_id = :entity_id OR r.object_id = :entity_id)"
                ),
                {"entity_id": entity_id},
            )
            # Also update atoms.policy for those relations to stay in sync.
            await self.db.execute(
                text(
                    "UPDATE atoms SET policy = json_build_object('tier', r.circle_tier), "
                    "updated_at = NOW() "
                    "FROM kg_relations r "
                    "WHERE atoms.atom_type = :edge_type "
                    "AND atoms.source_id = r.id::text "
                    "AND (r.subject_id = :entity_id OR r.object_id = :entity_id)"
                ),
                {"edge_type": ATOM_TYPE_KG_EDGE, "entity_id": entity_id},
            )

        await self.db.commit()
        self.resolver.invalidate_for_atom(atom_id)

    # ==========================================================================
    # Read
    # ==========================================================================

    async def get_atom(self, atom_id: str, asker_id: int) -> Atom | None:
        """
        Returns None for both not-found AND not-authorized (uniform 404).
        Callers needing 403 vs 404 distinction MUST call resolver.can_access_atom
        before this.
        """
        atom_orm = (await self.db.execute(
            select(AtomModel).where(AtomModel.atom_id == atom_id)
        )).scalar_one_or_none()
        if atom_orm is None:
            return None

        atom = atom_from_orm(atom_orm)
        if not await self.resolver.can_access_atom(asker_id, atom):
            return None  # uniform 404
        return atom

    # ==========================================================================
    # Delete
    # ==========================================================================

    async def soft_delete(self, atom_id: str) -> None:
        """
        Marks the source row inactive (sets is_active=False).
        The atoms row stays for audit trail; the FK ON DELETE CASCADE means
        hard-deleting the atom would cascade to the source row, which we avoid.
        """
        atom_orm = (await self.db.execute(
            select(AtomModel).where(AtomModel.atom_id == atom_id)
        )).scalar_one_or_none()
        if atom_orm is None:
            return

        # Most source tables have an is_active column; if not, this is a no-op.
        try:
            await self.db.execute(
                text(
                    f"UPDATE {atom_orm.source_table} SET is_active = false "
                    f"WHERE id = :source_id"
                ),
                {"source_id": atom_orm.source_id},
            )
            await self.db.commit()
        except Exception as e:
            logger.warning(
                f"AtomService.soft_delete: source table {atom_orm.source_table} "
                f"may lack is_active column or row missing: {e}"
            )
            await self.db.rollback()

        self.resolver.invalidate_for_atom(atom_id)


# ==========================================================================
# Helpers
# ==========================================================================


def _table_for_atom_type(atom_type: str) -> str:
    """Map atom_type discriminator → source table name.

    Post-atoms-per-document (pc20260423): ``kb_chunk`` is deliberately absent
    — the migration deletes every ``kb_chunk`` atom, ``document_chunks.atom_id``
    no longer exists, and ``upsert_atom``'s generic ``UPDATE … SET atom_id``
    would fail on that column. Any writer that still produces ``kb_chunk``
    atoms will raise ``ValueError`` here loudly rather than silently corrupting
    state downstream.
    """
    table_map = {
        "kb_document": "documents",
        "kg_node": "kg_entities",
        "kg_edge": "kg_relations",
        "conversation_memory": "conversation_memories",
    }
    if atom_type not in table_map:
        raise ValueError(f"Unknown atom_type: {atom_type}")
    return table_map[atom_type]


def _source_id_for(atom: Atom) -> str:
    """Extract the source row's primary key as a string."""
    payload = atom.payload or {}
    # Prefer explicit document_id / chunk_id / entity_id / relation_id /
    # memory_id keys (ordered most-specific first).
    for key in ("document_id", "chunk_id", "entity_id", "relation_id", "memory_id"):
        if key in payload:
            return str(payload[key])
    # Fall back to the atom's stored source_id placeholder if set elsewhere
    raise ValueError(
        f"Cannot determine source_id for atom {atom.atom_type}: "
        f"payload missing document_id/chunk_id/entity_id/relation_id/memory_id"
    )
