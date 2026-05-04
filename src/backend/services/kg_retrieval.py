"""
KG Retrieval — extracted from knowledge_graph_service.py for circles v1.

Holds the read-side retrieval methods for the Knowledge Graph: query-entity
extraction (LLM call) and similarity-based context fetching with scope filtering.
Write-side concerns (entity resolution, save_relation, extract_and_save,
extract_from_text, extract_from_chunks, CRUD operations, hooks) remain in
knowledge_graph_service.KnowledgeGraphService.

ASCII data flow:

    query (str) + user_id + user_role
       │
       ▼
    _extract_query_entities(query)  ←── LLM call (kg_extraction_model)
       │      returns: list[str] (entity names)  OR  []  on any failure
       │
       ▼
    For each name (or full query if no names extracted):
       │
       ▼
    _get_embedding(text)  ←── nomic-embed-text via Ollama
       │
       ▼
    SQL: SELECT entities WHERE embedding similar
                          AND (user_id matches OR scope is accessible)
       │
       ▼
    Filter by similarity threshold (settings.kg_retrieval_threshold)
       │
       ▼
    Collect matching entity_ids (deduplicated)
       │
       ▼
    SQL: SELECT relations WHERE subject_id IN ids OR object_id IN ids
                          LIMIT settings.kg_max_context_triples
       │
       ▼
    Format triples as "Subject predicate Object" lines + header
       │
       ▼
    Return formatted context string OR None

Lane A2 of the second-brain-circles eng-review plan. Mirrors Lane A1's
pattern (rag_retrieval.py): same dependency shape, same flag pattern,
same regression-test approach.

After v2.5 (KG retrieval upgrade — multi-hop, edge-type ranking, community
detection, structural query primitives), THIS module becomes the natural
home for all the new graph algorithms. Today's `get_relevant_context` is
flat-1-hop entity lookup; v2.5 generalizes it without touching consumers.
"""
from __future__ import annotations

import json
import re
from typing import Any

from loguru import logger
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import KGEntity, KGRelation
from utils.config import settings
from utils.llm_client import get_default_client, get_embed_client


