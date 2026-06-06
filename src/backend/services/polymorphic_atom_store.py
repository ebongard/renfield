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
        from services.document_fact_retrieval import DocumentFactRetrieval
        from services.kg_retrieval import KGRetrieval
        from services.lexical_retrieval import LexicalRetrieval
        from services.memory_retrieval import MemoryRetrieval
        from services.rag_retrieval import RAGRetrieval
        from services.skill_service import SkillService

        candidate_k = top_k * 3  # over-fetch for RRF fusion across sources

        rag_task = RAGRetrieval(self.db).search(query_text, top_k=candidate_k)
        # Structured per-entity/-relation KG atoms (not the aggregated string):
        # each kg_node/kg_edge carries its source id so the detail drawer can
        # render the entity + edit its tier. The agent path still uses the
        # string form (get_relevant_context) elsewhere.
        kg_task = KGRetrieval(self.db).get_relevant_atoms(query_text, user_id=asker_id)
        memory_task = MemoryRetrieval(self.db).retrieve(query_text, user_id=asker_id, limit=candidate_k)
        # Lexical retrievers — keyword / name fallback for queries the
        # vector path mis-ranks (single tokens like "Jutta", short
        # German questions whose embedding sits below
        # memory_retrieval_threshold). Returns RAG-shape and memory-shape
        # dicts so the existing _wrap_rag_results / _wrap_memory_results
        # helpers consume them unchanged. Duplicate atom_ids across
        # vector + lexical lists get an RRF boost from both contributions
        # — exactly the "this is both semantic AND keyword match"
        # behaviour we want.
        lex = LexicalRetrieval(self.db)
        lexical_chunks_task = lex.search_chunks_lexical(
            query_text, asker_id=asker_id, top_k=candidate_k,
        )
        lexical_memories_task = lex.search_memories_lexical(
            query_text, asker_id=asker_id, top_k=candidate_k,
        )
        # Schicht A document facts — a sixth source so structured facts
        # (Steuernummer, issuer, obligation) surface in /brain search via the
        # same RRF fusion. Keyword/identifier retrieval, not vector (facts are
        # short structured strings; the parent chunk is already embedded).
        document_fact_task = DocumentFactRetrieval(self.db).search(
            query_text, asker_id=asker_id, top_k=candidate_k,
        )
        # Self-learning Phase 1: procedural skills are atoms too — give
        # them a fourth source for the unified /brain search. Gated on
        # skills_enabled so the existing three-source RRF behavior is
        # unchanged for deployments that haven't opted in.
        if settings.skills_enabled:
            skill_task = SkillService(self.db).find_similar(
                query_text, asker_id=asker_id, top_k=candidate_k,
            )
        else:
            async def _empty():
                return []
            skill_task = _empty()

        # Per PR #402 review SHOULD-FIX #9: gather(return_exceptions=True) does NOT raise,
        # so the previous try/except wrapper around it was dead code that swallowed
        # programmer errors (e.g., import failure on the lazy-imported retrieval modules).
        # Removing the wrapper — exceptions in retrieval modules are converted to []
        # by the _wrap_* helpers, and any actual programmer error now bubbles to FastAPI.
        (
            rag_results, kg_atoms, memory_results,
            lexical_chunks, lexical_memories, skill_results, document_fact_results,
        ) = await asyncio.gather(
            rag_task, kg_task, memory_task,
            lexical_chunks_task, lexical_memories_task, skill_task, document_fact_task,
            return_exceptions=True,
        )

        rag_matches = _wrap_rag_results(rag_results)
        kg_matches = _wrap_kg_atoms(kg_atoms)
        memory_matches = _wrap_memory_results(memory_results)
        lexical_chunk_matches = _wrap_rag_results(lexical_chunks)
        lexical_memory_matches = _wrap_memory_results(lexical_memories)
        skill_matches = _wrap_skill_results(skill_results)
        document_fact_matches = _wrap_document_fact_results(document_fact_results)

        merged = _rrf_merge(
            [
                rag_matches, kg_matches, memory_matches,
                lexical_chunk_matches, lexical_memory_matches,
                skill_matches, document_fact_matches,
            ],
            top_k=top_k,
            k=settings.rag_hybrid_rrf_k,
        )

        # Phase 4: graph expansion (post-RRF). Walk 1-2 hops out from the fused
        # kg_node pivots so the agent/UI sees the neighbourhood, not just the
        # matched nodes. Circle-filtered per hop, leak-safe edges, decay-scored,
        # provenance-marked. Flag-gated (off => merged unchanged). Single seam so
        # the decay survives and it runs once.
        if settings.graph_expansion_enabled:
            from services.graph_expansion import expand_fused
            extra = await expand_fused(
                list(merged), asker_id, self.db,
                max_pivots=settings.graph_expansion_max_pivots,
                max_hops=settings.graph_expansion_max_hops,
                max_expanded=settings.graph_expansion_max_expanded,
            )
            if extra:
                have = {m.atom.atom_id for m in merged}
                merged = list(merged) + [m for m in extra if m.atom.atom_id not in have]
                merged.sort(key=lambda m: m.score, reverse=True)
                merged = merged[: top_k + settings.graph_expansion_max_expanded]
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
    """Convert RAGRetrieval.search output -> list[AtomMatch].

    Post-atoms-per-document (pc20260423): emits one AtomMatch per unique
    DOCUMENT, not per chunk. Multiple chunks of the same document that
    matched the query collapse to a single match carrying the best-scoring
    chunk's snippet. This keeps the polymorphic Cross-Source RRF fusion
    fair — documents compete on equal footing with KG entities and
    memories, rather than a long document flooding the top-K with its own
    chunks.
    """
    if isinstance(rag_results, Exception) or not rag_results:
        return []

    # Group chunk results by document atom_id, keeping the highest similarity
    # per document. RAGRetrieval.search returns chunks sorted by similarity
    # desc; we preserve that order when collapsing.
    now = _now()
    seen_atoms: dict[str, AtomMatch] = {}
    next_rank = 1
    for result in rag_results:
        chunk = result.get("chunk", {})
        doc = result.get("document", {})

        doc_atom_id = doc.get("atom_id")
        if doc_atom_id is None:
            logger.warning(
                f"PolymorphicAtomStore: document id={doc.get('id')} has no atom_id "
                f"(post-migration this should not happen — back-fill missed this "
                f"row or writer bypassed AtomService.create_with_source)"
            )
            doc_atom_id = f"kb_document:{doc.get('id', 0)}"
        atom_id = str(doc_atom_id)

        if atom_id in seen_atoms:
            continue  # already have a higher-scoring chunk for this document

        similarity = float(result.get("similarity", 0.0))
        seen_atoms[atom_id] = AtomMatch(
            atom=Atom(
                atom_id=atom_id,
                atom_type="kb_document",
                owner_user_id=0,
                # Chunks carry the denormalized tier from their document;
                # either side gives the same value, chunk-side is cheap.
                policy={"tier": chunk.get("circle_tier", doc.get("circle_tier", 0))},
                created_at=now,
                updated_at=now,
                payload={
                    "document_id": doc.get("id"),
                    "best_chunk_id": chunk.get("id"),
                    "content": chunk.get("content", ""),
                    "page_number": chunk.get("page_number"),
                    "section_title": chunk.get("section_title"),
                    "document_filename": doc.get("filename", ""),
                    "document_title": doc.get("title"),
                },
            ),
            score=similarity,
            snippet=chunk.get("content", "")[:200],
            rank=next_rank,
        )
        next_rank += 1
    return list(seen_atoms.values())


