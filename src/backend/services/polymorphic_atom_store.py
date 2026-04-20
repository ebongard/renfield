"""
PolymorphicAtomStore — v1 implementation of the AtomStore Protocol.

Wraps the three Lane-A retrieval modules (RAGRetrieval, KGRetrieval,
MemoryRetrieval) and merges their results via reciprocal rank fusion (RRF).
Each retrieval module is responsible for its own circle-tier filter pushdown
to SQL (added in Lane C alongside the legacy-consumer rewrite).

ASCII query flow:

    PolymorphicAtomStore.query(text, asker_id, max_visible_tier, top_k=20)
        |
        +-- Build per-asker AccessContext (dimensions + memberships) once.
        |
        +-- Parallel fan-out (asyncio.gather):
        |     +-- RAGRetrieval(db).search(text, top_k=top_k*3) -> kb_chunks
        |     +-- KGRetrieval(db).get_relevant_context(text, asker)
        |     +-- MemoryRetrieval(db).retrieve(text, asker_id)
        |
        +-- Wrap each source result list as AtomMatch[] with rank assigned.
        |
        +-- RRF merge across the four sources:
        |     score = sum(weight / (k + rank + 1))
        |     where k = settings.rag_hybrid_rrf_k (default 60)
        |
        +-- Truncate to top_k, return AtomMatch[].

Per CEO Tension A acceptance, this is the v1 default; v3 KG-as-brain swaps
in KGAtomStore against the same Protocol without touching this module's
consumers.

NOT IN SCOPE for v1 PolymorphicAtomStore:
- Cross-source ranking is rank-only RRF (per Open Q 9 — eng-review accepted
  this trade-off rather than normalizing heterogeneous score scales)
- get_atom / upsert_atom / update_tier / soft_delete delegate to AtomService
  (PolymorphicAtomStore is primarily a query-side router)
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Sequence

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from services.atom_service import AtomService
from services.atom_types import Atom, AtomMatch
from services.circle_resolver import CircleResolver
from utils.config import settings


class PolymorphicAtomStore:
    """v1 AtomStore implementation. Fans out to the Lane-A retrieval modules."""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.resolver = CircleResolver(db)
        self.atom_service = AtomService(db, self.resolver)

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
        Query each source in parallel; merge with RRF; return top_k.

        max_visible_tier is the integer tier index the asker can reach in
        the relevant atom owner's circles. For multi-owner queries (the
        common case), this is computed per-source via CircleResolver inside
        each retrieval module's filter clause.

        v1 PolymorphicAtomStore passes max_visible_tier through but the
        underlying retrieval modules don't yet apply circle filters — that's
        Lane C work (rewriting the legacy scope/permission filters into
        circle_tier filters in rag_retrieval / kg_retrieval / memory_retrieval).
        Until Lane C lands, query() returns un-filtered results from each
        source (legacy behavior preserved).
        """
        from services.kg_retrieval import KGRetrieval
        from services.memory_retrieval import MemoryRetrieval
        from services.rag_retrieval import RAGRetrieval

        candidate_k = top_k * 3  # over-fetch for RRF fusion across sources

        rag_task = RAGRetrieval(self.db).search(query_text, top_k=candidate_k)
        kg_task = KGRetrieval(self.db).get_relevant_context(query_text, user_id=asker_id)
        memory_task = MemoryRetrieval(self.db).retrieve(query_text, user_id=asker_id, limit=candidate_k)

        # Per PR #402 review SHOULD-FIX #9: gather(return_exceptions=True) does NOT raise,
        # so the previous try/except wrapper around it was dead code that swallowed
        # programmer errors (e.g., import failure on the lazy-imported retrieval modules).
        # Removing the wrapper — exceptions in retrieval modules are converted to []
        # by the _wrap_* helpers, and any actual programmer error now bubbles to FastAPI.
        rag_results, kg_context, memory_results = await asyncio.gather(
            rag_task, kg_task, memory_task,
            return_exceptions=True,
        )

        rag_matches = _wrap_rag_results(rag_results)
        kg_matches = _wrap_kg_context(kg_context)
        memory_matches = _wrap_memory_results(memory_results)

        merged = _rrf_merge(
            [rag_matches, kg_matches, memory_matches],
            top_k=top_k,
            k=settings.rag_hybrid_rrf_k,
        )
        return merged

    async def get_atom(self, atom_id: str, *, asker_id: int) -> Atom | None:
        """Delegates to AtomService — uniform None on not-found AND not-authorized."""
        return await self.atom_service.get_atom(atom_id, asker_id)

    async def upsert_atom(self, atom: Atom) -> str:
        return await self.atom_service.upsert_atom(atom)

    async def update_tier(self, atom_id: str, new_policy: dict[str, Any]) -> None:
        await self.atom_service.update_tier(atom_id, new_policy)

    async def soft_delete(self, atom_id: str) -> None:
        await self.atom_service.soft_delete(atom_id)


def _now() -> datetime:
    return datetime.now()


