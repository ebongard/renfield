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

# Domain keyword boosting: when the message contains one of these keywords
# (case-insensitive), the corresponding role gets a similarity boost.
# This fixes structural ambiguity where "Zeige mir alle Releases" matches
# "Zeige mir alle Incidents" because the sentence structure is identical.
_KEYWORD_BOOST: dict[str, list[str]] = {}
_KEYWORD_BOOST_AMOUNT = 0.15


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

        # Build keyword boost map from role descriptions.
        # If a role's description mentions domain-specific terms, those become
        # keywords that boost the role when found in the user's message.
        global _KEYWORD_BOOST
        _KEYWORD_BOOST.clear()
        for name, role in roles.items():
            keywords = role.keyword_boost if hasattr(role, 'keyword_boost') and role.keyword_boost else []
            if keywords:
                _KEYWORD_BOOST[name] = [k.lower() for k in keywords]

        logger.info(
            f"SemanticRouter initialized: {len(self._role_embeddings)} roles, "
            f"{total_utterances} utterances"
            + (f", {sum(len(v) for v in _KEYWORD_BOOST.values())} keyword boosts" if _KEYWORD_BOOST else "")
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

        # Apply keyword boosting: if the message contains domain-specific
        # keywords, re-evaluate with boosted scores. This fixes cases where
        # "Zeige mir alle Releases" matches "Zeige mir alle Incidents" because
        # the sentence structure is identical but the domain keyword differs.
        if _KEYWORD_BOOST:
            msg_lower = message.lower()
            boosted_role = None
            boosted_sim = 0.0
            for role_name, keywords in _KEYWORD_BOOST.items():
                if role_name not in self._role_embeddings:
                    continue
                if any(kw in msg_lower for kw in keywords):
                    # Find best sim for this specific role
                    for emb in self._role_embeddings[role_name]:
                        emb_arr = np.array(emb)
                        emb_norm = np.linalg.norm(emb_arr)
                        if emb_norm < 1e-10:
                            continue
                        sim = float(np.dot(query_emb, emb_arr) / (query_norm * emb_norm))
                        sim += _KEYWORD_BOOST_AMOUNT  # boost
                        if sim > boosted_sim:
                            boosted_sim = sim
                            boosted_role = role_name
            if boosted_role and boosted_sim >= self.threshold and boosted_role != best_role:
                logger.info(
                    f"SemanticRouter keyword boost: '{best_role}' ({best_sim:.3f}) → "
                    f"'{boosted_role}' ({boosted_sim:.3f})"
                )
                return boosted_role, boosted_sim

        if best_sim >= self.threshold:
            return best_role, best_sim
        return None, best_sim
