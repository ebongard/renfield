"""
Knowledge Graph Service — Entity-Relation triples from conversations.

Extracts named entities and their relationships from chat messages via LLM,
stores them with pgvector embeddings for semantic entity resolution, and
provides context retrieval for LLM prompt injection.

Pattern follows ConversationMemoryService for embedding generation and
cosine similarity search via raw SQL (pgvector).
"""
import json
import re
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import KG_ENTITY_TYPES, KG_SCOPE_PERSONAL, KGEntity, KGRelation
from utils.config import settings
from utils.llm_client import get_default_client

# =============================================================================
# Compiled regex patterns for entity validation (module-level for performance)
# =============================================================================

# Spaced-out characters: "F R E S E N", "0 8 . 0 6 . 2 0 2 2"
_RE_SPACED_CHARS = re.compile(r'^(?:\S\s){2,}\S$')

# URLs: www., http, .de/, .com, etc.
_RE_URL = re.compile(r'(?:https?://|www\.|\.(?:de|com|org|net|io|eu|at|ch)/)', re.IGNORECASE)

# Email addresses
_RE_EMAIL = re.compile(r'\S+@\S+\.\S+')

# Date patterns: 08.06.2022, 2022-06-08, 06/2022, etc.
_RE_DATE = re.compile(
    r'^(?:\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4}|\d{4}[./\-]\d{1,2}[./\-]\d{1,2}|\d{1,2}/\d{4})$'
)

# Phone patterns: +49 30 123456, 030/123456, (030) 123456
_RE_PHONE = re.compile(r'^\+?\d[\d\s/().\-]{6,}$')

# IBAN-like: DE + mostly digits
_RE_IBAN = re.compile(r'^[A-Z]{2}\d{2}[\s]?[\d\s]{10,}$')

# Pure reference codes: uppercase + digits, no spaces, 5+ chars (e.g. Y25588501619C, DE811127597)
_RE_REFCODE = re.compile(r'^[A-Z0-9]{5,}$')

# Numbered roles: "Bediener 2", "Sachbearbeiter 3"
_RE_NUMBERED_ROLE = re.compile(r'^.+\s+\d+$')

# Generic roles blocklist (German legal/business roles) — person type only
_GENERIC_ROLES = frozenset({
    "kunde", "kundin", "kunden", "auftraggeber", "auftraggeberin",
    "vermittler", "vermittlerin", "sachbearbeiter", "sachbearbeiterin",
    "berater", "beraterin", "betreuer", "betreuerin",
    "bediener", "bedienerin", "mitarbeiter", "mitarbeiterin",
    "geschäftsführer", "geschäftsführerin", "geschaeftsfuehrer",
    "vorstand", "vorsitzender", "vorsitzende",
    "vollziehungsbeamter", "vollziehungsbeamtin", "gerichtsvollzieher",
    "notar", "notarin", "richter", "richterin",
    "rechtsanwalt", "rechtsanwältin", "rechtsanwaeltin", "anwalt", "anwältin",
    "steuerberater", "steuerberaterin", "wirtschaftsprüfer",
    "bürgermeister", "bürgermeisterin", "der bürgermeister",
    "empfänger", "empfaenger", "absender", "antragsteller", "antragstellerin",
    "kläger", "klägerin", "klaeger", "beklagter", "beklagte",
    "schuldner", "schuldnerin", "gläubiger", "gläubigerin", "glaeubiger",
    "vermieter", "vermieterin", "mieter", "mieterin",
    "versicherungsnehmer", "versicherungsnehmerin", "versicherte", "versicherter",
    "patient", "patientin", "arzt", "ärztin",
    "unterzeichner", "unterzeichnerin", "bevollmächtigter", "bevollmächtigte",
})


