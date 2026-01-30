"""
Intent Feedback Service — Semantic correction learning.

Stores user corrections for wrong intent classifications, agent tool choices,
and complexity detection. Uses pgvector embeddings for cosine similarity search
to inject relevant corrections as few-shot examples into future prompts.

Feedback types:
  - "intent": Wrong intent classification (Single-Intent path)
  - "agent_tool": Wrong tool choice in Agent Loop
  - "complexity": Wrong simple/complex classification
"""
import time
from typing import Dict, List, Optional

from loguru import logger
from sqlalchemy import select, func, text, delete
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import IntentCorrection
from utils.config import settings


class IntentFeedbackService:
    """
    Manages intent correction feedback and semantic matching.

    Stores corrections with embeddings, finds semantically similar past
    corrections, and formats them for prompt injection.
    """

    # Cache for correction counts (avoid DB query on every intent extraction)
    _count_cache: Dict[str, tuple] = {}  # {feedback_type: (count, timestamp)}
    _CACHE_TTL = 300  # seconds (corrections are rare, 5 min is fine)

    def __init__(self, db: AsyncSession):
        self.db = db
        self._ollama_client = None

    async def _get_ollama_client(self):
        """Lazy initialization of Ollama client for embeddings."""
        if self._ollama_client is None:
            import ollama
            self._ollama_client = ollama.AsyncClient(host=settings.ollama_url)
        return self._ollama_client

    async def _get_embedding(self, text: str) -> List[float]:
        """Generate embedding using Ollama (nomic-embed-text, 768 dims)."""
        client = await self._get_ollama_client()
        response = await client.embeddings(
            model=settings.ollama_embed_model,
            prompt=text
        )
        return response.embedding

    async def _has_corrections(self, feedback_type: str) -> bool:
        """Quick check if any corrections exist (cached for performance)."""
        now = time.time()
        cached = self._count_cache.get(feedback_type)
        if cached and (now - cached[1]) < self._CACHE_TTL:
            return cached[0] > 0

        result = await self.db.execute(
            select(func.count(IntentCorrection.id))
            .where(IntentCorrection.feedback_type == feedback_type)
        )
        count = result.scalar() or 0
        IntentFeedbackService._count_cache[feedback_type] = (count, now)
        return count > 0

    # =========================================================================
    # Save
    # =========================================================================

    async def save_correction(
        self,
        message_text: str,
        feedback_type: str,
        original_value: str,
        corrected_value: str,
        user_id: Optional[int] = None,
        context: Optional[Dict] = None,
    ) -> IntentCorrection:
        """
        Save a correction with embedding for future semantic matching.

        Args:
            message_text: Original user message
            feedback_type: "intent", "agent_tool", or "complexity"
            original_value: What the system chose
            corrected_value: What the user corrected to
            user_id: Optional user ID
            context: Optional additional context (agent steps, etc.)
        """
        embedding = None
        try:
            embedding = await self._get_embedding(message_text)
        except Exception as e:
            logger.warning(f"⚠️ Could not generate embedding for correction: {e}")

        correction = IntentCorrection(
            message_text=message_text,
            feedback_type=feedback_type,
            original_value=original_value,
            corrected_value=corrected_value,
            embedding=embedding,
            context=context,
            user_id=user_id,
        )
        self.db.add(correction)
        await self.db.commit()
        await self.db.refresh(correction)

        # Invalidate count cache
        IntentFeedbackService._count_cache.pop(feedback_type, None)

        logger.info(
            f"📝 Correction saved: {feedback_type} "
            f"'{original_value}' → '{corrected_value}' "
            f"for message: '{message_text[:60]}...'"
        )
        return correction

    # =========================================================================
    # Find Similar
    # =========================================================================

    async def find_similar_corrections(
        self,
        message: str,
        feedback_type: str,
        limit: int = 3,
        threshold: float = 0.75,
    ) -> List[Dict]:
        """
        Find semantically similar past corrections using cosine similarity.

        Args:
            message: Current user message to match against
            feedback_type: Filter by correction type
            limit: Max number of results
            threshold: Minimum cosine similarity (0-1)

        Returns:
            List of dicts with message_text, original_value, corrected_value, similarity
        """
        # Early exit if no corrections exist
        if not await self._has_corrections(feedback_type):
            return []

        try:
            query_embedding = await self._get_embedding(message)
        except Exception as e:
            logger.warning(f"⚠️ Could not generate query embedding: {e}")
            return []

        embedding_str = f"[{','.join(map(str, query_embedding))}]"

        sql = text("""
            SELECT
                id,
                message_text,
                original_value,
                corrected_value,
                context,
                1 - (embedding <=> CAST(:embedding AS vector)) as similarity
            FROM intent_corrections
            WHERE feedback_type = :feedback_type
              AND embedding IS NOT NULL
            ORDER BY embedding <=> CAST(:embedding AS vector)
            LIMIT :limit
        """)

        result = await self.db.execute(sql, {
            "embedding": embedding_str,
            "feedback_type": feedback_type,
            "limit": limit,
        })
        rows = result.fetchall()

        corrections = []
        for row in rows:
            sim = float(row.similarity) if row.similarity else 0
            if sim >= threshold:
                corrections.append({
                    "id": row.id,
                    "message_text": row.message_text,
                    "original_value": row.original_value,
                    "corrected_value": row.corrected_value,
                    "context": row.context,
                    "similarity": round(sim, 3),
                })

        return corrections

    # =========================================================================
    # Format for Prompt Injection
    # =========================================================================

    def format_as_few_shot(self, corrections: List[Dict], lang: str = "de") -> str:
        """
        Format intent corrections as few-shot examples for the intent prompt.

        Returns empty string if no corrections (placeholder cleanly empty).
        """
        if not corrections:
            return ""

        if lang == "en":
            header = "LEARNING EXAMPLES from previous corrections (use these to avoid repeating mistakes):"
            template = '- "{msg}" → Wrong: {orig} → Correct: {corr}'
        else:
            header = "LERNBEISPIELE aus früheren Korrekturen (nutze diese um Fehler nicht zu wiederholen):"
            template = '- "{msg}" → Falsch: {orig} → Richtig: {corr}'

        lines = [header]
        for c in corrections:
            lines.append(template.format(
                msg=c["message_text"][:80],
                orig=c["original_value"],
                corr=c["corrected_value"],
            ))

        return "\n".join(lines)

    def format_agent_corrections(self, corrections: List[Dict], lang: str = "de") -> str:
        """
        Format agent tool corrections for the agent prompt.

        Returns empty string if no corrections.
        """
        if not corrections:
            return ""

        if lang == "en":
            header = "TOOL CORRECTIONS from previous queries (avoid these mistakes):"
            template = '- For "{msg}": Use {corr} instead of {orig}'
        else:
            header = "TOOL-KORREKTUREN aus früheren Anfragen (vermeide diese Fehler):"
            template = '- Bei "{msg}": Verwende {corr} statt {orig}'

        lines = [header]
        for c in corrections:
            lines.append(template.format(
                msg=c["message_text"][:80],
                orig=c["original_value"],
                corr=c["corrected_value"],
            ))

        return "\n".join(lines)

    # =========================================================================
    # Complexity Override
    # =========================================================================

    async def check_complexity_override(self, message: str) -> Optional[bool]:
        """
        Check if a similar message was corrected for complexity classification.

        Returns:
            True if corrected to "complex" (use Agent Loop)
            False if corrected to "simple" (use Single-Intent)
            None if no matching correction found
        """
        similar = await self.find_similar_corrections(
            message, feedback_type="complexity", limit=1, threshold=0.80
        )
        if not similar:
            return None

        corrected = similar[0]["corrected_value"]
        if corrected == "complex":
            return True
        elif corrected == "simple":
            return False
        return None

    # =========================================================================
    # Admin Operations
    # =========================================================================

    async def list_corrections(
        self,
        feedback_type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict]:
        """List corrections with optional type filter."""
        query = select(IntentCorrection).order_by(IntentCorrection.created_at.desc())

        if feedback_type:
            query = query.where(IntentCorrection.feedback_type == feedback_type)

        query = query.offset(offset).limit(limit)
        result = await self.db.execute(query)
        corrections = result.scalars().all()

        return [
            {
                "id": c.id,
                "message_text": c.message_text,
                "feedback_type": c.feedback_type,
                "original_value": c.original_value,
                "corrected_value": c.corrected_value,
                "context": c.context,
                "user_id": c.user_id,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in corrections
        ]

    async def delete_correction(self, correction_id: int) -> bool:
        """Delete a correction by ID."""
        result = await self.db.execute(
            select(IntentCorrection).where(IntentCorrection.id == correction_id)
        )
        correction = result.scalar_one_or_none()
        if not correction:
            return False

        feedback_type = correction.feedback_type
        await self.db.delete(correction)
        await self.db.commit()

        # Invalidate cache
        IntentFeedbackService._count_cache.pop(feedback_type, None)
        return True

    async def get_correction_count(self, feedback_type: Optional[str] = None) -> int:
        """Get total correction count, optionally filtered by type."""
        query = select(func.count(IntentCorrection.id))
        if feedback_type:
            query = query.where(IntentCorrection.feedback_type == feedback_type)

        result = await self.db.execute(query)
        return result.scalar() or 0
