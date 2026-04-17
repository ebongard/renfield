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
    """Embedding-based fast classifier for agent routing.

    Indexes both role-level utterances and per-role ``sub_intent``
    utterances so deliverable-style sub-intents (``my_dashboard``,
    ``status_report``, etc.) can win over the parent role when a user
    message lexically resembles both.
    """

    def __init__(self, threshold: float = 0.75):
        self.threshold = threshold
        self._role_embeddings: dict[str, list[list[float]]] = {}  # role_name -> list of embeddings
        # Same shape as ``_role_embeddings`` but keyed by (role, sub_intent).
        # Populated from ``role.sub_intent_definitions[*].utterances``.
        self._sub_intent_embeddings: dict[tuple[str, str], list[list[float]]] = {}
        self._ollama_client = None
        self._initialized = False

    async def initialize(self, roles: dict[str, AgentRole]) -> None:
        """Pre-compute embeddings for role + sub_intent utterances."""
        client = get_embed_client()

        async def _embed_all(phrases: list[str], label: str) -> list[list[float]]:
            out: list[list[float]] = []
            for phrase in phrases:
                try:
                    response = await client.embeddings(
                        model=settings.ollama_embed_model,
                        prompt=phrase,
                    )
                    out.append(response.embedding)
                except Exception as e:
                    logger.warning(f"Failed to embed utterance for {label}: {e}")
            return out

        for name, role in roles.items():
            if role.utterances:
                role_embs = await _embed_all(role.utterances, f"role {name}")
                if role_embs:
                    self._role_embeddings[name] = role_embs

            # Sub-intent utterances — indexed separately so a matching
            # sub_intent can override the parent role at classification
            # time. We read the utterances directly off the config since
            # AgentRole currently carries sub_intent_definitions as the
            # raw description/dispatch dict.
            for si_name, si_def in (role.sub_intent_definitions or {}).items():
                if not isinstance(si_def, dict):
                    continue
                si_utters = si_def.get("utterances") or []
                if not isinstance(si_utters, list) or not si_utters:
                    continue
                si_embs = await _embed_all(
                    [str(u) for u in si_utters], f"sub_intent {name}/{si_name}",
                )
                if si_embs:
                    self._sub_intent_embeddings[(name, si_name)] = si_embs

        self._ollama_client = client
        self._initialized = True
        total_utterances = (
            sum(len(v) for v in self._role_embeddings.values())
            + sum(len(v) for v in self._sub_intent_embeddings.values())
        )

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

    async def classify(
        self, message: str,
    ) -> tuple[str | None, str | None, float]:
        """Classify a message by cosine similarity to role utterances.

        Returns ``(role_name, sub_intent_name_or_None, similarity)``.
        Returns ``(None, None, best_sim)`` when the best match is below
        the threshold.

        Sub-intent utterances compete on equal footing with role
        utterances, so a sub_intent matching closer than its parent role
        wins — the caller can then bypass the agent loop via the
        sub-intent dispatch hook.
        """
        if not self._initialized or (
            not self._role_embeddings and not self._sub_intent_embeddings
        ):
            return None, None, 0.0

        try:
            response = await self._ollama_client.embeddings(
                model=settings.ollama_embed_model,
                prompt=message,
            )
            query_emb = np.array(response.embedding)
        except Exception as e:
            logger.warning(f"SemanticRouter embed failed: {e}")
            return None, None, 0.0

        best_role: str | None = None
        best_sub_intent: str | None = None
        best_sim = 0.0

        query_norm = np.linalg.norm(query_emb)
        if query_norm < 1e-10:
            return None, None, 0.0

        def _best_of(
            embeddings: list, current_best_sim: float,
        ) -> float:
            """Return the best cosine similarity found in ``embeddings``."""
            best = current_best_sim
            for emb in embeddings:
                emb_arr = np.array(emb)
                emb_norm = np.linalg.norm(emb_arr)
                if emb_norm < 1e-10:
                    continue
                sim = float(np.dot(query_emb, emb_arr) / (query_norm * emb_norm))
                if sim > best:
                    best = sim
            return best

        # Role-level utterances
        for role_name, embeddings in self._role_embeddings.items():
            sim = _best_of(embeddings, 0.0)
            if sim > best_sim:
                best_sim = sim
                best_role = role_name
                best_sub_intent = None

        # Sub-intent utterances — can override a role-level match when
        # the sub_intent phrasing is closer. Ties go to role level
        # (compare strict ``>`` to preserve role match on equal sim).
        for (role_name, si_name), embeddings in self._sub_intent_embeddings.items():
            sim = _best_of(embeddings, 0.0)
            if sim > best_sim:
                best_sim = sim
                best_role = role_name
                best_sub_intent = si_name

        # Apply keyword boosting: if the message contains domain-specific
        # keywords, re-evaluate with boosted scores. This fixes cases where
        # "Zeige mir alle Releases" matches "Zeige mir alle Incidents" because
        # the sentence structure is identical but the domain keyword differs.
        # Keyword boost operates on role-level only (sub_intents rely on
        # their explicit utterances rather than keyword hints).
        if _KEYWORD_BOOST:
            msg_lower = message.lower()
            boosted_role = None
            boosted_sim = 0.0
            for role_name, keywords in _KEYWORD_BOOST.items():
                if role_name not in self._role_embeddings:
                    continue
                if any(kw in msg_lower for kw in keywords):
                    sim = _best_of(self._role_embeddings[role_name], 0.0) + _KEYWORD_BOOST_AMOUNT
                    if sim > boosted_sim:
                        boosted_sim = sim
                        boosted_role = role_name
            if boosted_role and boosted_sim >= self.threshold and boosted_role != best_role:
                logger.info(
                    f"SemanticRouter keyword boost: '{best_role}' ({best_sim:.3f}) → "
                    f"'{boosted_role}' ({boosted_sim:.3f})"
                )
                return boosted_role, None, boosted_sim

        if best_sim >= self.threshold:
            return best_role, best_sub_intent, best_sim
        return None, None, best_sim
