"""
Episodic Memory Service — Records of past interactions.

Creates lightweight episodes from tool call data (no LLM needed),
provides recency-aware retrieval via pgvector, and batch-summarizes
old episodes into semantic facts for the ConversationMemoryService.
"""
import math
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    MEMORY_CATEGORY_FACT,
    EpisodicMemory,
)
from utils.config import settings
from utils.llm_client import get_embed_client


class EpisodicMemoryService:
    """
    Manages episodic memories — lightweight records of past interactions
    with recency-aware retrieval and periodic summarization.
    """

    def __init__(self, db: AsyncSession):
        self.db = db
        self._ollama_client = None

    async def _get_ollama_client(self):
        if self._ollama_client is None:
            self._ollama_client = get_embed_client()
        return self._ollama_client

    async def _get_embedding(self, text_input: str) -> list[float]:
        client = await self._get_ollama_client()
        response = await client.embeddings(
            model=settings.ollama_embed_model,
            prompt=text_input,
        )
        return response.embedding

    # =========================================================================
    # Create
    # =========================================================================

    async def create_episode(
        self,
        user_id: int | None,
        session_id: str | None,
        summary: str,
        topic: str | None = None,
        entities: dict | None = None,
        tools_used: list[str] | None = None,
        outcome: str | None = None,
        importance: float = 0.5,
    ) -> EpisodicMemory | None:
        """
        Create an episodic memory from a completed interaction.

        Args:
            user_id: Owner user ID
            session_id: Conversation/session ID
            summary: Human-readable summary of what happened
            topic: Domain topic (release_status, jira_search, etc.)
            entities: Extracted entities {release_id: "...", jira_key: "..."}
            tools_used: List of tool names used
            outcome: "success" / "error" / "no_result"
            importance: 0.0-1.0 importance score

        Returns:
            The created episode, or None on error.
        """
        # Check per-user limit
        if user_id is not None:
            count = await self._count_active(user_id)
            if count >= settings.memory_episodic_max_per_user:
                await self._deactivate_oldest(user_id)

        # Generate embedding for the summary
        embedding = None
        try:
            embedding = await self._get_embedding(summary)
        except Exception as e:
            logger.warning(f"Could not generate episode embedding: {e}")

        episode = EpisodicMemory(
            user_id=user_id,
            session_id=session_id,
            summary=summary,
            topic=topic,
            entities=entities,
            tools_used=tools_used,
            outcome=outcome,
            embedding=embedding,
            importance=importance,
        )
        self.db.add(episode)
        await self.db.commit()
        await self.db.refresh(episode)

        logger.debug(
            f"Episode created: topic={topic}, tools={tools_used}, id={episode.id}"
        )
        return episode

    # =========================================================================
    # Retrieve
    # =========================================================================

    async def retrieve(
        self,
        query: str,
        user_id: int | None = None,
        limit: int = 5,
        threshold: float = 0.5,
        recency_half_life_days: float = 14.0,
    ) -> list[dict]:
        """
        Retrieve relevant episodes using cosine similarity with recency scoring.

        The final score is: similarity * importance * recency_factor
        where recency_factor = exp(-0.693 * age_days / half_life)

        Args:
            query: Search query text
            user_id: Filter by user
            limit: Max results
            threshold: Minimum similarity before recency weighting
            recency_half_life_days: Half-life for recency decay

        Returns:
            List of episode dicts with similarity and recency_score.
        """
        try:
            query_embedding = await self._get_embedding(query)
        except Exception as e:
            logger.warning(f"Could not generate query embedding for episode retrieval: {e}")
            return []

        embedding_str = f"[{','.join(map(str, query_embedding))}]"
        user_filter = "AND user_id = :user_id" if user_id is not None else ""

        # Fetch more than needed, then re-rank with recency in Python
        fetch_limit = limit * 3

        sql = text(f"""
            SELECT
                id, summary, topic, entities, tools_used, outcome,
                importance, access_count, created_at,
                1 - (embedding <=> CAST(:embedding AS vector)) as similarity
            FROM episodic_memories
            WHERE is_active = true
              AND embedding IS NOT NULL
              {user_filter}
            ORDER BY (1 - (embedding <=> CAST(:embedding AS vector))) DESC
            LIMIT :limit
        """)

        params: dict = {"embedding": embedding_str, "limit": fetch_limit}
        if user_id is not None:
            params["user_id"] = user_id

        result = await self.db.execute(sql, params)
        rows = result.fetchall()

        now = datetime.now(UTC).replace(tzinfo=None)
        decay_constant = 0.693 / recency_half_life_days  # ln(2) / half_life

        scored = []
        for row in rows:
            sim = float(row.similarity) if row.similarity else 0
            if sim < threshold:
                continue

            age_days = max((now - row.created_at).total_seconds() / 86400, 0) if row.created_at else 0
            recency = math.exp(-decay_constant * age_days)
            final_score = sim * (row.importance or 0.5) * recency

            scored.append({
                "id": row.id,
                "summary": row.summary,
                "topic": row.topic,
                "entities": row.entities,
                "tools_used": row.tools_used,
                "outcome": row.outcome,
                "importance": row.importance,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "similarity": round(sim, 3),
                "recency_score": round(recency, 3),
                "final_score": round(final_score, 3),
            })

        # Sort by final_score descending, take top `limit`
        scored.sort(key=lambda x: x["final_score"], reverse=True)
        top = scored[:limit]

        # Update access tracking
        if top:
            episode_ids = [e["id"] for e in top]
            await self.db.execute(
                update(EpisodicMemory)
                .where(EpisodicMemory.id.in_(episode_ids))
                .values(
                    access_count=EpisodicMemory.access_count + 1,
                    last_accessed_at=now,
                )
            )
            await self.db.commit()

        return top

    async def retrieve_recent(
        self,
        user_id: int,
        limit: int = 5,
    ) -> list[dict]:
        """Retrieve most recent episodes for a user (no embedding needed)."""
        result = await self.db.execute(
            select(EpisodicMemory)
            .where(
                EpisodicMemory.user_id == user_id,
                EpisodicMemory.is_active == True,  # noqa: E712
            )
            .order_by(EpisodicMemory.created_at.desc())
            .limit(limit)
        )
        episodes = result.scalars().all()

        return [
            {
                "id": e.id,
                "summary": e.summary,
                "topic": e.topic,
                "entities": e.entities,
                "tools_used": e.tools_used,
                "outcome": e.outcome,
                "importance": e.importance,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in episodes
        ]

    # =========================================================================
    # Summarize (batch episodes -> semantic facts)
    # =========================================================================

    async def summarize_old(
        self,
        user_id: int,
        age_days: int | None = None,
    ) -> int:
        """
        Batch-summarize old episodes into semantic facts.

        When a user has more episodes than the threshold, the oldest episodes
        beyond the threshold are deactivated and a summary fact is created
        in ConversationMemory.

        Returns count of episodes summarized.
        """
        threshold = settings.memory_episodic_summarize_threshold
        age_days = age_days or settings.memory_episodic_decay_days

        # Count active episodes
        count_result = await self.db.execute(
            select(func.count(EpisodicMemory.id))
            .where(
                EpisodicMemory.user_id == user_id,
                EpisodicMemory.is_active == True,  # noqa: E712
            )
        )
        total = count_result.scalar() or 0

        if total <= threshold:
            return 0

        # Get the oldest episodes beyond threshold
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=age_days)
        result = await self.db.execute(
            select(EpisodicMemory)
            .where(
                EpisodicMemory.user_id == user_id,
                EpisodicMemory.is_active == True,  # noqa: E712
                EpisodicMemory.created_at < cutoff,
            )
            .order_by(EpisodicMemory.created_at.asc())
            .limit(total - threshold)
        )
        old_episodes = result.scalars().all()

        if not old_episodes:
            return 0

        # Group by topic for coherent summaries
        by_topic: dict[str, list] = {}
        for ep in old_episodes:
            topic = ep.topic or "general"
            by_topic.setdefault(topic, []).append(ep)

        from services.conversation_memory_service import ConversationMemoryService

        mem_svc = ConversationMemoryService(self.db)
        summarized = 0

        for topic, episodes in by_topic.items():
            summaries = [ep.summary for ep in episodes]
            fact_content = f"Past interactions about {topic}: " + "; ".join(summaries[:10])
            if len(summaries) > 10:
                fact_content += f" (and {len(summaries) - 10} more)"

            if len(fact_content) > 500:
                fact_content = fact_content[:497] + "..."

            await mem_svc.save(
                content=fact_content,
                category=MEMORY_CATEGORY_FACT,
                user_id=user_id,
                importance=0.6,
                source_session_id=None,
            )

            for ep in episodes:
                ep.is_active = False
                summarized += 1

        await self.db.commit()
        if summarized:
            logger.info(f"Summarized {summarized} episodes for user {user_id}")

        return summarized

    # =========================================================================
    # Cleanup
    # =========================================================================

    async def cleanup(self) -> dict:
        """
        Deactivate old and low-importance episodes.

        Returns counts by reason.
        """
        decay_days = settings.memory_episodic_decay_days
        now = datetime.now(UTC).replace(tzinfo=None)
        counts = {"decayed": 0, "old": 0}

        # 1. Episodes not accessed within decay period
        decay_cutoff = now - timedelta(days=decay_days)
        result = await self.db.execute(
            update(EpisodicMemory)
            .where(
                EpisodicMemory.is_active == True,  # noqa: E712
                EpisodicMemory.last_accessed_at != None,  # noqa: E711
                EpisodicMemory.last_accessed_at < decay_cutoff,
            )
            .values(is_active=False)
        )
        counts["decayed"] = result.rowcount

        # 2. Very old episodes (>365 days) regardless of access
        max_cutoff = now - timedelta(days=365)
        result = await self.db.execute(
            update(EpisodicMemory)
            .where(
                EpisodicMemory.is_active == True,  # noqa: E712
                EpisodicMemory.created_at < max_cutoff,
            )
            .values(is_active=False)
        )
        counts["old"] = result.rowcount

        await self.db.commit()

        total = sum(counts.values())
        if total > 0:
            logger.info(f"Episodic cleanup: {counts}")

        return counts

    # =========================================================================
    # Internal helpers
    # =========================================================================

    async def _count_active(self, user_id: int) -> int:
        result = await self.db.execute(
            select(func.count(EpisodicMemory.id))
            .where(
                EpisodicMemory.user_id == user_id,
                EpisodicMemory.is_active == True,  # noqa: E712
            )
        )
        return result.scalar() or 0

    async def _deactivate_oldest(self, user_id: int) -> None:
        """Deactivate the oldest episode for a user."""
        result = await self.db.execute(
            select(EpisodicMemory.id)
            .where(
                EpisodicMemory.user_id == user_id,
                EpisodicMemory.is_active == True,  # noqa: E712
            )
            .order_by(EpisodicMemory.importance.asc(), EpisodicMemory.created_at.asc())
            .limit(1)
        )
        oldest_id = result.scalar()
        if oldest_id:
            await self.db.execute(
                update(EpisodicMemory)
                .where(EpisodicMemory.id == oldest_id)
                .values(is_active=False)
            )
            await self.db.commit()
