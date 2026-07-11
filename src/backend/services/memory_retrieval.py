"""
Memory Retrieval — extracted from conversation_memory_service.py for circles v1.

Holds the read-side methods for Conversation Memory: semantic retrieval,
essential-memory pull, recency scoring, and budget-aware prompt-section
assembly. Write-side concerns (save, extract_and_save, dedup, contradiction
resolution, cleanup, CRUD) remain in
conversation_memory_service.ConversationMemoryService.

ASCII data flow (retrieve_for_prompt — main agent-facing entry point):

    query (str) + user_id + team_ids + budget_chars
       │
       ▼
    Section 1: ESSENTIAL (high-importance facts/preferences, always injected)
       │  retrieve_essential() ── importance >= memory_essential_threshold
       │                          AND category != 'context'
       ▼
    Section 2: PROCEDURAL (behavioral rules, scope-filtered)
       │  scope = global OR (user_id match) OR (team_id match)
       │  trigger_pattern matched against query (regex)
       ▼
    Section 3: SEMANTIC (query-relevant via embedding similarity)
       │  retrieve(query) ── pgvector cosine, threshold + importance + confidence ranking
       ▼
    Section 4: EPISODIC (recent interaction summaries; if memory_episodic_enabled)
       │  EpisodicMemoryService.retrieve(query)
       ▼
    Cap total at budget_chars; return dict[section_name -> list[memory]]

Both retrieve() and retrieve_essential() are READ-WRITE: they update
access_count + last_accessed_at on returned memories (used by the decay
cleanup logic in ConversationMemoryService.cleanup). This is intentional —
read frequency feeds memory importance.

Lane A3 of the second-brain-circles eng-review plan. Same pattern as
Lane A1 (rag_retrieval) and Lane A2 (kg_retrieval).

Note on circles v1 schema: ConversationMemory.scope (user/team/global) +
team_id is the existing access model. Per Finding 1.2C in the eng-review,
team_id is parked for v2 named-circles migration; v1 circles work adds
circle_tier alongside scope without disturbing this retrieval module.
This module's queries will gain a circle-tier filter when LANE B (schema
work) lands; the access model stays scope+team for now.
"""
from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from typing import Any

from loguru import logger
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import TIER_PUBLIC, ConversationMemory
from services.circle_sql import conversation_memories_circles_filter
from utils.config import settings
from utils.llm_client import get_embed_client

# Similarity score assigned to deterministic subject-linked memories (Phase 3c).
# High floor so a fact about a query-named subject is never ranked below a fuzzy
# embedding hit — but < 1.0 so retrieve_essential's always-injected rows still lead.
_SUBJECT_MATCH_SIMILARITY = 0.99


