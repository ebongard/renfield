"""
Conversation Memory Service — Long-term memory for the assistant.

Stores facts, preferences, instructions, and context extracted from
conversations. Uses pgvector embeddings for semantic retrieval so the
assistant can recall relevant memories across sessions.

Pattern follows IntentFeedbackService for embedding generation and
cosine similarity search via raw SQL (pgvector).
"""
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import MEMORY_CATEGORIES, ConversationMemory
from utils.config import settings
from utils.llm_client import get_default_client


class ConversationMemoryService:
    """
    Manages long-term conversation memories with semantic deduplication
    and retrieval via pgvector cosine similarity.
    """

    def __init__(self, db: AsyncSession):
        self.db = db
        self._ollama_client = None

    async def _get_ollama_client(self):
        """Lazy initialization of Ollama client for embeddings."""
        if self._ollama_client is None:
            self._ollama_client = get_default_client()
        return self._ollama_client

    async def _get_embedding(self, text_input: str) -> list[float]:
        """Generate embedding using Ollama (nomic-embed-text, 768 dims)."""
        client = await self._get_ollama_client()
        response = await client.embeddings(
            model=settings.ollama_embed_model,
            prompt=text_input
        )
        return response.embedding

    # =========================================================================
    # Save
    # =========================================================================

    async def save(
        self,
        content: str,
        category: str,
        user_id: int | None = None,
        importance: float = 0.5,
        source_session_id: str | None = None,
        source_message_id: int | None = None,
        expires_at: datetime | None = None,
    ) -> ConversationMemory | None:
        """
        Save a memory with deduplication.

        If a semantically similar memory already exists (above dedup threshold),
        updates access_count and last_accessed_at instead of creating a duplicate.

        Returns the new or existing memory, or None on error.
        """
        if category not in MEMORY_CATEGORIES:
            logger.warning(f"Invalid memory category: {category}")
            return None

        # Generate embedding
        embedding = None
        try:
            embedding = await self._get_embedding(content)
        except Exception as e:
            logger.warning(f"Could not generate embedding for memory: {e}")

        # Deduplication check
        if embedding:
            duplicate = await self._find_duplicate(embedding, user_id)
            if duplicate:
                duplicate.access_count = (duplicate.access_count or 0) + 1
                duplicate.last_accessed_at = datetime.utcnow()
                await self.db.commit()
                await self.db.refresh(duplicate)
                logger.debug(f"Memory deduplicated (id={duplicate.id}), access_count={duplicate.access_count}")
                return duplicate

        # Check max limit per user
        if user_id is not None:
            count = await self._count_active_for_user(user_id)
            if count >= settings.memory_max_per_user:
                # Deactivate the least important memory
                await self._deactivate_least_important(user_id)

        memory = ConversationMemory(
            content=content,
            category=category,
            user_id=user_id,
            embedding=embedding,
            importance=importance,
            source_session_id=source_session_id,
            source_message_id=source_message_id,
            expires_at=expires_at,
        )
        self.db.add(memory)
        await self.db.commit()
        await self.db.refresh(memory)

        logger.info(
            f"Memory saved: category={category}, "
            f"user_id={user_id}, id={memory.id}"
        )
        return memory

    # =========================================================================
    # Retrieve
    # =========================================================================

    async def retrieve(
        self,
        message: str,
        user_id: int | None = None,
        limit: int | None = None,
        threshold: float | None = None,
    ) -> list[dict]:
        """
        Retrieve relevant memories using cosine similarity search.

        Args:
            message: Query text to match against
            user_id: Optional filter by user
            limit: Max results (default: settings.memory_retrieval_limit)
            threshold: Min similarity (default: settings.memory_retrieval_threshold)

        Returns:
            List of dicts with id, content, category, importance, similarity
        """
        limit = limit or settings.memory_retrieval_limit
        threshold = threshold if threshold is not None else settings.memory_retrieval_threshold

        try:
            query_embedding = await self._get_embedding(message)
        except Exception as e:
            logger.warning(f"Could not generate query embedding for memory retrieval: {e}")
            return []

        embedding_str = f"[{','.join(map(str, query_embedding))}]"

        # Build user filter
        user_filter = "AND user_id = :user_id" if user_id is not None else ""

        sql = text(f"""
            SELECT
                id,
                content,
                category,
                importance,
                access_count,
                created_at,
                1 - (embedding <=> CAST(:embedding AS vector)) as similarity
            FROM conversation_memories
            WHERE is_active = true
              AND embedding IS NOT NULL
              {user_filter}
            ORDER BY (1 - (embedding <=> CAST(:embedding AS vector))) * importance DESC
            LIMIT :limit
        """)

        params = {
            "embedding": embedding_str,
            "limit": limit,
        }
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

        # Update access tracking for retrieved memories
        if memory_ids:
            await self.db.execute(
                update(ConversationMemory)
                .where(ConversationMemory.id.in_(memory_ids))
                .values(
                    access_count=ConversationMemory.access_count + 1,
                    last_accessed_at=datetime.utcnow(),
                )
            )
            await self.db.commit()

        return memories

    # =========================================================================
    # Cleanup
    # =========================================================================

    async def cleanup(self) -> dict:
        """
        Deactivate expired and decayed memories.

        Returns counts of deactivated memories by reason.
        """
        now = datetime.utcnow()
        counts = {"expired": 0, "decayed": 0, "over_limit": 0}

        # 1. Expired memories (expires_at < now)
        result = await self.db.execute(
            update(ConversationMemory)
            .where(
                ConversationMemory.is_active == True,  # noqa: E712
                ConversationMemory.expires_at != None,  # noqa: E711
                ConversationMemory.expires_at < now,
            )
            .values(is_active=False)
        )
        counts["expired"] = result.rowcount

        # 2. Context decay — context-category memories not accessed recently
        decay_cutoff = now - timedelta(days=settings.memory_context_decay_days)
        result = await self.db.execute(
            update(ConversationMemory)
            .where(
                ConversationMemory.is_active == True,  # noqa: E712
                ConversationMemory.category == "context",
                ConversationMemory.last_accessed_at != None,  # noqa: E711
                ConversationMemory.last_accessed_at < decay_cutoff,
            )
            .values(is_active=False)
        )
        counts["decayed"] = result.rowcount

        # Also decay context memories never accessed and created before cutoff
        result = await self.db.execute(
            update(ConversationMemory)
            .where(
                ConversationMemory.is_active == True,  # noqa: E712
                ConversationMemory.category == "context",
                ConversationMemory.last_accessed_at == None,  # noqa: E711
                ConversationMemory.created_at < decay_cutoff,
            )
            .values(is_active=False)
        )
        counts["decayed"] += result.rowcount

        await self.db.commit()

        total = sum(counts.values())
        if total > 0:
            logger.info(f"Memory cleanup: {counts}")

        return counts

    # =========================================================================
    # Delete / List
    # =========================================================================

    async def delete(self, memory_id: int) -> bool:
        """Soft-delete a memory by setting is_active=False."""
        result = await self.db.execute(
            select(ConversationMemory).where(ConversationMemory.id == memory_id)
        )
        memory = result.scalar_one_or_none()
        if not memory:
            return False

        memory.is_active = False
        await self.db.commit()
        return True

    async def list_for_user(
        self,
        user_id: int,
        category: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """List active memories for a user with optional category filter."""
        query = (
            select(ConversationMemory)
            .where(
                ConversationMemory.user_id == user_id,
                ConversationMemory.is_active == True,  # noqa: E712
            )
            .order_by(ConversationMemory.created_at.desc())
        )

        if category:
            query = query.where(ConversationMemory.category == category)

        query = query.offset(offset).limit(limit)
        result = await self.db.execute(query)
        memories = result.scalars().all()

        return [
            {
                "id": m.id,
                "content": m.content,
                "category": m.category,
                "importance": m.importance,
                "access_count": m.access_count,
                "created_at": m.created_at.isoformat() if m.created_at else None,
                "last_accessed_at": m.last_accessed_at.isoformat() if m.last_accessed_at else None,
            }
            for m in memories
        ]

    # =========================================================================
    # Internal Helpers
    # =========================================================================

    async def _find_duplicate(
        self,
        embedding: list[float],
        user_id: int | None,
    ) -> ConversationMemory | None:
        """Find an existing memory that is semantically too similar (duplicate)."""
        threshold = settings.memory_dedup_threshold
        embedding_str = f"[{','.join(map(str, embedding))}]"

        user_filter = "AND user_id = :user_id" if user_id is not None else ""

        sql = text(f"""
            SELECT id,
                   1 - (embedding <=> CAST(:embedding AS vector)) as similarity
            FROM conversation_memories
            WHERE is_active = true
              AND embedding IS NOT NULL
              {user_filter}
            ORDER BY embedding <=> CAST(:embedding AS vector)
            LIMIT 1
        """)

        params = {"embedding": embedding_str}
        if user_id is not None:
            params["user_id"] = user_id

        result = await self.db.execute(sql, params)
        row = result.fetchone()

        if row and float(row.similarity) >= threshold:
            # Fetch the full ORM object
            mem_result = await self.db.execute(
                select(ConversationMemory).where(ConversationMemory.id == row.id)
            )
            return mem_result.scalar_one_or_none()

        return None

    async def _count_active_for_user(self, user_id: int) -> int:
        """Count active memories for a user."""
        result = await self.db.execute(
            select(func.count(ConversationMemory.id))
            .where(
                ConversationMemory.user_id == user_id,
                ConversationMemory.is_active == True,  # noqa: E712
            )
        )
        return result.scalar() or 0

    async def _deactivate_least_important(self, user_id: int) -> None:
        """Deactivate the least important active memory for a user."""
        result = await self.db.execute(
            select(ConversationMemory)
            .where(
                ConversationMemory.user_id == user_id,
                ConversationMemory.is_active == True,  # noqa: E712
            )
            .order_by(ConversationMemory.importance.asc(), ConversationMemory.access_count.asc())
            .limit(1)
        )
        memory = result.scalar_one_or_none()
        if memory:
            memory.is_active = False
            await self.db.commit()
            logger.debug(f"Deactivated least important memory id={memory.id} for user {user_id}")