def _wrap_kg_atoms(kg_atoms: Any) -> list[AtomMatch]:
    """
    Convert KGRetrieval.get_relevant_atoms output -> list[AtomMatch].

    Emits ONE ``kg_node`` atom per matched entity and ONE ``kg_edge`` atom per
    relation, each carrying its source id in the payload (``entity_id`` /
    ``relation_id``) so the unified-search detail drawer can render the entity
    and edit its circle tier via the KG int-id endpoint. Replaces the old single
    ``kg_aggregated`` string blob (which had no id and couldn't be opened).
    """
    if isinstance(kg_atoms, Exception) or not kg_atoms:
        return []
    entities = kg_atoms.get("entities") or []
    relations = kg_atoms.get("relations") or []
    if not entities and not relations:
        return []

    now = _now()
    matches: list[AtomMatch] = []
    rank = 1
    # Entities first (more relevant for a "tell me about X" search), then edges.
    for e in entities:
        eid = e.get("id")
        matches.append(
            AtomMatch(
                atom=Atom(
                    atom_id=f"kg_node:{eid}",
                    atom_type="kg_node",
                    owner_user_id=0,
                    policy={"tier": int(e.get("circle_tier", 0) or 0)},
                    created_at=now,
                    updated_at=now,
                    payload={
                        "entity_id": eid,
                        "name": e.get("name", ""),
                        "entity_type": e.get("entity_type"),
                    },
                ),
                score=float(e.get("similarity", 0.0) or 0.0),
                snippet=str(e.get("name", ""))[:200],
                rank=rank,
            )
        )
        rank += 1
    for r in relations:
        rid = r.get("id")
        subj = r.get("subject_name", "?")
        pred = r.get("predicate", "")
        obj = r.get("object_name", "?")
        matches.append(
            AtomMatch(
                atom=Atom(
                    atom_id=f"kg_edge:{rid}",
                    atom_type="kg_edge",
                    owner_user_id=0,
                    policy={"tier": int(r.get("circle_tier", 0) or 0)},
                    created_at=now,
                    updated_at=now,
                    payload={
                        "relation_id": rid,
                        "subject_id": r.get("subject_id"),
                        "subject_name": subj,
                        "predicate": pred,
                        "object_id": r.get("object_id"),
                        "object_name": obj,
                    },
                ),
                score=0.5,  # relations rank below entities; RRF blends across sources
                snippet=f"{subj} {pred} {obj}"[:200],
                rank=rank,
            )
        )
        rank += 1
    return matches