class MemoryRetrieval:
    """
    Stateless-ish read-side service for Conversation Memory.

    Same dependency shape as ConversationMemoryService but scoped to
    retrieval + access tracking only:
      - db: AsyncSession (queries; commits access_count updates)
      - lazy ollama client (for query embeddings)

    Public surface (mirrors ConversationMemoryService retrieval methods):
      - retrieve(message, user_id?, limit?, threshold?) -> list[dict]
      - retrieve_essential(user_id?, limit?) -> list[dict]
      - retrieve_for_prompt(query, user_id?, team_ids?, budget_chars?)
            -> dict[section -> list[memory]]
    """

    def __init__(self, db: AsyncSession):
        self.db = db
        self._ollama_client = None

    async def _get_ollama_client(self):
        """Lazy init of the embedding Ollama client."""
        if self._ollama_client is None:
            self._ollama_client = get_embed_client()
        return self._ollama_client

    async def _get_embedding(self, text_input: str) -> list[float]:
        """Generate query embedding via Ollama."""
        client = await self._get_ollama_client()
        response = await client.embeddings(
            model=settings.ollama_embed_model,
            prompt=text_input,
        )
        return response.embedding

    # ==========================================================================
    # Semantic retrieval (cosine similarity + importance/confidence ranking)
    # ==========================================================================

    async def retrieve(
        self,
        message: str,
        user_id: int | None = None,
        limit: int | None = None,
        threshold: float | None = None,
        ranker: str = "default",
        enforce_circles: bool = False,
    ) -> list[dict]:
        """
        Retrieve relevant memories using cosine similarity search.

        ``enforce_circles`` (federation): force peer-scoped circle filtering even
        when ``AUTH_ENABLED=false``. Default False = today's behavior.

        Lane C: `user_id` is the asker. Results include the asker's own
        memories + public-tier + explicit-grant + tier-membership reachable
        memories from circle peers (per `circle_sql.conversation_memories_circles_filter`).
        For anonymous callers (`user_id is None`) only public-tier memories
        are returned.

        `ranker` controls the SQL ordering:
          - "default": single-stage ORDER BY similarity * importance * confidence
            (legacy behavior, used by web chat + retrieve_for_prompt).
          - "recency_aware": two-stage CTE — Stage-1 HNSW recall over
            `memory_retrieval_recall_k` candidates, Stage-2 rerank with
            recency factor on `last_accessed_at`. Used by Lane B/2 v2
            extractor (improves cross_session_stale detection by widening
            the candidate window and surfacing recently-touched rows that
            the user is contradicting).

        Returns list of dicts with id, content, category, importance, similarity.
        Side effect: updates access_count + last_accessed_at on returned rows.
        """
        limit = limit or settings.memory_retrieval_limit
        threshold = threshold if threshold is not None else settings.memory_retrieval_threshold

        try:
            query_embedding = await self._get_embedding(message)
        except Exception as e:
            logger.warning(f"Could not generate query embedding for memory retrieval: {e}")
            return []

        embedding_str = f"[{','.join(map(str, query_embedding))}]"
        circles_clause, circles_params = self._memory_circles_filter(user_id, enforce_circles)

        if ranker == "recency_aware":
            recall_k = max(int(settings.memory_retrieval_recall_k), int(limit))
            sql = text(f"""
                WITH candidates AS (
                    SELECT
                        id, content, category, importance, confidence, access_count,
                        created_at, last_accessed_at, subject_name,
                        (1 - (embedding <=> CAST(:embedding AS vector))) AS similarity
                    FROM conversation_memories m
                    WHERE is_active = true
                      AND embedding IS NOT NULL
                      AND {circles_clause}
                    ORDER BY (embedding <=> CAST(:embedding AS vector)) ASC
                    LIMIT :recall_k
                )
                SELECT
                    id, content, category, importance, confidence,
                    access_count, created_at, subject_name, similarity
                FROM candidates
                ORDER BY (
                    similarity * importance * confidence
                    * (1.0 + :recency_weight * EXP(
                        - GREATEST(
                            0,
                            EXTRACT(EPOCH FROM (NOW() - COALESCE(last_accessed_at, created_at)))
                          ) / 86400.0 / :half_life_days
                    ))
                ) DESC
                LIMIT :limit
            """)
            params: dict[str, Any] = {
                "embedding": embedding_str,
                "limit": limit,
                "recall_k": recall_k,
                "recency_weight": float(settings.memory_retrieval_recency_weight),
                "half_life_days": float(settings.memory_retrieval_recency_half_life_days),
                **circles_params,
            }
        else:
            sql = text(f"""
                SELECT
                    id,
                    content,
                    category,
                    importance,
                    confidence,
                    access_count,
                    created_at,
                    subject_name,
                    1 - (embedding <=> CAST(:embedding AS vector)) as similarity
                FROM conversation_memories m
                WHERE is_active = true
                  AND embedding IS NOT NULL
                  AND {circles_clause}
                ORDER BY (1 - (embedding <=> CAST(:embedding AS vector))) * importance * confidence DESC
                LIMIT :limit
            """)
            params = {
                "embedding": embedding_str, "limit": limit, **circles_params,
            }

        result = await self.db.execute(sql, params)
        rows = result.fetchall()

        memories = []
        memory_ids = []
        for row in rows:
            sim = float(row.similarity) if row.similarity else 0
            if sim >= threshold:
                memories.append({
                    "id": row.id,
                    "content": row.content,
                    "category": row.category,
                    "importance": row.importance,
                    "access_count": row.access_count,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "subject_name": row.subject_name,
                    "similarity": round(sim, 3),
                })
                memory_ids.append(row.id)

        # Phase 3c: entity-augmented union. Resolve subjects named in the query
        # and prepend their deterministic memories (circle-filtered) so a fact
        # about a named person isn't dropped just because its wording is far
        # from the query embedding. Subject hits are prioritized but capped
        # within `limit` so they can't flood a generic query. Opt-in flag.
        if settings.memory_kg_bridge_enabled:
            union_limit = min(max(int(settings.memory_retrieval_subject_union_limit), 1), limit)
            existing_ids = {m["id"] for m in memories}
            subject_hits = [
                h for h in await self._subject_linked_memories(
                    message, user_id, union_limit, enforce_circles=enforce_circles
                )
                if h["id"] not in existing_ids
            ]
            if subject_hits:
                memories = (subject_hits + memories)[:limit]
                memory_ids = [m["id"] for m in memories]

        # Update access tracking. flush() not commit() — the caller's
        # outer transaction decides when to persist. Previously this
        # committed unconditionally, which broke the v2-shadow savepoint
        # (the commit closed the savepoint's enclosing transaction,
        # causing the rollback at the end of `_extract_v2_shadow_only`
        # to fail with `ResourceClosedError: This transaction is
        # closed`). Real callers (chat_handler, agent loop) commit at
        # the end of their own scope anyway, so this preserves access
        # tracking durability while letting shadow mode properly roll
        # back.
        if memory_ids:
            await self.db.execute(
                update(ConversationMemory)
                .where(ConversationMemory.id.in_(memory_ids))
                .values(
                    access_count=ConversationMemory.access_count + 1,
                    last_accessed_at=datetime.now(UTC).replace(tzinfo=None),
                )
            )
            await self.db.flush()

        return memories

    # ==========================================================================
    # Essential memory pull (always injected — name, location, preferences)
    # ==========================================================================

    async def retrieve_essential(
        self,
        user_id: int | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """
        Retrieve high-importance memories regardless of query similarity.

        Essential memories (importance >= threshold, category != 'context')
        are always injected into the LLM context so the assistant knows
        the user's name, location, preferences, etc.

        Lane C: `user_id` is the asker — circle filter applies (own +
        public + explicit-grant + tier-membership).
        """
        threshold = settings.memory_essential_threshold
        limit = limit or settings.memory_retrieval_limit
        circles_clause, circles_params = self._memory_circles_filter(user_id)

        sql = text(f"""
            SELECT id, content, category, importance, access_count, created_at, subject_name
            FROM conversation_memories m
            WHERE is_active = true
              AND importance >= :threshold
              AND category != 'context'
              AND {circles_clause}
            ORDER BY importance DESC
            LIMIT :limit
        """)

        params: dict[str, Any] = {
            "threshold": threshold, "limit": limit, **circles_params,
        }

        result = await self.db.execute(sql, params)
        rows = result.fetchall()

        memories = []
        memory_ids = []
        for row in rows:
            memories.append({
                "id": row.id,
                "content": row.content,
                "category": row.category,
                "importance": row.importance,
                "access_count": row.access_count,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "subject_name": row.subject_name,
                "similarity": 1.0,
            })
            memory_ids.append(row.id)

        if memory_ids:
            await self.db.execute(
                update(ConversationMemory)
                .where(ConversationMemory.id.in_(memory_ids))
                .values(
                    access_count=ConversationMemory.access_count + 1,
                    last_accessed_at=datetime.now(UTC).replace(tzinfo=None),
                )
            )
            await self.db.commit()

        return memories

    # ==========================================================================
    # Budget-aware retrieval for prompt injection
    # ==========================================================================

    @staticmethod
    def _recency_score(
        created_at: datetime | None,
        half_life_days: float = 14.0,
    ) -> float:
        """Exponential decay score based on age. Returns 0.0-1.0."""
        if not created_at:
            return 0.5
        now = datetime.now(UTC).replace(tzinfo=None)
        age_days = max((now - created_at).total_seconds() / 86400, 0)
        return math.exp(-0.693 * age_days / half_life_days)

    async def _find_query_entities(self, message: str, user_id: int | None) -> list[int]:
        """Resolve LIVE canonical entity ids named in the query (Phase 3c).

        Exact word-token match on name/surface-form (+ multi-word phrase
        substring) — cheap SQL, NO LLM. Word-token match avoids the
        substring false positive ("Anna" inside "Annahme"). Scoped to the
        asker's own + unowned entities so we never resolve to another user's
        private entity. Returns canonical (canonical_id IS NULL) ids only;
        the caller expands to tombstones via COALESCE(canonical_id, id).
        """
        tokens = sorted({t.lower() for t in re.findall(r"\w{3,}", message or "")})
        if not tokens:
            return []
        params: dict[str, Any] = {
            "tokens": tokens,
            "msg": (message or "").lower(),
            "cap": max(int(settings.memory_retrieval_subject_union_limit), 1) * 4,
        }
        user_filter = ""
        if user_id is not None and settings.auth_enabled:
            user_filter = "AND (user_id = :uid OR user_id IS NULL)"
            params["uid"] = user_id
        sql = text(f"""
            SELECT id FROM kg_entities
            WHERE is_active = true AND canonical_id IS NULL
              {user_filter}
              AND (
                lower(name) = ANY(:tokens)
                OR (position(' ' in name) > 0 AND :msg LIKE '%' || lower(name) || '%')
                OR EXISTS (
                    SELECT 1 FROM jsonb_array_elements_text(surface_forms) sf
                    WHERE lower(sf) = ANY(:tokens)
                )
              )
            LIMIT :cap
        """)
        rows = (await self.db.execute(sql, params)).fetchall()
        return [int(r.id) for r in rows]

    async def _subject_linked_memories(
        self, message: str, user_id: int | None, limit: int,
        enforce_circles: bool = False,
    ) -> list[dict]:
        """Deterministic memories whose subject entity is named in the query (Phase 3c).

        Closes the embedding-recall tail: a fact about a named subject is
        returned even when its wording is far from the query. Tombstone-safe
        (COALESCE(canonical_id, id) chase) and circle-filtered via the SAME
        clause as the embedding path — the union must never bypass circle access
        (incl. the federation ``enforce_circles`` peer scope).
        """
        eids = await self._find_query_entities(message, user_id)
        if not eids:
            return []
        clause, cparams = self._memory_circles_filter(user_id, enforce_circles)
        sql = text(f"""
            SELECT id, content, category, importance, access_count, created_at, subject_name
            FROM conversation_memories m
            WHERE is_active = true
              AND subject_entity_id IS NOT NULL
              AND subject_entity_id IN (
                  SELECT e.id FROM kg_entities e
                  WHERE COALESCE(e.canonical_id, e.id) = ANY(:eids)
              )
              AND {clause}
            ORDER BY importance * confidence DESC
            LIMIT :union_limit
        """)
        rows = (await self.db.execute(
            sql, {"eids": eids, "union_limit": limit, **cparams}
        )).fetchall()
        return [{
            "id": row.id,
            "content": row.content,
            "category": row.category,
            "importance": row.importance,
            "access_count": row.access_count,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "subject_name": row.subject_name,
            "similarity": _SUBJECT_MATCH_SIMILARITY,
        } for row in rows]

    async def list_by_subject_entity(
        self, entity_id: int, user_id: int | None, limit: int = 50,
    ) -> list[dict]:
        """All memories whose subject entity is `entity_id` (canonical-chased),
        circle-filtered for the asker. Backs the /wissen entity drawer
        "Erinnerungen über diesen Knoten" (Phase 3c)."""
        clause, params = self._memory_circles_filter(user_id)
        sql = text(f"""
            SELECT id, content, category, importance, access_count,
                   created_at, last_accessed_at, subject_name, subject_entity_id
            FROM conversation_memories m
            WHERE is_active = true
              AND subject_entity_id IS NOT NULL
              AND subject_entity_id IN (
                  SELECT e.id FROM kg_entities e
                  WHERE COALESCE(e.canonical_id, e.id) = :eid
              )
              AND {clause}
            ORDER BY importance * confidence DESC
            LIMIT :limit
        """)
        rows = (await self.db.execute(
            sql, {"eid": entity_id, "limit": limit, **params}
        )).fetchall()
        return [{
            "id": row.id,
            "content": row.content,
            "category": row.category,
            "importance": row.importance,
            "access_count": row.access_count,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "last_accessed_at": row.last_accessed_at.isoformat() if row.last_accessed_at else None,
            "subject_name": row.subject_name,
            "subject_entity_id": row.subject_entity_id,
        } for row in rows]

    @staticmethod
    def _memory_circles_filter(
        user_id: int | None, enforce_circles: bool = False
    ) -> tuple[str, dict[str, Any]]:
        """
        Build the conversation_memories WHERE-fragment + bind params for circle access.

        AUTH_ENABLED=false: full bypass (single-user mode sees everything) — UNLESS
        ``enforce_circles`` (federation), which keeps the peer-scoped filter active.
        Anonymous-but-auth-on (`user_id is None`): only public-tier memories.
        Authenticated callers (or ``enforce_circles``): via
        `circle_sql.conversation_memories_circles_filter` (peer-scoped drops the
        owner + explicit-grant branches).
        """
        if not settings.auth_enabled and not enforce_circles:
            return ("TRUE", {})
        if user_id is None:
            return ("m.circle_tier = :asker_id_pub", {"asker_id_pub": TIER_PUBLIC})
        return conversation_memories_circles_filter(user_id, alias="m", peer_scoped=enforce_circles)

    async def retrieve_for_prompt(
        self,
        query: str,
        user_id: int | None = None,
        budget_chars: int | None = None,
    ) -> dict[str, list[dict]]:
        """
        Budget-aware memory retrieval organized by section.

        Returns memories partitioned into sections for structured prompt injection:
        - essential: High-importance facts/preferences (always included)
        - procedural: Behavioral rules
        - semantic: Query-relevant memories
        - episodic: Recent interaction episodes (if episodic memory enabled)

        The total character count of all sections is capped at budget_chars.
        Lane C: legacy `team_ids` parameter removed — circle_tier subsumes
        team scoping (parked for v2 named-circles).
        """
        budget = budget_chars or settings.memory_retrieval_budget_chars
        sections: dict[str, list[dict]] = {
            "essential": [],
            "procedural": [],
            "semantic": [],
            "episodic": [],
        }
        used_chars = 0
        seen_ids: set[int] = set()

        # --- 1. Essential memories (always injected) ---
        essential = await self.retrieve_essential(user_id=user_id)
        for m in essential:
            content_len = len(m["content"])
            if used_chars + content_len > budget:
                break
            sections["essential"].append(m)
            seen_ids.add(m["id"])
            used_chars += content_len

        # --- 2. Procedural memories (circle-filtered) ---
        circles_clause, circles_params = self._memory_circles_filter(user_id)
        procedural_sql = text(f"""
            SELECT id, content, category, importance, access_count, created_at,
                   source, circle_tier, trigger_pattern
            FROM conversation_memories m
            WHERE is_active = true
              AND category = 'procedural'
              AND {circles_clause}
            ORDER BY importance DESC
            LIMIT 10
        """)
        params: dict[str, Any] = dict(circles_params)

        try:
            result = await self.db.execute(procedural_sql, params)
            for row in result.fetchall():
                if row.id in seen_ids:
                    continue
                # trigger_pattern: skip if pattern is set and doesn't match query
                pattern = getattr(row, "trigger_pattern", None)
                if pattern:
                    try:
                        if not re.search(pattern, query, re.IGNORECASE):
                            # Essential procedural memories (importance >= 0.9) always pass
                            if (row.importance or 0) < settings.memory_essential_threshold:
                                continue
                    except re.error:
                        pass  # Invalid regex — include the memory anyway
                content_len = len(row.content)
                if used_chars + content_len > budget:
                    break
                sections["procedural"].append({
                    "id": row.id,
                    "content": row.content,
                    "category": row.category,
                    "importance": row.importance,
                    "source": getattr(row, "source", "llm_inferred"),
                    "circle_tier": int(getattr(row, "circle_tier", 0) or 0),
                })
                seen_ids.add(row.id)
                used_chars += content_len
        except Exception as e:
            logger.warning(f"Procedural memory retrieval failed: {e}")

        # --- 3. Semantic memories (query-relevant) ---
        if used_chars < budget:
            semantic = await self.retrieve(query, user_id=user_id)
            for m in semantic:
                if m["id"] in seen_ids:
                    continue
                content_len = len(m["content"])
                if used_chars + content_len > budget:
                    break
                created = None
                if m.get("created_at"):
                    try:
                        created = datetime.fromisoformat(m["created_at"])
                    except (ValueError, TypeError):
                        pass
                m["recency_score"] = round(self._recency_score(created), 3)
                sections["semantic"].append(m)
                seen_ids.add(m["id"])
                used_chars += content_len

        # --- 4. Episodic memories (recent interactions) ---
        if used_chars < budget and settings.memory_episodic_enabled:
            try:
                from services.episodic_memory_service import EpisodicMemoryService

                ep_svc = EpisodicMemoryService(self.db)
                episodes = await ep_svc.retrieve(
                    query, user_id=user_id, limit=3, threshold=0.4
                )
                for ep in episodes:
                    summary_len = len(ep["summary"])
                    if used_chars + summary_len > budget:
                        break
                    sections["episodic"].append(ep)
                    used_chars += summary_len
            except Exception as e:
                logger.warning(f"Episodic memory retrieval failed: {e}")

        total = sum(len(v) for v in sections.values())
        if total:
            logger.debug(
                f"Memory prompt: {total} items ({used_chars} chars) — "
                f"essential={len(sections['essential'])}, "
                f"procedural={len(sections['procedural'])}, "
                f"semantic={len(sections['semantic'])}, "
                f"episodic={len(sections['episodic'])}"
            )

        return sections