class KnowledgeGraphService:
    """Manages knowledge graph entities and relations with pgvector."""

    def __init__(self, db: AsyncSession):
        self.db = db
        self._ollama_client = None

    async def _get_ollama_client(self):
        if self._ollama_client is None:
            self._ollama_client = get_default_client()
        return self._ollama_client

    async def _get_embedding(self, text_input: str) -> list[float]:
        """Generate embedding using Ollama."""
        client = await self._get_ollama_client()
        response = await client.embeddings(
            model=settings.ollama_embed_model,
            prompt=text_input,
        )
        return response.embedding

    # =========================================================================
    # Entity Validation (post-extraction filter)
    # =========================================================================

    @staticmethod
    def _is_valid_entity(name: str, entity_type: str) -> bool:
        """
        Fast regex-based validation to reject garbage entities from LLM extraction.

        Catches OCR artifacts, URLs, emails, IDs, reference codes, dates,
        phone numbers, IBANs, and generic roles (for person type).
        Called BEFORE resolve_entity() to avoid polluting the graph.
        """
        if not name:
            return False

        stripped = name.strip()

        # Length bounds
        if len(stripped) < 2 or len(stripped) > 120:
            return False

        # Spaced-out characters (OCR artifact): "F R E S E N"
        if _RE_SPACED_CHARS.match(stripped):
            return False

        # URLs
        if _RE_URL.search(stripped):
            return False

        # Email addresses
        if _RE_EMAIL.search(stripped):
            return False

        # Pure digits/symbols (no alpha chars at all)
        if not any(c.isalpha() for c in stripped):
            return False

        # Digit ratio > 50% (catches IDs, reference codes like DE811127597)
        alpha_count = sum(1 for c in stripped if c.isalpha())
        digit_count = sum(1 for c in stripped if c.isdigit())
        if digit_count > 0 and digit_count / (alpha_count + digit_count) > 0.5:
            return False

        # Date patterns
        if _RE_DATE.match(stripped):
            return False

        # Phone patterns
        if _RE_PHONE.match(stripped):
            return False

        # IBAN-like
        if _RE_IBAN.match(stripped):
            return False

        # Pure reference codes (uppercase + digits, no spaces, 5+ chars)
        if _RE_REFCODE.match(stripped):
            return False

        # Person-specific: generic roles and numbered roles
        if entity_type == "person":
            name_lower = stripped.lower()
            if name_lower in _GENERIC_ROLES:
                return False
            if _RE_NUMBERED_ROLE.match(stripped):
                # Check if the text before the number is a generic role
                base = stripped.rsplit(None, 1)[0].lower() if " " in stripped else ""
                if base in _GENERIC_ROLES:
                    return False

        return True

    # =========================================================================
    # Entity Resolution
    # =========================================================================

    async def resolve_entity(
        self,
        name: str,
        entity_type: str,
        user_id: int | None,
        user_role: str | None = None,
        description: str | None = None,
    ) -> KGEntity:
        """
        Resolve an entity by name, creating or merging as needed.

        Resolution order:
        1. Exact name match in personal entities (user_id + scope=personal)
        2. Exact name match in accessible custom scopes (based on user role)
        3. Embedding similarity in personal entities
        4. Embedding similarity in accessible custom scopes
        5. Create new personal entity
        """
        from services.kg_scope_loader import get_scope_loader
        scope_loader = get_scope_loader()

        # Step 1: Personal exact match
        query = select(KGEntity).where(
            func.lower(KGEntity.name) == name.lower(),
            KGEntity.is_active == True,  # noqa: E712
            KGEntity.user_id == user_id,
            KGEntity.scope == KG_SCOPE_PERSONAL,
        )
        result = await self.db.execute(query)
        existing = result.scalar_one_or_none()

        if existing:
            existing.mention_count = (existing.mention_count or 1) + 1
            existing.last_seen_at = datetime.now(UTC).replace(tzinfo=None)
            if description and not existing.description:
                existing.description = description
            await self.db.flush()
            return existing

        # Step 2: Custom scopes exact match (user's accessible scopes)
        accessible_scopes = scope_loader.get_accessible_scopes(user_role, include_personal=False)
        if accessible_scopes:
            query = select(KGEntity).where(
                func.lower(KGEntity.name) == name.lower(),
                KGEntity.is_active == True,  # noqa: E712
                KGEntity.scope.in_(accessible_scopes),
            )
            result = await self.db.execute(query)
            existing = result.scalar_one_or_none()

            if existing:
                # Update mention count but keep the original owner
                existing.mention_count = (existing.mention_count or 1) + 1
                existing.last_seen_at = datetime.now(UTC).replace(tzinfo=None)
                if description and not existing.description:
                    existing.description = description
                await self.db.flush()
                return existing

        # Step 3 & 4: Embedding similarity check
        embedding = None
        try:
            embedding = await self._get_embedding(name)
        except Exception as e:
            logger.warning(f"KG: Could not generate embedding for entity '{name}': {e}")

        if embedding:
            # Check personal entities first
            similar = await self._find_similar_entity(
                embedding, user_id=user_id, accessible_scopes=None
            )
            if similar:
                similar.mention_count = (similar.mention_count or 1) + 1
                similar.last_seen_at = datetime.now(UTC).replace(tzinfo=None)
                if description and not similar.description:
                    similar.description = description
                await self.db.flush()
                return similar

            # Check accessible custom scopes
            if accessible_scopes:
                similar = await self._find_similar_entity(
                    embedding, user_id=None, accessible_scopes=accessible_scopes
                )
                if similar:
                    similar.mention_count = (similar.mention_count or 1) + 1
                    similar.last_seen_at = datetime.now(UTC).replace(tzinfo=None)
                    if description and not similar.description:
                        similar.description = description
                    await self.db.flush()
                    return similar

        # Step 5: Check personal entity limit (only personal entities count)
        if user_id is not None:
            count_result = await self.db.execute(
                select(func.count(KGEntity.id)).where(
                    KGEntity.user_id == user_id,
                    KGEntity.is_active == True,  # noqa: E712
                    KGEntity.scope == KG_SCOPE_PERSONAL,
                )
            )
            count = count_result.scalar() or 0
            if count >= settings.kg_max_entities_per_user:
                logger.warning(f"KG: Personal entity limit reached for user {user_id}")
                # Return a best-effort match or skip
                return await self._get_oldest_entity(user_id)

        # Create new personal entity
        entity = KGEntity(
            user_id=user_id,
            name=name,
            entity_type=entity_type if entity_type in KG_ENTITY_TYPES else "thing",
            description=description,
            embedding=embedding,
            scope=KG_SCOPE_PERSONAL,  # Personal by default
        )
        self.db.add(entity)
        await self.db.flush()
        logger.debug(f"KG: New entity '{name}' ({entity_type}) id={entity.id} scope=personal")
        return entity

    async def _find_similar_entity(
        self,
        embedding: list[float],
        user_id: int | None,
        accessible_scopes: list[str] | None = None,
    ) -> KGEntity | None:
        """
        Find an existing entity above the similarity threshold.

        Args:
            embedding: Entity embedding vector
            user_id: User ID for personal scope filtering (None = no personal filtering)
            accessible_scopes: List of custom scope names accessible to the user (None = skip)
        """
        threshold = settings.kg_similarity_threshold
        embedding_str = f"[{','.join(map(str, embedding))}]"

        if accessible_scopes:
            # Search in accessible custom scopes only
            scopes_str = ','.join(f"'{s}'" for s in accessible_scopes)
            user_filter = f"AND scope IN ({scopes_str})"
            params: dict = {"embedding": embedding_str}
        elif user_id is not None:
            # Search in personal (user-owned) only
            user_filter = "AND (user_id = :user_id AND scope = 'personal')"
            params = {"embedding": embedding_str, "user_id": user_id}
        else:
            # No filtering (shouldn't happen in normal flow)
            user_filter = ""
            params = {"embedding": embedding_str}

        sql = text(f"""
            SELECT id,
                   1 - (embedding <=> CAST(:embedding AS vector)) as similarity
            FROM kg_entities
            WHERE is_active = true
              AND embedding IS NOT NULL
              {user_filter}
            ORDER BY embedding <=> CAST(:embedding AS vector)
            LIMIT 1
        """)

        result = await self.db.execute(sql, params)
        row = result.fetchone()

        if row and float(row.similarity) >= threshold:
            entity_result = await self.db.execute(
                select(KGEntity).where(KGEntity.id == row.id)
            )
            return entity_result.scalar_one_or_none()

        return None

    async def _get_oldest_entity(self, user_id: int) -> KGEntity | None:
        """Get the oldest personal entity for a user (fallback when limit reached)."""
        result = await self.db.execute(
            select(KGEntity)
            .where(
                KGEntity.user_id == user_id,
                KGEntity.is_active == True,  # noqa: E712
                KGEntity.scope == KG_SCOPE_PERSONAL,
            )
            .order_by(KGEntity.first_seen_at.asc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    # =========================================================================
    # Relations
    # =========================================================================

    async def save_relation(
        self,
        subject_id: int,
        predicate: str,
        object_id: int,
        user_id: int | None = None,
        confidence: float = 0.8,
        source_session_id: str | None = None,
    ) -> KGRelation:
        """Save a relation, deduplicating same subject+predicate+object."""
        # Check for existing relation
        query = select(KGRelation).where(
            KGRelation.subject_id == subject_id,
            KGRelation.predicate == predicate,
            KGRelation.object_id == object_id,
            KGRelation.is_active == True,  # noqa: E712
        )
        result = await self.db.execute(query)
        existing = result.scalar_one_or_none()

        if existing:
            # Update confidence (take the max)
            existing.confidence = max(existing.confidence or 0, confidence)
            await self.db.flush()
            return existing

        relation = KGRelation(
            user_id=user_id,
            subject_id=subject_id,
            predicate=predicate,
            object_id=object_id,
            confidence=confidence,
            source_session_id=source_session_id,
        )
        self.db.add(relation)
        await self.db.flush()
        logger.debug(f"KG: New relation {subject_id} --{predicate}--> {object_id}")
        return relation

    # =========================================================================
    # Extract from Conversation
    # =========================================================================

    async def extract_and_save(
        self,
        user_message: str,
        assistant_response: str,
        user_id: int | None = None,
        session_id: str | None = None,
        lang: str = "de",
    ) -> tuple[list[KGEntity], list[KGRelation]]:
        """Extract entities and relations from a conversation exchange."""
        from models.database import User
        from services.prompt_manager import prompt_manager

        # Get user's role name if authenticated
        user_role = None
        if user_id is not None:
            result = await self.db.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            if user and user.role:
                user_role = user.role.name

        prompt = prompt_manager.get(
            "knowledge_graph", "extraction_prompt", lang=lang,
            user_message=user_message,
            assistant_response=assistant_response,
        )
        system_msg = prompt_manager.get(
            "knowledge_graph", "extraction_system", lang=lang,
        )
        llm_options = prompt_manager.get_config("knowledge_graph", "llm_options") or {}

        model = settings.kg_extraction_model or settings.ollama_model

        try:
            from utils.llm_client import extract_response_content, get_classification_chat_kwargs

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
            logger.warning(f"KG extraction LLM call failed: {e}")
            return [], []

        extracted = self._parse_extraction_response(raw_text)
        if not extracted:
            return [], []

        entities_data = extracted.get("entities", [])
        relations_data = extracted.get("relations", [])

        # Resolve entities (with validation filter)
        entity_map: dict[str, KGEntity] = {}  # name -> entity
        saved_entities = []
        rejected_count = 0
        for ent in entities_data:
            name = ent.get("name", "").strip()
            etype = ent.get("type", "thing").strip().lower()
            desc = ent.get("description", "").strip() or None
            if not name:
                continue

            if not self._is_valid_entity(name, etype):
                logger.debug(f"KG: Rejected invalid entity: '{name}' ({etype})")
                rejected_count += 1
                continue

            entity = await self.resolve_entity(name, etype, user_id, user_role, desc)
            entity_map[name.lower()] = entity
            saved_entities.append(entity)

        if rejected_count:
            logger.info(f"KG: Filtered out {rejected_count} invalid entities from conversation")

        # Save relations
        saved_relations = []
        for rel in relations_data:
            subj_name = rel.get("subject", "").strip().lower()
            pred = rel.get("predicate", "").strip()
            obj_name = rel.get("object", "").strip().lower()
            conf = rel.get("confidence", 0.8)

            if not subj_name or not pred or not obj_name:
                continue

            subject = entity_map.get(subj_name)
            obj = entity_map.get(obj_name)

            if not subject or not obj:
                continue

            try:
                conf = max(0.1, min(1.0, float(conf)))
            except (TypeError, ValueError):
                conf = 0.8

            relation = await self.save_relation(
                subject_id=subject.id,
                predicate=pred,
                object_id=obj.id,
                user_id=user_id,
                confidence=conf,
                source_session_id=session_id,
            )
            saved_relations.append(relation)

        await self.db.commit()

        if saved_entities or saved_relations:
            logger.info(
                f"KG: Extracted {len(saved_entities)} entities, "
                f"{len(saved_relations)} relations (user_id={user_id})"
            )

        return saved_entities, saved_relations

    async def extract_from_text(
        self,
        text: str,
        user_id: int | None = None,
        source_ref: str | None = None,
        lang: str = "de",
    ) -> tuple[list[KGEntity], list[KGRelation]]:
        """Extract entities and relations from a free-text passage (e.g. document chunk)."""
        from models.database import User
        from services.prompt_manager import prompt_manager

        # Get user's role name if authenticated
        user_role = None
        if user_id is not None:
            result = await self.db.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            if user and user.role:
                user_role = user.role.name

        prompt = prompt_manager.get(
            "knowledge_graph", "document_extraction_prompt", lang=lang,
            text=text,
        )
        system_msg = prompt_manager.get(
            "knowledge_graph", "extraction_system", lang=lang,
        )
        llm_options = prompt_manager.get_config("knowledge_graph", "llm_options") or {}

        model = settings.kg_extraction_model or settings.ollama_model

        try:
            from utils.llm_client import extract_response_content, get_classification_chat_kwargs

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
            logger.warning(f"KG document extraction LLM call failed: {e}")
            return [], []

        extracted = self._parse_extraction_response(raw_text)
        if not extracted:
            return [], []

        entities_data = extracted.get("entities", [])
        relations_data = extracted.get("relations", [])

        # Resolve entities (with validation filter)
        entity_map: dict[str, KGEntity] = {}
        saved_entities = []
        rejected_count = 0
        for ent in entities_data:
            name = ent.get("name", "").strip()
            etype = ent.get("type", "thing").strip().lower()
            desc = ent.get("description", "").strip() or None
            if not name:
                continue

            if not self._is_valid_entity(name, etype):
                logger.debug(f"KG: Rejected invalid entity: '{name}' ({etype})")
                rejected_count += 1
                continue

            entity = await self.resolve_entity(name, etype, user_id, user_role, desc)
            entity_map[name.lower()] = entity
            saved_entities.append(entity)

        if rejected_count:
            logger.info(f"KG: Filtered out {rejected_count} invalid entities from document")

        # Save relations
        saved_relations = []
        for rel in relations_data:
            subj_name = rel.get("subject", "").strip().lower()
            pred = rel.get("predicate", "").strip()
            obj_name = rel.get("object", "").strip().lower()
            conf = rel.get("confidence", 0.8)

            if not subj_name or not pred or not obj_name:
                continue

            subject = entity_map.get(subj_name)
            obj = entity_map.get(obj_name)

            if not subject or not obj:
                continue

            try:
                conf = max(0.1, min(1.0, float(conf)))
            except (TypeError, ValueError):
                conf = 0.8

            relation = await self.save_relation(
                subject_id=subject.id,
                predicate=pred,
                object_id=obj.id,
                user_id=user_id,
                confidence=conf,
                source_session_id=source_ref,
            )
            saved_relations.append(relation)

        await self.db.commit()

        if saved_entities or saved_relations:
            logger.info(
                f"KG: Extracted {len(saved_entities)} entities, "
                f"{len(saved_relations)} relations from text "
                f"(user_id={user_id}, source={source_ref})"
            )

        return saved_entities, saved_relations

    async def extract_from_chunks(
        self,
        chunks: list[str],
        user_id: int | None = None,
        source_ref: str | None = None,
        lang: str = "de",
    ) -> tuple[list[KGEntity], list[KGRelation]]:
        """Extract entities and relations from multiple text chunks sequentially."""
        all_entities: list[KGEntity] = []
        all_relations: list[KGRelation] = []

        for i, chunk_text in enumerate(chunks):
            if not chunk_text or not chunk_text.strip():
                continue
            try:
                entities, relations = await self.extract_from_text(
                    chunk_text, user_id=user_id, source_ref=source_ref, lang=lang,
                )
                all_entities.extend(entities)
                all_relations.extend(relations)
            except Exception as e:
                logger.warning(f"KG: Chunk {i} extraction failed: {e}")

        if all_entities or all_relations:
            logger.info(
                f"KG: Extracted {len(all_entities)} entities, "
                f"{len(all_relations)} relations from {len(chunks)} chunks "
                f"(source={source_ref})"
            )

        return all_entities, all_relations

    @staticmethod
    def _parse_extraction_response(raw_text: str) -> dict | None:
        """Parse JSON object from LLM extraction response."""
        if not raw_text:
            return None

        text_content = raw_text.strip()

        # Remove markdown code blocks
        if "```" in text_content:
            match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text_content, re.DOTALL)
            if match:
                text_content = match.group(1)

        # Find JSON object
        first_brace = text_content.find('{')
        last_brace = text_content.rfind('}')
        if first_brace >= 0 and last_brace > first_brace:
            text_content = text_content[first_brace:last_brace + 1]

        try:
            data = json.loads(text_content)
            if isinstance(data, dict):
                return data
            return None
        except (json.JSONDecodeError, TypeError):
            logger.debug(f"KG extraction: could not parse JSON from: {raw_text[:200]}")
            return None

    # =========================================================================
    # Retrieve Context
    # =========================================================================

    async def get_relevant_context(
        self,
        query: str,
        user_id: int | None = None,
        user_role: str | None = None,
        lang: str = "de",
    ) -> str | None:
        """
        Retrieve relevant graph triples for a query based on user's accessible scopes.

        Returns formatted context string or None if nothing relevant.
        """
        from services.kg_scope_loader import get_scope_loader
        scope_loader = get_scope_loader()

        try:
            query_embedding = await self._get_embedding(query)
        except Exception as e:
            logger.warning(f"KG: Could not embed query: {e}")
            return None

        if not query_embedding:
            return None

        embedding_str = f"[{','.join(map(str, query_embedding))}]"
        threshold = settings.kg_retrieval_threshold
        max_triples = settings.kg_max_context_triples

        # Build scope filter based on user's accessible scopes
        if user_id is not None:
            accessible_scopes = scope_loader.get_accessible_scopes(user_role, include_personal=False)

            if accessible_scopes:
                # User sees: personal + accessible custom scopes
                scopes_list = ','.join(f"'{s}'" for s in accessible_scopes)
                user_filter = f"""AND (
                    (e.user_id = :user_id AND e.scope = 'personal')
                    OR e.scope IN ({scopes_list})
                )"""
                params: dict = {"embedding": embedding_str, "user_id": user_id}
            else:
                # User sees: personal only
                user_filter = "AND (e.user_id = :user_id AND e.scope = 'personal')"
                params = {"embedding": embedding_str, "user_id": user_id}
        else:
            # No auth: only public scope if defined
            accessible_scopes = scope_loader.get_accessible_scopes(None, include_personal=False)
            if accessible_scopes:
                scopes_list = ','.join(f"'{s}'" for s in accessible_scopes)
                user_filter = f"AND e.scope IN ({scopes_list})"
                params = {"embedding": embedding_str}
            else:
                # No accessible scopes for unauthenticated users
                return None

        # Find top-N similar entities
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

        # Filter by threshold
        relevant_ids = []
        for row in rows:
            sim = float(row.similarity) if row.similarity else 0
            if sim >= threshold:
                relevant_ids.append(row.id)

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

        header = "## Wissensgraph" if lang == "de" else "## Knowledge Graph"
        return f"{header}\n" + "\n".join(triples)

    # =========================================================================
    # CRUD for API
    # =========================================================================

    async def list_entities(
        self,
        user_id: int | None = None,
        entity_type: str | None = None,
        search: str | None = None,
        scope: str | None = None,
        page: int = 1,
        size: int = 50,
    ) -> tuple[list[KGEntity], int]:
        """List active entities with filters."""
        from services.kg_scope_loader import get_scope_loader
        scope_loader = get_scope_loader()

        query = select(KGEntity).where(KGEntity.is_active == True)  # noqa: E712
        count_query = select(func.count(KGEntity.id)).where(KGEntity.is_active == True)  # noqa: E712

        if user_id is not None:
            query = query.where(KGEntity.user_id == user_id)
            count_query = count_query.where(KGEntity.user_id == user_id)
        if entity_type:
            query = query.where(KGEntity.entity_type == entity_type)
            count_query = count_query.where(KGEntity.entity_type == entity_type)
        if search:
            like_pattern = f"%{search}%"
            query = query.where(KGEntity.name.ilike(like_pattern))
            count_query = count_query.where(KGEntity.name.ilike(like_pattern))
        if scope is not None and scope_loader.is_valid_scope(scope):
            query = query.where(KGEntity.scope == scope)
            count_query = count_query.where(KGEntity.scope == scope)

        total_result = await self.db.execute(count_query)
        total = total_result.scalar() or 0

        offset = (page - 1) * size
        query = query.order_by(KGEntity.last_seen_at.desc()).offset(offset).limit(size)
        result = await self.db.execute(query)
        entities = list(result.scalars().all())

        return entities, total

    async def get_entity(self, entity_id: int) -> KGEntity | None:
        result = await self.db.execute(
            select(KGEntity).where(
                KGEntity.id == entity_id,
                KGEntity.is_active == True,  # noqa: E712
            )
        )
        return result.scalar_one_or_none()

    async def update_entity(
        self,
        entity_id: int,
        name: str | None = None,
        entity_type: str | None = None,
        description: str | None = None,
    ) -> KGEntity | None:
        entity = await self.get_entity(entity_id)
        if not entity:
            return None

        if name is not None:
            entity.name = name
            # Re-embed with new name
            try:
                entity.embedding = await self._get_embedding(name)
            except Exception:
                pass
        if entity_type is not None and entity_type in KG_ENTITY_TYPES:
            entity.entity_type = entity_type
        if description is not None:
            entity.description = description

        await self.db.commit()
        await self.db.refresh(entity)
        return entity

    async def update_entity_scope(
        self,
        entity_id: int,
        scope: str,
    ) -> KGEntity | None:
        """Update scope of an entity (admin only)."""
        from services.kg_scope_loader import get_scope_loader
        scope_loader = get_scope_loader()

        if not scope_loader.is_valid_scope(scope):
            raise ValueError(f"Invalid scope: {scope}")

        entity = await self.get_entity(entity_id)
        if not entity:
            return None

        entity.scope = scope
        await self.db.commit()
        await self.db.refresh(entity)
        return entity

    async def delete_entity(self, entity_id: int) -> bool:
        """Soft-delete an entity and its relations."""
        entity = await self.get_entity(entity_id)
        if not entity:
            return False

        entity.is_active = False

        # Deactivate related relations
        await self.db.execute(
            update(KGRelation)
            .where(
                (KGRelation.subject_id == entity_id) | (KGRelation.object_id == entity_id)
            )
            .values(is_active=False)
        )

        await self.db.commit()
        return True

    async def merge_entities(
        self,
        source_id: int,
        target_id: int,
    ) -> KGEntity | None:
        """Merge source entity into target. Moves relations, deactivates source."""
        source = await self.get_entity(source_id)
        target = await self.get_entity(target_id)
        if not source or not target:
            return None

        # Move source's relations to target
        await self.db.execute(
            update(KGRelation)
            .where(KGRelation.subject_id == source_id, KGRelation.is_active == True)  # noqa: E712
            .values(subject_id=target_id)
        )
        await self.db.execute(
            update(KGRelation)
            .where(KGRelation.object_id == source_id, KGRelation.is_active == True)  # noqa: E712
            .values(object_id=target_id)
        )

        # Accumulate mention count
        target.mention_count = (target.mention_count or 1) + (source.mention_count or 1)
        if source.description and not target.description:
            target.description = source.description

        # Deactivate source
        source.is_active = False

        await self.db.commit()
        await self.db.refresh(target)
        return target

    async def list_relations(
        self,
        user_id: int | None = None,
        entity_id: int | None = None,
        page: int = 1,
        size: int = 50,
    ) -> tuple[list[dict], int]:
        """List active relations with entity data."""
        query = (
            select(KGRelation)
            .where(KGRelation.is_active == True)  # noqa: E712
        )
        count_query = select(func.count(KGRelation.id)).where(KGRelation.is_active == True)  # noqa: E712

        if user_id is not None:
            query = query.where(KGRelation.user_id == user_id)
            count_query = count_query.where(KGRelation.user_id == user_id)
        if entity_id is not None:
            query = query.where(
                (KGRelation.subject_id == entity_id) | (KGRelation.object_id == entity_id)
            )
            count_query = count_query.where(
                (KGRelation.subject_id == entity_id) | (KGRelation.object_id == entity_id)
            )

        total_result = await self.db.execute(count_query)
        total = total_result.scalar() or 0

        offset = (page - 1) * size
        query = query.order_by(KGRelation.created_at.desc()).offset(offset).limit(size)
        result = await self.db.execute(query)
        relations = list(result.scalars().all())

        # Fetch entity names
        entity_ids = set()
        for r in relations:
            entity_ids.add(r.subject_id)
            entity_ids.add(r.object_id)

        entity_map = {}
        if entity_ids:
            entities_result = await self.db.execute(
                select(KGEntity).where(KGEntity.id.in_(entity_ids))
            )
            entity_map = {e.id: e for e in entities_result.scalars().all()}

        relation_dicts = []
        for r in relations:
            subj = entity_map.get(r.subject_id)
            obj = entity_map.get(r.object_id)
            relation_dicts.append({
                "id": r.id,
                "subject": {
                    "id": subj.id, "name": subj.name, "entity_type": subj.entity_type,
                } if subj else None,
                "predicate": r.predicate,
                "object": {
                    "id": obj.id, "name": obj.name, "entity_type": obj.entity_type,
                } if obj else None,
                "confidence": r.confidence,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            })

        return relation_dicts, total

    async def update_relation(
        self,
        relation_id: int,
        predicate: str | None = None,
        confidence: float | None = None,
        subject_id: int | None = None,
        object_id: int | None = None,
    ) -> KGRelation | None:
        """Update an existing relation's predicate, confidence, or endpoints."""
        result = await self.db.execute(
            select(KGRelation).where(
                KGRelation.id == relation_id,
                KGRelation.is_active == True,  # noqa: E712
            )
        )
        relation = result.scalar_one_or_none()
        if not relation:
            return None

        new_subject = subject_id if subject_id is not None else relation.subject_id
        new_object = object_id if object_id is not None else relation.object_id

        if new_subject == new_object:
            raise ValueError("Subject and object must be different entities")

        # Validate that referenced entities exist
        for eid in (new_subject, new_object):
            if eid != relation.subject_id and eid != relation.object_id:
                entity = await self.get_entity(eid)
                if not entity:
                    raise ValueError(f"Entity {eid} not found")

        if predicate is not None:
            relation.predicate = predicate
        if confidence is not None:
            relation.confidence = confidence
        if subject_id is not None:
            relation.subject_id = subject_id
        if object_id is not None:
            relation.object_id = object_id

        await self.db.commit()
        await self.db.refresh(relation)
        return relation

    async def delete_relation(self, relation_id: int) -> bool:
        result = await self.db.execute(
            select(KGRelation).where(
                KGRelation.id == relation_id,
                KGRelation.is_active == True,  # noqa: E712
            )
        )
        relation = result.scalar_one_or_none()
        if not relation:
            return False
        relation.is_active = False
        await self.db.commit()
        return True

    async def get_stats(self, user_id: int | None = None) -> dict:
        """Get knowledge graph statistics."""
        base_entity = select(func.count(KGEntity.id)).where(KGEntity.is_active == True)  # noqa: E712
        base_relation = select(func.count(KGRelation.id)).where(KGRelation.is_active == True)  # noqa: E712

        if user_id is not None:
            base_entity = base_entity.where(KGEntity.user_id == user_id)
            base_relation = base_relation.where(KGRelation.user_id == user_id)

        entity_count = (await self.db.execute(base_entity)).scalar() or 0
        relation_count = (await self.db.execute(base_relation)).scalar() or 0

        # Entity type distribution
        type_query = (
            select(KGEntity.entity_type, func.count(KGEntity.id))
            .where(KGEntity.is_active == True)  # noqa: E712
            .group_by(KGEntity.entity_type)
        )
        if user_id is not None:
            type_query = type_query.where(KGEntity.user_id == user_id)

        type_result = await self.db.execute(type_query)
        entity_types = {row[0]: row[1] for row in type_result.fetchall()}

        return {
            "entity_count": entity_count,
            "relation_count": relation_count,
            "entity_types": entity_types,
        }


# =============================================================================
# Hook Functions (module-level, registered in lifecycle.py)
# =============================================================================

async def kg_post_message_hook(
    user_msg: str,
    assistant_msg: str,
    user_id: int | None = None,
    session_id: str | None = None,
    **kwargs,
):
    """Extract entities and relations from conversation (post_message hook)."""
    try:
        from services.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            svc = KnowledgeGraphService(db)
            lang = kwargs.get("lang", settings.default_language)
            await svc.extract_and_save(user_msg, assistant_msg, user_id, session_id, lang)
    except Exception as e:
        logger.warning(f"KG post_message hook failed: {e}")


async def kg_retrieve_context_hook(
    query: str,
    user_id: int | None = None,
    lang: str = "de",
    **kwargs,
) -> str | None:
    """Retrieve relevant graph context for LLM prompt (retrieve_context hook)."""
    try:
        from models.database import User
        from services.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            # Get user's role name if authenticated
            user_role = None
            if user_id is not None:
                result = await db.execute(select(User).where(User.id == user_id))
                user = result.scalar_one_or_none()
                if user and user.role:
                    user_role = user.role.name

            svc = KnowledgeGraphService(db)
            return await svc.get_relevant_context(query, user_id, user_role, lang)
    except Exception as e:
        logger.warning(f"KG retrieve_context hook failed: {e}")
        return None


async def kg_post_document_ingest_hook(
    chunks: list[str],
    document_id: int | None = None,
    user_id: int | None = None,
    **kwargs,
):
    """Extract KG entities from ingested document chunks (post_document_ingest hook)."""
    try:
        from services.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            svc = KnowledgeGraphService(db)
            source_ref = f"doc:{document_id}" if document_id else None
            lang = kwargs.get("lang", settings.default_language)
            await svc.extract_from_chunks(
                chunks, user_id=user_id, source_ref=source_ref, lang=lang,
            )
    except Exception as e:
        logger.warning(f"KG post_document_ingest hook failed: {e}")