def _wrap_skill_results(skill_results: Any) -> list[AtomMatch]:
    """Convert SkillService.find_similar output -> list[AtomMatch]."""
    if isinstance(skill_results, Exception) or not skill_results:
        return []
    matches: list[AtomMatch] = []
    now = _now()
    for rank, s in enumerate(skill_results, start=1):
        atom_id = f"procedural_skill:{s.get('id', 0)}"
        body = s.get("body_md") or ""
        matches.append(
            AtomMatch(
                atom=Atom(
                    atom_id=str(atom_id),
                    atom_type="procedural_skill",
                    owner_user_id=0,  # not exposed by find_similar; not needed downstream
                    policy={"tier": 0},  # tier already enforced by find_similar's WHERE
                    created_at=now,
                    updated_at=now,
                    payload={
                        "skill_id": s.get("id"),
                        "title": s.get("title"),
                        "content": body,
                        "trigger_examples": s.get("trigger_examples") or [],
                        "tool_sequence": s.get("tool_sequence") or [],
                        "source": s.get("source"),
                    },
                ),
                score=float(s.get("similarity", 0.0)),
                snippet=body[:200],
                rank=rank,
            )
        )
    return matches


def _wrap_document_fact_results(fact_results: Any) -> list[AtomMatch]:
    """Convert DocumentFactRetrieval.search output -> list[AtomMatch].

    Each fact already carries its real ``atom_id`` (the document_fact atom),
    so duplicates across sources fuse correctly in RRF. The snippet prefers the
    fact ``value`` (the headline — a Steuernummer, issuer, summary) and falls
    back to the excerpt. The payload carries the structured fields the /brain
    UI and any obligation surface need (obligation_date ISO, amount, legal_gate,
    source). Exceptions / empty -> [] (gather resilience).
    """
    if isinstance(fact_results, Exception) or not fact_results:
        return []
    matches: list[AtomMatch] = []
    now = _now()
    for rank, f in enumerate(fact_results, start=1):
        atom_id = f.get("atom_id") or f"document_fact:{f.get('id', 0)}"
        value = f.get("value") or ""
        snippet = value or (f.get("excerpt") or "")
        matches.append(
            AtomMatch(
                atom=Atom(
                    atom_id=str(atom_id),
                    atom_type="document_fact",
                    owner_user_id=0,  # not exposed by search; not needed downstream
                    policy={"tier": f.get("circle_tier", 0)},
                    created_at=now,
                    updated_at=now,
                    payload={
                        "fact_id": f.get("id"),
                        "document_id": f.get("document_id"),
                        "category": f.get("category"),
                        "kind": f.get("kind"),
                        "value": value,
                        "normalized_value": f.get("normalized_value"),
                        "excerpt": f.get("excerpt"),
                        "obligation_date": f.get("obligation_date"),
                        "amount_value": f.get("amount_value"),
                        "amount_currency": f.get("amount_currency"),
                        "legal_gate": f.get("legal_gate", False),
                        "payment_method": f.get("payment_method"),
                        "source": f.get("source"),
                        "tier_overridden": f.get("tier_overridden", False),
                    },
                ),
                score=float(f.get("similarity", 0.0)),
                snippet=snippet[:200],
                rank=rank,
            )
        )
    return matches


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