def _wrap_rag_results(rag_results: Any) -> list[AtomMatch]:
    """Convert RAGRetrieval.search output -> list[AtomMatch]."""
    if isinstance(rag_results, Exception) or not rag_results:
        return []
    matches = []
    now = _now()
    for rank, result in enumerate(rag_results, start=1):
        chunk = result.get("chunk", {})
        doc = result.get("document", {})
        # Per PR #402 review SHOULD-FIX #11: warn-log when atom_id is missing
        # (should never happen post-migration; if it does, the back-fill skipped
        # this row or a writer bypassed AtomService).
        if chunk.get("atom_id") is None:
            logger.warning(
                f"PolymorphicAtomStore: chunk id={chunk.get('id')} has no atom_id "
                f"(post-migration this should not happen — back-fill missed this row "
                f"or writer bypassed AtomService.upsert_atom)"
            )
        atom_id = chunk.get("atom_id") or f"kb_chunk:{chunk.get('id', 0)}"
        matches.append(
            AtomMatch(
                atom=Atom(
                    atom_id=str(atom_id),
                    atom_type="kb_chunk",
                    owner_user_id=0,
                    policy={"tier": chunk.get("circle_tier", 0)},
                    created_at=now,
                    updated_at=now,
                    payload={
                        "chunk_id": chunk.get("id"),
                        "document_id": doc.get("id"),
                        "content": chunk.get("content", ""),
                        "page_number": chunk.get("page_number"),
                        "section_title": chunk.get("section_title"),
                        "document_filename": doc.get("filename", ""),
                        "document_title": doc.get("title"),
                    },
                ),
                score=float(result.get("similarity", 0.0)),
                snippet=chunk.get("content", "")[:200],
                rank=rank,
            )
        )
    return matches


def _wrap_kg_context(kg_context: Any) -> list[AtomMatch]:
    """
    Convert KGRetrieval.get_relevant_context output (str or None) -> list[AtomMatch].

    KGRetrieval returns a formatted string today (per Lane A1). For PolymorphicAtomStore
    we represent it as a single AtomMatch wrapping the formatted text. v2.5 KG retrieval
    upgrade will return per-triple AtomMatch[] for proper RRF participation.
    """
    if isinstance(kg_context, Exception) or not kg_context:
        return []
    now = _now()
    return [
        AtomMatch(
            atom=Atom(
                atom_id="kg_aggregated",  # placeholder; v2.5 returns per-triple atoms
                atom_type="kg_node",
                owner_user_id=0,
                policy={"tier": 0},
                created_at=now,
                updated_at=now,
                payload={"content": str(kg_context)},
            ),
            score=0.7,  # placeholder; v2.5 returns proper per-triple scores
            snippet=str(kg_context)[:200],
            rank=1,
        )
    ]


def _wrap_memory_results(memory_results: Any) -> list[AtomMatch]:
    """Convert MemoryRetrieval.retrieve output -> list[AtomMatch]."""
    if isinstance(memory_results, Exception) or not memory_results:
        return []
    matches = []
    now = _now()
    for rank, m in enumerate(memory_results, start=1):
        atom_id = m.get("atom_id") or f"memory:{m.get('id', 0)}"
        matches.append(
            AtomMatch(
                atom=Atom(
                    atom_id=str(atom_id),
                    atom_type="conversation_memory",
                    owner_user_id=0,
                    policy={"tier": m.get("circle_tier", 0)},
                    created_at=now,
                    updated_at=now,
                    payload={
                        "memory_id": m.get("id"),
                        "content": m.get("content", ""),
                        "category": m.get("category"),
                        "importance": m.get("importance", 0.5),
                    },
                ),
                score=float(m.get("similarity", 0.0)),
                snippet=m.get("content", "")[:200],
                rank=rank,
            )
        )
    return matches


def _rrf_merge(
    source_lists: list[list[AtomMatch]],
    top_k: int,
    k: int = 60,
) -> list[AtomMatch]:
    """
    Reciprocal rank fusion across N source lists.

    score = sum(1 / (k + rank)) for each appearance.
    Equal source weighting (could be made configurable; not in v1 scope).
    """
    scores: dict[str, float] = {}
    matches_by_id: dict[str, AtomMatch] = {}

    for source_list in source_lists:
        for match in source_list:
            atom_id = match.atom.atom_id
            scores[atom_id] = scores.get(atom_id, 0.0) + 1.0 / (k + match.rank)
            # Per PR #402 review SHOULD-FIX #10: keep the highest-ranked source
            # (lowest rank value) for the snippet/score, not first-seen.
            if atom_id not in matches_by_id or match.rank < matches_by_id[atom_id].rank:
                matches_by_id[atom_id] = match

    sorted_ids = sorted(scores.keys(), key=lambda aid: scores[aid], reverse=True)[:top_k]

    result = []
    for new_rank, atom_id in enumerate(sorted_ids, start=1):
        original = matches_by_id[atom_id]
        result.append(
            AtomMatch(
                atom=original.atom,
                score=round(scores[atom_id], 6),
                snippet=original.snippet,
                rank=new_rank,
            )
        )
    return result
