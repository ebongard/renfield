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
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import KG_ENTITY_TYPES, TIER_PUBLIC, KGEntity, KGRelation
from utils.config import settings
from utils.llm_client import get_embed_client

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

# German month dates: "März 2013", "01. Januar 2019", "Dezember"
_RE_GERMAN_MONTH = re.compile(
    r'^(\d{1,2}\.?\s*)?'
    r'(januar|februar|m[aä]rz|april|mai|juni|juli|august|september|oktober|november|dezember)'
    r'(\s+\d{2,4})?$',
    re.IGNORECASE,
)

# Currency symbols or codes: "100 EUR", "0,04 € /Minute"
_RE_CURRENCY = re.compile(r'\b(EUR|USD|CHF|GBP)\b|[€$£]')

# Field separators (asterisk/pipe/backslash delimited codes): "DUEL*MS*OS*BUEN/HA*BI"
_RE_FIELD_SEPARATOR = re.compile(r'[*|\\]')

# Nr. labels: "Vertragsnr. J269385", "Kunden Nr 12345"
_RE_NR_LABEL = re.compile(r'nr\.?\s', re.IGNORECASE)

# German field label suffixes — generic document field names (non-person only)
_FIELD_LABEL_SUFFIXES = (
    "nummer", "nummern",
    "bedingungen", "bestimmungen",
    "unterlagen", "dokumente", "nachweise",
    "angaben", "hinweise",
    "gebühren", "gebuehren", "entgelte",
    "zeitraum", "fristen",
    "bescheid", "bescheinigung",
    "erklärung", "erklaerung",
    "anschrift",
)

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
        # Cached fallback owner id — resolved lazily via _resolve_owner_user_id
        # when a writer doesn't carry an authenticated user (auth disabled, or
        # background jobs extracted from anonymous context). Matches the
        # migration's back-fill pattern: "first user by id" (see
        # pc20260420_circles_v1_schema.py:344).
        self._fallback_owner_id: int | None = None

    async def _resolve_owner_user_id(self, user_id: int | None) -> int | None:
        """Resolve a non-null owner for atom rows, or None if unavailable.

        Falls back to the first user's id when ``user_id`` is None — matches
        the migration's back-fill pattern (pc20260420_circles_v1_schema.py:344).
        Returns None only in dev setups (empty users table) where atom
        registration is skipped and the source row is written with
        ``atom_id=None``. Production always has the admin user from
        bootstrap, so this is never None in real deploys.
        """
        if user_id is not None:
            return user_id
        if self._fallback_owner_id is not None:
            return self._fallback_owner_id
        from models.database import User
        result = await self.db.execute(
            select(User.id).order_by(User.id.asc()).limit(1)
        )
        fallback = result.scalar()
        if fallback is None:
            return None
        self._fallback_owner_id = int(fallback)
        return self._fallback_owner_id

    async def _create_atom_for_new_source(
        self,
        atom_type: str,
        owner_user_id: int,
        tier: int,
    ) -> str:
        """Pre-create an ``atoms`` row before inserting the source row.

        The source-table ``atom_id`` columns carry a NOT NULL constraint and
        a non-deferrable FK back to ``atoms.atom_id`` (per the
        pc20260420_circles_v1 migration). That means the atoms row must
        already exist when the source-row INSERT fires. The source row's
        primary key is auto-incremented and only known after flush, so we
        seed ``atoms.source_id`` with a unique placeholder; the caller
        invokes :meth:`_finalize_atom_source_id` after flushing the source
        row to overwrite the placeholder with the real PK.
        """
        from datetime import UTC, datetime
        import uuid as _uuid
        from models.database import Atom as AtomORM
        atom_id = str(_uuid.uuid4())
        source_table = {
            "kg_node": "kg_entities",
            "kg_edge": "kg_relations",
        }[atom_type]
        placeholder = f"__pending__{atom_id}"
        now = datetime.now(UTC).replace(tzinfo=None)
        atom_row = AtomORM(
            atom_id=atom_id,
            atom_type=atom_type,
            source_table=source_table,
            source_id=placeholder,
            owner_user_id=int(owner_user_id),
            policy={"tier": int(tier)},
            created_at=now,
            updated_at=now,
        )
        self.db.add(atom_row)
        await self.db.flush()
        return atom_id

    async def _finalize_atom_source_id(self, atom_id: str, source_id: int) -> None:
        """Replace the placeholder ``source_id`` on an atoms row with the
        freshly-flushed source-row's primary key."""
        from models.database import Atom as AtomORM
        atom = (await self.db.execute(
            select(AtomORM).where(AtomORM.atom_id == atom_id)
        )).scalar_one()
        atom.source_id = str(source_id)
        await self.db.flush()

    async def _get_ollama_client(self):
        if self._ollama_client is None:
            self._ollama_client = get_embed_client()
        return self._ollama_client

    async def _get_embedding(self, text_input: str) -> list[float]:
        """Generate embedding using Ollama."""
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

        # Parse JSON array from response
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
                # Filter to non-empty strings only
                return [str(item).strip() for item in data if isinstance(item, str) and item.strip()]
            return []
        except (json.JSONDecodeError, TypeError):
            logger.debug(f"KG: Could not parse entity list from: {raw_text[:200]}")
            return []

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

        # German month dates: "März 2013", "01. Januar 2019", "Dezember"
        if _RE_GERMAN_MONTH.match(stripped):
            return False

        # Currency symbols or codes
        if _RE_CURRENCY.search(stripped):
            return False

        # Field separator codes (asterisk/pipe/backslash)
        if _RE_FIELD_SEPARATOR.search(stripped):
            return False

        # Nr. labels: "Vertragsnr. J269385"
        if _RE_NR_LABEL.search(stripped):
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
        else:
            # Non-person: reject German field label suffixes
            name_lower = stripped.lower()
            if any(name_lower.endswith(suffix) for suffix in _FIELD_LABEL_SUFFIXES):
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
        user_role: str | None = None,  # kept for back-compat; ignored under circles
        description: str | None = None,
    ) -> KGEntity:
        """
        Resolve an entity by name, creating or merging as needed.

        Lane C rewrite: scope-based steps 2 and 4 (accessible custom scopes
        from kg_scope_loader) are removed because the scope column was
        DROPPED by pc20260420_circles_v1_schema. New resolution order:

        1. Exact name match in user's own entities (user_id + unowned)
        2. Embedding similarity in user's own entities
        3. Create new entity owned by this user (circle_tier defaults to
           the user's default_capture_policy.tier; AtomService.upsert_atom
           creates the corresponding atoms row)

        Cross-user entity dedup (the old "accessible scopes" behavior) is
        deferred to v2 — household-shared knowledge graph entities will
        come back via the named-circles work then. For v1 dogfooding,
        per-user entity isolation is acceptable + simpler.
        """
        # Step 1: Exact name match in user's own entities (include unowned)
        query = select(KGEntity).where(
            func.lower(KGEntity.name) == name.lower(),
            KGEntity.is_active == True,  # noqa: E712
            or_(KGEntity.user_id == user_id, KGEntity.user_id.is_(None)),
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

        # Step 2: Embedding similarity check (user's own entities only)
        embedding = None
        try:
            embedding = await self._get_embedding(name)
        except Exception as e:
            logger.warning(f"KG: Could not generate embedding for entity '{name}': {e}")

        if embedding:
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

        # Step 3: Per-user entity limit check (no scope filter; just count user's entities)
        if user_id is not None:
            count_result = await self.db.execute(
                select(func.count(KGEntity.id)).where(
                    KGEntity.user_id == user_id,
                    KGEntity.is_active == True,  # noqa: E712
                )
            )
            count = count_result.scalar() or 0
            if count >= settings.kg_max_entities_per_user:
                logger.warning(f"KG: Entity limit reached for user {user_id}")
                return await self._get_oldest_entity(user_id)

        # Create new entity. circle_tier defaults to 0 (self) — owner can
        # promote later via /api/atoms/{id}/tier. The scope column is gone
        # but the model declaration retains it for back-compat (Lane C
        # cleanup will remove the ORM stub).
        #
        # Atom registration order matters here: the kg_entities.atom_id
        # column is NOT NULL with a non-deferrable FK to atoms.atom_id, so
        # the atoms row must exist BEFORE the entity INSERT (see #438).
        # We pre-create with a placeholder source_id, INSERT the entity
        # carrying the just-minted atom_id, then patch the atoms row's
        # source_id once entity.id is known.
        owner_id = await self._resolve_owner_user_id(user_id)
        default_tier = 0
        # owner_id is None only in dev/test setups with an empty users table;
        # in that path we skip atom registration and write the entity with
        # atom_id=None (the source-row ORM column is nullable). Production
        # always has the bootstrap admin, so the atom-backed path is the one
        # that actually runs.
        atom_id: str | None = None
        if owner_id is not None:
            atom_id = await self._create_atom_for_new_source(
                atom_type="kg_node",
                owner_user_id=owner_id,
                tier=default_tier,
            )
        entity = KGEntity(
            user_id=owner_id,
            name=name,
            entity_type=entity_type if entity_type in KG_ENTITY_TYPES else "thing",
            description=description,
            embedding=embedding,
            atom_id=atom_id,
            circle_tier=default_tier,
        )
        self.db.add(entity)
        await self.db.flush()
        if atom_id is not None:
            await self._finalize_atom_source_id(atom_id, entity.id)
        logger.debug(f"KG: New entity '{name}' ({entity_type}) id={entity.id} atom_id={atom_id}")
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

        # Lane C rewrite: scope column was DROPPED. Filter by user_id only;
        # the accessible_scopes parameter is kept in the signature for back-compat
        # with existing callers but is now ignored. Cross-user dedup returns
        # via v2 named-circles work.
        if user_id is not None:
            user_filter = "AND (user_id = :user_id OR user_id IS NULL)"
            params: dict = {"embedding": embedding_str, "user_id": user_id}
        else:
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
        """Get the oldest entity for a user (fallback when entity limit reached)."""
        # Lane C rewrite: dropped scope filter; just match on user_id.
        result = await self.db.execute(
            select(KGEntity)
            .where(
                KGEntity.user_id == user_id,
                KGEntity.is_active == True,  # noqa: E712
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

        # Inherit the relation's circle_tier from MIN(subject_tier, object_tier)
        # — the relation can be no more visible than the more-restricted endpoint
        # (CEO Finding E cascade rule, mirrors AtomService.update_tier:198-210).
        endpoints = (await self.db.execute(
            select(KGEntity.circle_tier).where(KGEntity.id.in_([subject_id, object_id]))
        )).scalars().all()
        relation_tier = min(endpoints) if endpoints else 0

        # Same atom-first ordering as _get_or_create_entity: kg_relations.atom_id
        # is NOT NULL with a non-deferrable FK, so the atoms row is pre-created
        # with a placeholder source_id and patched after the relation flushes.
        owner_id = await self._resolve_owner_user_id(user_id)
        atom_id: str | None = None
        if owner_id is not None:
            atom_id = await self._create_atom_for_new_source(
                atom_type="kg_edge",
                owner_user_id=owner_id,
                tier=relation_tier,
            )
        relation = KGRelation(
            user_id=owner_id,
            subject_id=subject_id,
            predicate=predicate,
            object_id=object_id,
            confidence=confidence,
            source_session_id=source_session_id,
            atom_id=atom_id,
            circle_tier=relation_tier,
        )
        self.db.add(relation)
        await self.db.flush()
        if atom_id is not None:
            await self._finalize_atom_source_id(atom_id, relation.id)
        logger.debug(
            f"KG: New relation {subject_id} --{predicate}--> {object_id} "
            f"atom_id={atom_id} tier={relation_tier}"
        )
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
            from sqlalchemy.orm import selectinload
            result = await self.db.execute(
                select(User).options(selectinload(User.role)).where(User.id == user_id)
            )
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

            # Broadcast to live KG graph viewers (fire-and-forget)
            try:
                from api.websocket.kg_live_handler import broadcast_kg_update

                await broadcast_kg_update(
                    entities=[
                        {
                            "id": e.id,
                            "name": e.name,
                            "type": e.entity_type,
                            "mention_count": e.mention_count,
                        }
                        for e in saved_entities
                    ],
                    relations=[
                        {
                            "id": r.id,
                            "subject_id": r.subject_id,
                            "predicate": r.predicate,
                            "object_id": r.object_id,
                            "confidence": r.confidence,
                        }
                        for r in saved_relations
                    ],
                )
            except Exception as e:
                logger.debug(f"KG live broadcast failed (non-critical): {e}")

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
            from sqlalchemy.orm import selectinload
            result = await self.db.execute(
                select(User).options(selectinload(User.role)).where(User.id == user_id)
            )
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
        user_role: str | None = None,  # kept for back-compat; ignored under circles
        lang: str = "de",
    ) -> str | None:
        """
        Retrieve relevant graph triples for a query, filtered by circle access.

        Lane C rewrite: this method ALWAYS delegates to KGRetrieval, regardless
        of the CIRCLES_USE_NEW_KG flag. The legacy inline scope-based body was
        removed because it referenced kg_entities.scope which was DROPPED by
        pc20260420_circles_v1_schema. The flag is preserved for back-compat
        with existing config but is now a no-op for this method.

        See services/kg_retrieval.py for the implementation. The kg_scope_loader
        and YAML scope config (config/kg_scopes.yaml) are no longer consulted.
        """
        from services.kg_retrieval import KGRetrieval
        return await KGRetrieval(self.db).get_relevant_context(
            query, user_id=user_id, user_role=user_role, lang=lang,
        )

    # =========================================================================
    # CRUD for API
    # =========================================================================

    async def list_entities(
        self,
        user_id: int | None = None,
        entity_type: str | None = None,
        search: str | None = None,
        circle_tier: int | None = None,
        page: int = 1,
        size: int = 50,
        asker_id: int | None = None,
    ) -> tuple[list[KGEntity], int]:
        """
        List active entities with optional filters.

        Circle access: when `asker_id` is provided, results are restricted to
        entities the asker can see (own + public + explicit-grant + tier-reach).
        `asker_id=None` in auth-enabled mode falls back to public-tier only;
        when `AUTH_ENABLED=false` the asker check is skipped entirely (the
        legacy "single-user sees everything" contract).

        The `user_id` filter is ORTHOGONAL to the circle check — callers
        requesting `?user_id=X` see only entities owned by X *that asker can
        also access*. Without the asker filter, any KG_VIEW caller could query
        `?user_id=<anyone>` and exfiltrate the full entity set (review BLOCKING #8).
        """
        from sqlalchemy import text as sa_text
        from services.circle_sql import kg_entities_circles_filter

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
        if circle_tier is not None:
            query = query.where(KGEntity.circle_tier == int(circle_tier))
            count_query = count_query.where(KGEntity.circle_tier == int(circle_tier))

        # Circle access check (review BLOCKING #8 fix).
        if not settings.auth_enabled:
            pass  # single-user bypass — no filter
        elif asker_id is None:
            from models.database import TIER_PUBLIC
            query = query.where(KGEntity.circle_tier == TIER_PUBLIC)
            count_query = count_query.where(KGEntity.circle_tier == TIER_PUBLIC)
        else:
            # Alias the KGEntity table as `e` so the helper's clause applies.
            clause, circle_params = kg_entities_circles_filter(asker_id, alias="kg_entities")
            query = query.where(sa_text(clause).bindparams(**circle_params))
            count_query = count_query.where(sa_text(clause).bindparams(**circle_params))

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

    async def update_entity_circle_tier(
        self,
        entity_id: int,
        circle_tier: int,
    ) -> KGEntity | None:
        """
        Update an entity's circle_tier (admin only).

        Cascades through AtomService when the entity has a backing atoms row,
        which:
          - rewrites atom.policy = {"tier": new_tier}
          - rewrites kg_relations.circle_tier on every incident edge using
            MIN(subject.circle_tier, object.circle_tier) (CEO Finding E)
          - invalidates resolver caches for the atom

        For entities without an atom_id (legacy rows the AtomService
        backfill missed), we update the column directly + manually cascade
        the kg_relations recompute, but skip the policy/cache machinery.
        """
        if circle_tier < 0 or circle_tier > TIER_PUBLIC:
            raise ValueError(
                f"Invalid circle_tier: {circle_tier} (must be 0..{TIER_PUBLIC})"
            )

        entity = await self.get_entity(entity_id)
        if not entity:
            return None

        if entity.atom_id:
            from services.atom_service import AtomService
            await AtomService(self.db).update_tier(
                entity.atom_id, {"tier": int(circle_tier)},
            )
            await self.db.refresh(entity)
            return entity

        # No atom_id — direct column write + manual relation recompute.
        # Explicit flush before the raw UPDATE so the cascade reads the new
        # entity.circle_tier via LEAST(). Don't rely on autoflush (some
        # session configs disable it; subtle drift if it ever flips).
        entity.circle_tier = int(circle_tier)
        await self.db.flush()
        await self.db.execute(
            text(
                "UPDATE kg_relations r SET circle_tier = "
                "LEAST(s.circle_tier, o.circle_tier) "
                "FROM kg_entities s, kg_entities o "
                "WHERE r.subject_id = s.id AND r.object_id = o.id "
                "AND (r.subject_id = :entity_id OR r.object_id = :entity_id)"
            ),
            {"entity_id": int(entity_id)},
        )
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
        from sqlalchemy.orm import selectinload

        async with AsyncSessionLocal() as db:
            user_role = None
            if user_id is not None:
                result = await db.execute(
                    select(User).options(selectinload(User.role)).where(User.id == user_id)
                )
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
