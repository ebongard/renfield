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
from utils.llm_client import get_embed_client


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
        self._ollama_client = None

    async def _get_ollama_client(self):
        """Lazy init of the embedding/extraction Ollama client."""
        if self._ollama_client is None:
            self._ollama_client = get_embed_client()
        return self._ollama_client

    async def _get_embedding(self, text_input: str) -> list[float]:
        """Generate embedding for query/entity text."""
        client = await self._get_ollama_client()
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

            client = await self._get_ollama_client()
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
        user_role: str | None = None,
        lang: str = "de",
    ) -> str | None:
        """
        Retrieve relevant graph triples for a query based on user's accessible scopes.

        Uses LLM entity extraction to convert natural-language queries into
        entity name searches, improving cosine similarity matching. Falls back
        to embedding the full query if no entity names are extracted.

        Returns formatted context string or None if nothing relevant.
        """
        from services.kg_scope_loader import get_scope_loader
        scope_loader = get_scope_loader()

        threshold = settings.kg_retrieval_threshold
        max_triples = settings.kg_max_context_triples

        # Build scope filter based on user's accessible scopes
        if user_id is not None:
            accessible_scopes = scope_loader.get_accessible_scopes(user_role, include_personal=False)

            if accessible_scopes:
                scopes_list = ','.join(f"'{s}'" for s in accessible_scopes)
                user_filter = f"""AND (
                    ((e.user_id = :user_id OR e.user_id IS NULL) AND e.scope = 'personal')
                    OR e.scope IN ({scopes_list})
                )"""
                base_params: dict[str, Any] = {"user_id": user_id}
            else:
                user_filter = "AND ((e.user_id = :user_id OR e.user_id IS NULL) AND e.scope = 'personal')"
                base_params = {"user_id": user_id}
        else:
            accessible_scopes = scope_loader.get_accessible_scopes(None, include_personal=False)
            if accessible_scopes:
                scopes_list = ','.join(f"'{s}'" for s in accessible_scopes)
                user_filter = f"AND e.scope IN ({scopes_list})"
                base_params = {}
            else:
                return None

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
            params = {**base_params, "embedding": embedding_str}

            sql = text(f"""
                SELECT e.id, e.name, e.entity_type,
                       1 - (e.embedding <=> CAST(:embedding AS vector)) as similarity
                FROM kg_entities e
                WHERE e.is_active = true
                  AND e.embedding IS NOT NULL
                  {user_filter}
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

        # Fetch relations involving those entities
        relations = await self.db.execute(
            select(KGRelation)
            .where(
                KGRelation.is_active == True,  # noqa: E712
                (KGRelation.subject_id.in_(relevant_ids)) | (KGRelation.object_id.in_(relevant_ids)),
            )
            .limit(max_triples)
        )
        relation_rows = relations.scalars().all()

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
