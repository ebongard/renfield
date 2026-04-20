"""
AtomStore Protocol — the storage-agnostic abstraction over heterogeneous atoms.

Defined as a Protocol (not an ABC) so concrete implementations can be plain
classes without inheritance ceremony. Two implementations ship in v1:

    PolymorphicAtomStore      v1 default — wraps the Lane-A retrieval modules
                              (RAGRetrieval, KGRetrieval, MemoryRetrieval) +
                              merges results via reciprocal rank fusion.

    KGAtomStore (FUTURE)      v3 — backs every atom with a KG node. Same
                              Protocol; PolymorphicAtomStore is replaced
                              without consumer code changes.

Returns None for both not-found AND not-authorized (uniform 404 to avoid
existence oracle attacks). Callers needing 403 vs 404 distinction must call
CircleResolver.can_access_atom first.

upsert_atom writes the atoms row + the source row in one transaction with
SELECT ... FOR UPDATE on the atoms row. Source-table writers (document
worker, KG extractor, conversation memory extractor) MUST go through
AtomService.upsert_atom — direct INSERTs to source tables are forbidden by
code review and a CI lint rule (lint not yet built; will land in Lane C).
"""
from __future__ import annotations

from typing import Protocol, Sequence

from services.atom_types import Atom, AtomMatch


class AtomStore(Protocol):
    """Storage-agnostic interface over the unified atom abstraction."""

    async def query(
        self,
        query_text: str,
        *,
        asker_id: int,
        max_visible_tier: int,
        hybrid: bool = True,
        top_k: int = 20,
    ) -> Sequence[AtomMatch]:
        """
        Top-K atoms most relevant to query_text, filtered by access policy.

        max_visible_tier comes from CircleResolver.get_max_visible_tier(asker, owner).
        For the v1 home deployment with single 'tier' ladder, this is the depth
        index the asker has been placed at by the atom owner. Atoms with
        circle_tier < max_visible_tier are filtered OUT (they're too private
        for this asker to see).

        hybrid=True enables BM25 + dense vector + RRF (default for kb_chunks).
        Pure-vector or pure-BM25 modes are concrete-implementation specific.
        """
        ...

    async def get_atom(self, atom_id: str, *, asker_id: int) -> Atom | None:
        """
        Returns None for both "not found" AND "not authorized" (uniform 404
        to avoid existence oracle). Audit log records access-denial separately
        (in AtomService).
        """
        ...

    async def upsert_atom(self, atom: Atom) -> str:
        """
        Writes the atoms row AND the source row in one transaction.

        Source-table writers MUST go through this method. Direct INSERTs to
        source tables (document_chunks, kg_entities, etc.) bypass the atom
        registry and break circle access checks downstream.

        Returns the atom_id (UUID4 as 36-char string) of the upserted atom.
        """
        ...

    async def update_tier(self, atom_id: str, new_policy: dict) -> None:
        """
        Updates atoms.policy AND the denormalized circle_tier on the source row
        in one transaction. Invalidates CircleResolver cache for this atom.

        For kg_node tier changes, also cascades to all incident kg_relations
        (each relation's circle_tier becomes MIN(subject.tier, object.tier)
        per the back-fill rule).
        """
        ...

    async def soft_delete(self, atom_id: str) -> None:
        """
        Marks the source row inactive. The atoms row stays for audit trail;
        the FK with ON DELETE CASCADE means hard-deleting the atom would
        cascade to the source row, which we generally avoid (audit-trail
        preservation matters more than table cleanliness here).
        """
        ...
