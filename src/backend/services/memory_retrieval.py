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

from models.database import ConversationMemory
from utils.config import settings
from utils.llm_client import get_embed_client


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
    ) -> list[dict]:
        """
        Retrieve relevant memories using cosine similarity search.

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

        user_filter = "AND user_id = :user_id" if user_id is not None else ""

        sql = text(f"""
            SELECT
                id,
                content,
                category,
                importance,
                confidence,
                access_count,
                created_at,
                1 - (embedding <=> CAST(:embedding AS vector)) as similarity
            FROM conversation_memories
            WHERE is_active = true
              AND embedding IS NOT NULL
              {user_filter}
            ORDER BY (1 - (embedding <=> CAST(:embedding AS vector))) * importance * confidence DESC
            LIMIT :limit
        """)

        params: dict[str, Any] = {"embedding": embedding_str, "limit": limit}
        if user_id is not None:
            params["user_id"] = user_id

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
                    "similarity": round(sim, 3),
                })
                memory_ids.append(row.id)

        # Update access tracking
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
        """
        threshold = settings.memory_essential_threshold
        limit = limit or settings.memory_retrieval_limit

        user_filter = "AND user_id = :user_id" if user_id is not None else ""

        sql = text(f"""
            SELECT id, content, category, importance, access_count, created_at
            FROM conversation_memories
            WHERE is_active = true
              AND importance >= :threshold
              AND category != 'context'
              {user_filter}
            ORDER BY importance DESC
            LIMIT :limit
        """)

        params: dict[str, Any] = {"threshold": threshold, "limit": limit}
        if user_id is not None:
            params["user_id"] = user_id

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

    @staticmethod
    def _build_scope_filter(
        user_id: int | None,
        team_ids: list[str] | None,
    ) -> str:
        """Build SQL scope filter clause for multi-scope retrieval."""
        conditions = ["scope = 'global'"]
        if user_id is not None:
            conditions.append("(scope = 'user' AND user_id = :user_id)")
        if team_ids:
            conditions.append("(scope = 'team' AND team_id IN :team_ids)")
        return "AND (" + " OR ".join(conditions) + ")"

    async def retrieve_for_prompt(
        self,
        query: str,
        user_id: int | None = None,
        team_ids: list[str] | None = None,
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

        # --- 2. Procedural memories (scope: user + team + global) ---
        scope_filter = self._build_scope_filter(user_id, team_ids)
        procedural_sql = text(f"""
            SELECT id, content, category, importance, access_count, created_at,
                   source, scope, trigger_pattern
            FROM conversation_memories
            WHERE is_active = true
              AND category = 'procedural'
              {scope_filter}
            ORDER BY importance DESC
            LIMIT 10
        """)
        params: dict[str, Any] = {}
        if user_id is not None:
            params["user_id"] = user_id
        if team_ids:
            params["team_ids"] = tuple(team_ids)

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
                    "scope": getattr(row, "scope", "user"),
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
