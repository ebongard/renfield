"""
Semantic Router — Embedding-based fast classification for agent routing.

Pre-computes embeddings for role utterances at init time.
At classification time, embeds the user message and finds the nearest role
via cosine similarity. Falls back to None if no role exceeds the threshold.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
from loguru import logger

from utils.config import settings
from utils.llm_client import get_embed_client

if TYPE_CHECKING:
    from services.agent_router import AgentRole


class SemanticRouter:
    """Embedding-based fast classifier for agent routing."""

    def __init__(self, threshold: float = 0.75):
        self.threshold = threshold
        self._role_embeddings: dict[str, list[list[float]]] = {}  # role_name -> list of embeddings
        self._ollama_client = None
        self._initialized = False

    async def initialize(self, roles: dict[str, AgentRole]) -> None:
        """Pre-compute embeddings for all role utterances."""
        client = get_embed_client()
        for name, role in roles.items():
            if not role.utterances:
                continue
            embeddings = []
            for utterance in role.utterances:
                try:
                    response = await client.embeddings(
                        model=settings.ollama_embed_model,
                        prompt=utterance,
                    )
                    embeddings.append(response.embedding)
                except Exception as e:
                    logger.warning(f"Failed to embed utterance for role {name}: {e}")
            if embeddings:
                self._role_embeddings[name] = embeddings
        self._ollama_client = client
        self._initialized = True
        total_utterances = sum(len(v) for v in self._role_embeddings.values())
        logger.info(
            f"SemanticRouter initialized: {len(self._role_embeddings)} roles, "
            f"{total_utterances} utterances"
        )

    async def classify(self, message: str) -> tuple[str | None, float]:
        """Classify a message by cosine similarity to role utterances.

        Returns (role_name, similarity) or (None, 0.0) if below threshold.
        """
        if not self._initialized or not self._role_embeddings:
            return None, 0.0

        try:
            response = await self._ollama_client.embeddings(
                model=settings.ollama_embed_model,
                prompt=message,
            )
            query_emb = np.array(response.embedding)
        except Exception as e:
            logger.warning(f"SemanticRouter embed failed: {e}")
            return None, 0.0

        best_role = None
        best_sim = 0.0

        query_norm = np.linalg.norm(query_emb)
        if query_norm < 1e-10:
            return None, 0.0

        for role_name, embeddings in self._role_embeddings.items():
            for emb in embeddings:
                emb_arr = np.array(emb)
                emb_norm = np.linalg.norm(emb_arr)
                if emb_norm < 1e-10:
                    continue
                sim = float(np.dot(query_emb, emb_arr) / (query_norm * emb_norm))
                if sim > best_sim:
                    best_sim = sim
                    best_role = role_name

        if best_sim >= self.threshold:
            return best_role, best_sim
        return None, best_sim