class KGRetrieval:
    """
    Stateless-ish retrieval service for Knowledge Graph context.

    Same dependency shape as KnowledgeGraphService but scoped to read-side only:
      - db: AsyncSession (queries; never writes)
      - lazy ollama client (for query embeddings + entity-name extraction)

    Public surface (mirrors the methods extracted from KnowledgeGraphService):
      - get_relevant_context(query, user_id?, user_role?, lang?) -> str | None
      - _extract_query_entities(query, lang?) -> list[str]   (private helper)
      - _get_embedding(text) -> list[float]                   (private helper)
    """

    def __init__(self, db: AsyncSession):
        self.db = db
        self._embed_client = None
        self._chat_client = None

    async def _get_embed_client(self):
        """Embed-tier LLM client (Qwen3-Embedding via llama-server-embed)."""
        if self._embed_client is None:
            self._embed_client = get_embed_client()
        return self._embed_client

    async def _get_chat_client(self):
        """Chat-tier LLM client (Qwen3.6 via llama-server-agent) for entity-name extraction."""
        if self._chat_client is None:
            self._chat_client = get_default_client()
        return self._chat_client

    async def _get_embedding(self, text_input: str) -> list[float]:
        """Generate embedding for query/entity text."""
        client = await self._get_embed_client()
        response = await client.embeddings(
            model=settings.ollama_embed_model,
            prompt=text_input,
        )
        return response.embedding

    async def _extract_query_entities(self, query: str, lang: str = "de") -> list[str]:
        """
        Extract entity names from a natural-language query via LLM.

        Returns a list of proper names mentioned in the query, or an empty
        list on any failure (LLM error, parse error, no entities found).
        Used by get_relevant_context() to improve embedding search accuracy.
        """
        from services.prompt_manager import prompt_manager
        from utils.llm_client import extract_response_content, get_classification_chat_kwargs

        try:
            prompt = prompt_manager.get(
                "knowledge_graph", "query_entities_prompt", lang=lang,
                query=query,
            )
            system_msg = prompt_manager.get(
                "knowledge_graph", "query_entities_system", lang=lang,
            )
            llm_options = prompt_manager.get_config("knowledge_graph", "llm_options") or {}
            model = settings.kg_extraction_model or settings.ollama_model

            client = await self._get_chat_client()
            response = await client.chat(
                model=model,
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": prompt},
                ],
                options=llm_options,
                **get_classification_chat_kwargs(model),
            )
            raw_text = extract_response_content(response)
        except Exception as e:
            logger.debug(f"KG: Query entity extraction LLM call failed: {e}")
            return []

        if not raw_text:
            return []

        text_content = raw_text.strip()

        # Remove markdown code blocks
        if "```" in text_content:
            match = re.search(r'```(?:json)?\s*(\[.*?\])\s*```', text_content, re.DOTALL)
            if match:
                text_content = match.group(1)

        # Find JSON array
        first_bracket = text_content.find('[')
        last_bracket = text_content.rfind(']')
        if first_bracket >= 0 and last_bracket > first_bracket:
            text_content = text_content[first_bracket:last_bracket + 1]

        try:
            data = json.loads(text_content)
            if isinstance(data, list):
                return [str(item).strip() for item in data if isinstance(item, str) and item.strip()]
            return []
        except (json.JSONDecodeError, TypeError):
            logger.debug(f"KG: Could not parse entity list from: {raw_text[:200]}")
            return []

    async def get_relevant_context(
        self,
        query: str,
        user_id: int | None = None,
        user_role: str | None = None,  # kept for back-compat; ignored under circles
        lang: str = "de",
    ) -> str | None:
        """
        Retrieve relevant graph triples for a query, filtered by circle access.

        Uses LLM entity extraction to convert natural-language queries into
        entity name searches, improving cosine similarity matching. Falls back
        to embedding the full query if no entity names are extracted.

        Lane C rewrite: filter is now circle-tier-based (per-entity-owner) via
        EXISTS subqueries on circle_memberships + atom_explicit_grants. The
        legacy scope-based filter (and user_role parameter) are retired —
        scope was dropped from kg_entities by pc20260420_circles_v1_schema.

        For un-authenticated callers (user_id is None), only public-tier
        entities are reachable. AUTH_ENABLED=false single-user mode is handled
        upstream (the route layer doesn't call this with user_id=None for
        single-user instances; it passes the sole user's id).

        Returns formatted context string or None if nothing relevant.
        """
        from services.circle_sql import (
            kg_entities_circles_filter,
            kg_relations_circles_filter,
        )

        threshold = settings.kg_retrieval_threshold
        max_triples = settings.kg_max_context_triples

        # AUTH_ENABLED=false → full bypass (single-user mode sees everything).
        # Anonymous-but-auth-on (`user_id is None`) → public-tier only.
        # Authenticated → standard 4-branch OR via circle_sql helpers.
        if not settings.auth_enabled:
            entity_filter = ""
            entity_params: dict[str, Any] = {}
            relation_filter_clause = ""
            relation_params: dict[str, Any] = {}
        elif user_id is None:
            from models.database import TIER_PUBLIC
            entity_filter = "AND e.circle_tier = :pub_tier"
            entity_params = {"pub_tier": TIER_PUBLIC}
            relation_filter_clause = "AND r.circle_tier = :pub_tier"
            relation_params = {"pub_tier": TIER_PUBLIC}
        else:
            ent_clause, ent_params = kg_entities_circles_filter(user_id, alias="e")
            entity_filter = f"AND {ent_clause}"
            entity_params = ent_params
            rel_clause, rel_params = kg_relations_circles_filter(user_id, alias="r")
            relation_filter_clause = f"AND {rel_clause}"
            relation_params = rel_params

        # Extract entity names from query, fall back to full query
        extracted_names = await self._extract_query_entities(query, lang)
        search_texts = extracted_names if extracted_names else [query]

        if extracted_names:
            logger.debug(f"KG: Extracted entity names from query: {extracted_names}")

        # Search for each text, collecting matching entity IDs
        relevant_ids: list[int] = []
        seen_ids: set[int] = set()

        for search_text in search_texts:
            try:
                embedding = await self._get_embedding(search_text)
            except Exception as e:
                logger.warning(f"KG: Could not embed '{search_text}': {e}")
                continue

            if not embedding:
                continue

            embedding_str = f"[{','.join(map(str, embedding))}]"
            params = {**entity_params, "embedding": embedding_str}

            sql = text(f"""
                SELECT e.id, e.name, e.entity_type,
                       1 - (e.embedding <=> CAST(:embedding AS vector)) as similarity
                FROM kg_entities e
                WHERE e.is_active = true
                  AND e.embedding IS NOT NULL
                  {entity_filter}
                ORDER BY e.embedding <=> CAST(:embedding AS vector)
                LIMIT 10
            """)

            result = await self.db.execute(sql, params)
            rows = result.fetchall()

            for row in rows:
                sim = float(row.similarity) if row.similarity else 0
                if sim >= threshold and row.id not in seen_ids:
                    relevant_ids.append(row.id)
                    seen_ids.add(row.id)

        if not relevant_ids:
            return None

        # Fetch relations involving those entities, filtered by relation tier
        # (defends against the case where an entity is accessible but a relation
        # at a more-restrictive tier should still be hidden — kg_relations
        # carries its own circle_tier from MIN(subject.tier, object.tier)).
        rel_sql = text(f"""
            SELECT r.id, r.subject_id, r.predicate, r.object_id
            FROM kg_relations r
            WHERE r.is_active = true
              AND (r.subject_id = ANY(:rel_ids) OR r.object_id = ANY(:rel_ids))
              {relation_filter_clause}
            LIMIT :max_triples
        """)
        rel_params_full = {
            **relation_params,
            "rel_ids": relevant_ids,
            "max_triples": max_triples,
        }
        rel_result = await self.db.execute(rel_sql, rel_params_full)
        relation_rows_raw = rel_result.fetchall()

        # Convert to KGRelation-like objects for the rest of the function
        from types import SimpleNamespace
        relation_rows = [
            SimpleNamespace(
                id=r.id, subject_id=r.subject_id, predicate=r.predicate, object_id=r.object_id,
            )
            for r in relation_rows_raw
        ]

        if not relation_rows:
            return None

        # Fetch all entity names we need
        entity_ids = set()
        for r in relation_rows:
            entity_ids.add(r.subject_id)
            entity_ids.add(r.object_id)

        entities_result = await self.db.execute(
            select(KGEntity).where(KGEntity.id.in_(entity_ids))
        )
        entity_map = {e.id: e.name for e in entities_result.scalars().all()}

        # Format triples
        triples = []
        for r in relation_rows:
            subj = entity_map.get(r.subject_id, "?")
            obj = entity_map.get(r.object_id, "?")
            triples.append(f"- {subj} {r.predicate} {obj}")

        if not triples:
            return None

        if lang == "de":
            header = (
                "WISSENSGRAPH (persoenliche Fakten ueber den Benutzer und sein Umfeld):\n"
                "Die folgenden Fakten stammen aus Dokumenten und Gespraechen des Benutzers.\n"
                "Nutze NUR diese Fakten wenn die Frage sich auf genannte Personen/Orte/Organisationen bezieht.\n"
                "Erfinde KEINE zusaetzlichen Informationen ueber diese Entitaeten."
            )
        else:
            header = (
                "KNOWLEDGE GRAPH (personal facts about the user and their environment):\n"
                "The following facts come from the user's documents and conversations.\n"
                "Use ONLY these facts when the question refers to named people/places/organizations.\n"
                "Do NOT invent additional information about these entities."
            )
        return f"{header}\n" + "\n".join(triples)
