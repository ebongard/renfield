"""
Conversation Memory Service — Long-term memory for the assistant.

Stores facts, preferences, instructions, and context extracted from
conversations. Uses pgvector embeddings for semantic retrieval so the
assistant can recall relevant memories across sessions.

Pattern follows IntentFeedbackService for embedding generation and
cosine similarity search via raw SQL (pgvector).
"""
import json
import math
import re
import time
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    ATOM_TYPE_CONVERSATION_MEMORY,
    MEMORY_ACTION_CREATED,
    MEMORY_ACTION_DELETED,
    MEMORY_ACTION_UPDATED,
    MEMORY_CATEGORIES,
    MEMORY_CATEGORY_FACT,
    MEMORY_CATEGORY_PREFERENCE,
    MEMORY_CHANGED_BY_RESOLUTION,
    MEMORY_CHANGED_BY_SYSTEM,
    MEMORY_CHANGED_BY_USER,
    MEMORY_SCOPE_USER,
    MEMORY_SOURCE_LLM_INFERRED,
    ConversationMemory,
    MemoryHistory,
)
from services.atom_owner import AtomOwnerResolverMixin
from utils.config import settings
from utils.llm_client import get_default_client, get_embed_client

# ---------------------------------------------------------------------------
# Memory Poisoning Defense — pattern lists for extraction gating
# ---------------------------------------------------------------------------

_MEMORY_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?(?:previous\s+)?(?:instructions|rules)", re.I),
    re.compile(r"vergiss\s+(alle\s+)?(?:deine\s+)?regeln", re.I),
    re.compile(r"neue?\s+anweisungen?\s*:", re.I),
    re.compile(r"new\s+instructions?\s*:", re.I),
    re.compile(r"(?:ich\s+bin|i\s+am)\s+(?:der\s+|the\s+)?admin", re.I),
    re.compile(r"bypass\s+(?:auth|security|privacy)", re.I),
    re.compile(r"override\s+(?:system|security)", re.I),
    re.compile(r"(?:datenschutz|dsgvo)\s+(?:ignorieren|umgehen|gilt\s+nicht)", re.I),
]

_MEMORABLE_PATTERNS = [
    re.compile(r"\b(?:i\s+am|ich\s+bin|my\s+name\s+is|ich\s+hei(?:ss|ß)e)\b", re.I),
    re.compile(r"\b(?:i\s+(?:like|prefer|love|hate)|ich\s+(?:mag|bevorzuge|liebe|hasse))\b", re.I),
    re.compile(r"\b(?:remember\s+(?:that|this)|merk\s+dir|erinner(?:e|st)?\s+dich)\b", re.I),
    re.compile(r"\b(?:always|never|immer|nie(?:mals)?)\b.*\b(?:should|soll|must|muss)\b", re.I),
]

_TRANSACTIONAL_PATTERNS = [
    re.compile(r"^(?:show|list|search|find|get|display|zeig|such|find|hol|gib)\b", re.I),
    re.compile(r"^(?:turn\s+(?:on|off)|schalt[e]?|mach)\b", re.I),
    re.compile(r"^(?:play|stop|pause|next|skip|spiel|stopp)\b", re.I),
    re.compile(r"^(?:what\s+is|wie\s+(?:ist|wird)|was\s+ist)\b", re.I),
    re.compile(r"^(?:how\s+(?:many|much)|wieviel)\b", re.I),
]


class ConversationMemoryService(AtomOwnerResolverMixin):
    """
    Manages long-term conversation memories with semantic deduplication
    and retrieval via pgvector cosine similarity.
    """

    def __init__(self, db: AsyncSession):
        self.db = db
        self._embed_client = None
        self._chat_client = None

    def _atom_service(self):
        """Lazy AtomService bound to the same DB session."""
        from services.atom_service import AtomService
        return AtomService(self.db)

    async def _get_embed_client(self):
        """Lazy initialization of LLM client for embeddings (Qwen3-Embedding via llama-server-embed)."""
        if self._embed_client is None:
            self._embed_client = get_embed_client()
        return self._embed_client

    async def _get_chat_client(self):
        """Lazy initialization of LLM client for chat/extraction (Qwen3.6 via llama-server-agent)."""
        if self._chat_client is None:
            self._chat_client = get_default_client()
        return self._chat_client

    async def _get_embedding(self, text_input: str) -> list[float]:
        """Generate embedding via the embed-tier LLM client."""
        client = await self._get_embed_client()
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
        source: str | None = None,
        scope: str | None = None,
        team_id: str | None = None,
        confidence: float = 1.0,
        trigger_pattern: str | None = None,
        subject: str | None = None,
    ) -> ConversationMemory | None:
        """
        Save a memory with deduplication.

        ``subject`` is the person/entity the fact is ABOUT (a named person), stored
        in ``subject_name`` so retrieval can disambiguate per-subject instead of
        relying on embedding neighborhood — this is the structural fix for the
        cross-person conflation bug (D9). The KG-entity link (subject_entity_id)
        is populated by the Phase-3 bridge, not here.

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
                duplicate.last_accessed_at = datetime.now(UTC).replace(tzinfo=None)
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

        # Atom-first ordering: conversation_memories.atom_id is NOT NULL +
        # non-deferrable FK, so the atoms row must exist before the source
        # INSERT. See AtomService.create_with_source for the 3-phase
        # contract. owner_id is None only in fresh-DB dev setups; atom
        # registration is then skipped and memory.atom_id stays NULL
        # (ORM column is nullable, test SQLite lets this through).
        owner_id = await self._resolve_owner_user_id(user_id)
        default_tier = 0  # self — owner promotes via /api/atoms
        atom_id: str | None = None
        atom_svc = self._atom_service()
        if owner_id is not None:
            atom_id = await atom_svc.create_with_source(
                atom_type=ATOM_TYPE_CONVERSATION_MEMORY,
                owner_user_id=owner_id,
                tier=default_tier,
            )

        memory = ConversationMemory(
            content=content,
            category=category,
            user_id=owner_id,
            embedding=embedding,
            importance=importance,
            source_session_id=source_session_id,
            source_message_id=source_message_id,
            expires_at=expires_at,
            source=source or MEMORY_SOURCE_LLM_INFERRED,
            scope=scope or MEMORY_SCOPE_USER,
            team_id=team_id,
            confidence=confidence,
            trigger_pattern=trigger_pattern,
            subject_name=(subject.strip() or None) if subject else None,
            atom_id=atom_id,
            circle_tier=default_tier,
        )
        self.db.add(memory)
        await self.db.flush()
        if atom_id is not None:
            await atom_svc.finalize_source_id(atom_id, memory.id)

        await self._record_history(
            memory_id=memory.id,
            action=MEMORY_ACTION_CREATED,
            new_content=content,
            new_category=category,
            new_importance=importance,
            changed_by=MEMORY_CHANGED_BY_SYSTEM,
        )

        await self.db.commit()
        await self.db.refresh(memory)

        logger.info(
            f"Memory saved: category={category}, "
            f"user_id={user_id}, id={memory.id}"
        )
        return memory

    # =========================================================================
    # Memory Poisoning Defense
    # =========================================================================

    @staticmethod
    def should_extract_memories(user_msg: str, assistant_response: str) -> bool:
        """Determine whether to run memory extraction on this exchange.

        3-stage filter:
        1. BLOCK: Injection patterns detected -> skip extraction
        2. ALLOW: Memorable patterns present -> proceed to extraction
        3. SKIP: Transactional queries -> skip extraction
        4. DEFAULT: Proceed to LLM extraction (let the LLM decide)
        """
        # Stage 1: Block injection attempts.
        # Scan BOTH user_msg and assistant_response — the v2 prompt
        # interpolates both verbatim, so a poisoned MCP tool result
        # reflected into assistant_response is an injection vector too.
        for pattern in _MEMORY_INJECTION_PATTERNS:
            if pattern.search(user_msg):
                logger.info(
                    f"Memory extraction blocked: injection pattern in user_msg "
                    f"'{user_msg[:60]}...'"
                )
                return False
            if pattern.search(assistant_response):
                logger.info(
                    f"Memory extraction blocked: injection pattern in "
                    f"assistant_response '{assistant_response[:60]}...'"
                )
                return False

        # Stage 2: Allow memorable content
        for pattern in _MEMORABLE_PATTERNS:
            if pattern.search(user_msg):
                return True

        # Stage 3: Skip transactional queries
        stripped = user_msg.strip()
        for pattern in _TRANSACTIONAL_PATTERNS:
            if pattern.search(stripped):
                logger.debug(
                    f"Memory extraction skipped: transactional query "
                    f"'{user_msg[:60]}...'"
                )
                return False

        # Stage 4: Default — let LLM extraction decide
        return True

    # =========================================================================
    # Extract
    # =========================================================================

    async def extract_and_save(
        self,
        user_message: str,
        assistant_response: str,
        user_id: int | None = None,
        session_id: str | None = None,
        lang: str = "de",
        captured_kg_subjects: set[str] | None = None,
    ) -> list[ConversationMemory]:
        """Dispatcher — routes to v1 or v2 based on settings flags.

        Flag matrix:
          v2_authoritative=True  -> extract_and_save_v2 (v2 path; falls back
                                    to v1 on LLM/schema/drift failure)
          v2_shadow=True (only)  -> v1 returns; v2 then runs synchronously
                                    on the SAME session after v1 commits,
                                    logging its outcome to
                                    memory_v2_shadow_log. Synchronous (not
                                    fire-and-forget) because SQLAlchemy 2
                                    AsyncSession is not concurrent-safe.
          both False (default)   -> v1 only (current behavior)

        Public API kept stable so chat_handler and other callers don't
        change. The v2 path's fallback uses the private `_extract_and_save_v1_impl`
        directly to avoid an infinite dispatcher recursion when
        v2_authoritative is on.

        ``captured_kg_subjects`` (Phase 3-subsume per-fact fix): the lowercased
        subject NAMES of relations the KG extractor saved for THIS turn, passed
        by the chat handler after the `post_message` hook runs FIRST in the same
        background coroutine. It is the per-fact subsume signal; only the v1 path
        (the only path with a subsume gate) consumes it. None = legacy /
        uncoordinated caller → fall back to the subject-level proxy guard.
        """
        if settings.memory_extraction_v2_authoritative:
            return await self.extract_and_save_v2(
                user_message=user_message,
                assistant_response=assistant_response,
                user_id=user_id,
                session_id=session_id,
                lang=lang,
            )

        # v1 runs first (sequentially) so it has exclusive use of the
        # session. The shadow path then runs on the same session AFTER
        # v1 commits — concurrent task scheduling would race on the
        # shared AsyncSession (SQLAlchemy 2 async sessions are not
        # concurrent-safe).
        v1_started = time.monotonic()
        v1_result = await self._extract_and_save_v1_impl(
            user_message=user_message,
            assistant_response=assistant_response,
            user_id=user_id,
            session_id=session_id,
            lang=lang,
            captured_kg_subjects=captured_kg_subjects,
        )
        v1_latency = time.monotonic() - v1_started

        if settings.memory_extraction_v2_shadow:
            # Run shadow synchronously on the same session. chat_handler
            # already calls extract_and_save in a fire-and-forget post-
            # response context, so the doubled latency does not affect
            # user-facing response time.
            try:
                await self._extract_v2_shadow_only(
                    user_message=user_message,
                    assistant_response=assistant_response,
                    user_id=user_id,
                    session_id=session_id,
                    lang=lang,
                    v1_outcome=f"saved_{len(v1_result)}" if v1_result else "noop",
                    v1_extracted_count=len(v1_result),
                    v1_latency_seconds=v1_latency,
                )
            except Exception as e:
                # Shadow must NEVER affect the primary path.
                logger.warning(
                    "v2 shadow: outer call failed (swallowed): %s", type(e).__name__
                )

        return v1_result

    async def _extract_v2_shadow_only(
        self,
        user_message: str,
        assistant_response: str,
        user_id: int | None,
        session_id: str | None,
        lang: str,
        v1_outcome: str | None = None,
        v1_extracted_count: int | None = None,
        v1_latency_seconds: float | None = None,
    ) -> None:
        """Run v2 in shadow mode + log v1 vs v2 outcome to memory_v2_shadow_log.

        Calls extract_and_save_v2 (LLM + drift check), then ROLLS BACK any
        writes via a savepoint so production state is unaffected. Writes
        a single row to memory_v2_shadow_log capturing both v1's outcome
        (passed in from the dispatcher, the authoritative result the user
        saw) and v2's outcome (rolled back).

        Errors in shadow mode are swallowed; they cannot affect the
        primary v1 path. Failures still land in the shadow log with
        v2_error set, so the daily diff report sees them.
        """
        from models.database import MemoryV2ShadowLog

        v2_outcome = None
        v2_count: int | None = None
        v2_ops_json: str | None = None
        v2_latency: float | None = None
        v2_error: str | None = None
        v2_fallback_reason: str | None = None
        sp = None

        started = time.monotonic()
        ops_capture: list[str] = []
        try:
            sp = await self.db.begin_nested()
            result = await self.extract_and_save_v2(
                user_message=user_message,
                assistant_response=assistant_response,
                user_id=user_id,
                session_id=session_id,
                lang=lang,
                _ops_capture=ops_capture,
            )
            v2_latency = time.monotonic() - started
            v2_count = len(result)
            # Capture the LLM's serialized MemoryOpsList for the diff report.
            # ops_capture stays empty when v2 fell back to v1 on LLM/schema
            # reject (no ops to log); a drift-reject still populates it because
            # the LLM produced a valid (but stale) ops list.
            if ops_capture:
                v2_ops_json = ops_capture[0]
            if v2_count == 0:
                v2_outcome = "noop"
            else:
                v2_outcome = "saved"  # mixed ADD/UPDATE/DELETE collapsed
        except Exception as e:
            v2_latency = time.monotonic() - started
            v2_error = type(e).__name__
            v2_outcome = "error"
            logger.warning(
                "v2 shadow: extraction failed (swallowed): %s", v2_error
            )
        finally:
            if sp is not None:
                try:
                    await sp.rollback()
                except Exception as e_rb:
                    # Savepoint rollback failed — log and continue. The
                    # outer caller's transaction will eventually decide
                    # whether to commit (v1 writes will persist regardless).
                    logger.warning(
                        f"v2 shadow: savepoint rollback failed (swallowed): {type(e_rb).__name__}: {e_rb}"
                    )

        # Write the shadow log row in its OWN savepoint so a log-table
        # failure (e.g., schema drift, FK violation) does not cascade
        # into the v1 transaction.
        log_sp = None
        try:
            log_sp = await self.db.begin_nested()
            self.db.add(MemoryV2ShadowLog(
                user_id=user_id,
                session_id=session_id,
                lang=lang,
                v1_outcome=v1_outcome,
                v1_extracted_count=v1_extracted_count,
                v1_latency_seconds=v1_latency_seconds,
                v2_outcome=v2_outcome,
                v2_ops_json=v2_ops_json,
                v2_extracted_count=v2_count,
                v2_fallback_reason=v2_fallback_reason,
                v2_latency_seconds=v2_latency,
                v2_error=v2_error,
            ))
            await self.db.flush()
            # Release the savepoint into the outer transaction.
            await log_sp.commit()
            # Persist the outer transaction. Without this, callers that
            # don't commit explicitly (e.g. chat_handler's
            # `async with AsyncSessionLocal()` which closes WITHOUT
            # committing the autobegun outer transaction) silently lose
            # the shadow log row on session close — verified empirically
            # in prod 2026-05-14 where 4 chat turns produced 0 shadow
            # log rows despite extract_and_save_v2 running cleanly. The
            # log row is the WHOLE POINT of shadow mode; persist it
            # ourselves rather than depending on the caller's commit
            # discipline.
            await self.db.commit()
        except Exception as e:
            if log_sp is not None:
                try:
                    await log_sp.rollback()
                except Exception:
                    pass
            logger.warning(
                f"v2 shadow: log-row write failed (swallowed): {type(e).__name__}: {e}"
            )

    async def _extract_and_save_v1_impl(
        self,
        user_message: str,
        assistant_response: str,
        user_id: int | None = None,
        session_id: str | None = None,
        lang: str = "de",
        captured_kg_subjects: set[str] | None = None,
    ) -> list[ConversationMemory]:
        """v1 extraction implementation. Called directly by:
          - the public extract_and_save() dispatcher when both v2 flags are off
          - extract_and_save_v2's fallback path on any v2 failure
        Do NOT add the v2 flag dispatch here — would recurse.

        ``captured_kg_subjects`` (Phase 3-subsume per-fact fix): lowercased
        subject NAMES of relations the KG extractor actually saved THIS turn (the
        chat handler runs KG extraction first and threads the captured set here).
        Used as the PRIMARY subsume gate: a fact is dropped-as-subsumed only when
        its subject is in this set. None = no coordination available → fall back
        to the subject-level proxy ``_subject_is_kg_representable``.
        """
        # Guard: Skip extraction for injection attempts and transactional queries
        if not self.should_extract_memories(user_message, assistant_response):
            return []

        from services.prompt_manager import prompt_manager

        # Build extraction prompt
        prompt = prompt_manager.get(
            "memory", "extraction_prompt", lang=lang,
            user_message=user_message,
            assistant_response=assistant_response,
        )
        system_msg = prompt_manager.get(
            "memory", "extraction_system", lang=lang,
        )
        llm_options = prompt_manager.get_config("memory", "llm_options") or {}

        # LLM call. Extraction expects strict JSON, so we disable thinking
        # mode for thinking-capable models (Qwen3, Qwen3.6, deepseek-r1, …);
        # otherwise the JSON ends up in `reasoning_content` and `content` is
        # empty, returning an "extracted=0" silent miss. Same fix pattern as
        # the KG and intent paths (see utils/llm_client.py).
        from utils.llm_client import extract_response_content, get_classification_chat_kwargs

        try:
            client = await self._get_chat_client()
            extraction_model = settings.memory_extraction_model or settings.ollama_model
            response = await client.chat(
                model=extraction_model,
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": prompt},
                ],
                options=llm_options,
                **get_classification_chat_kwargs(extraction_model),
            )
            raw_text = extract_response_content(response)
        except Exception as e:
            logger.warning(f"Memory extraction LLM call failed: {e}")
            return []

        # Parse JSON array from response
        extracted = self._parse_extraction_response(raw_text)
        if not extracted:
            return []

        # Save each extracted fact (cap to avoid runaway DB calls)
        max_extracts = 10
        saved: list[ConversationMemory] = []
        for item in extracted[:max_extracts]:
            content = item.get("content", "").strip()
            category = item.get("category", "").strip().lower()
            importance = item.get("importance", 0.5)
            trigger_pattern = item.get("trigger_pattern")
            subject = (item.get("subject") or "").strip() or None

            if not content:
                continue
            if category not in MEMORY_CATEGORIES:
                logger.debug(f"Skipping extracted memory with invalid category: {category}")
                continue

            # Phase 3-subsume (opt-in): a decomposable fact about a named subject
            # lives in the KG (entities + relations, extracted by the KG hook on
            # the SAME turn), so don't also store a flat duplicate. Preferences/
            # instructions/context/procedural stay flat (their object is often not
            # a named entity). Off (default) => unchanged.
            #
            # RECALL-LOSS GATE (memory_subsume_require_kg_relation, default on):
            # the KG extraction only persists a relation when the fact's OBJECT
            # is a named entity (a state/feeling — "müde", "krank", "gestresst" —
            # yields no entity, no relation). Dropping the flat store for such a
            # fact loses it silently and unrecoverably.
            #
            # PER-(SUBJECT, TURN) GATE (Phase 3-subsume coordination): when the
            # chat handler coordinates the two background extractors (runs KG
            # first and threads the saved-relation subjects in via
            # `captured_kg_subjects`), we subsume a fact ONLY when its subject is
            # among the subjects the KG actually captured a relation for THIS
            # turn. That closes the CROSS-TURN residual the old subject-level
            # proxy missed: a state-fact about an ALREADY-related person ("Anna
            # ist müde" while Anna already lives somewhere in the KG) has subject
            # NOT in `captured_kg_subjects` this turn → kept flat. A named-entity-
            # object fact ("Anna wohnt in Bonn") whose relation IS saved this
            # turn → subsumed.
            #
            # NOT truly per-fact (caveat): the signal is subject NAMES, not
            # (subject, object) pairs. A same-turn, same-subject mix (one entity-
            # object fact + one state fact) still subsumes the state fact — a
            # narrower residual measured by the `mixed-same-subject-*` eval case.
            #
            # FALLBACK: `captured_kg_subjects is None` means an uncoordinated
            # caller (legacy / non-chat path) — fall back to the subject-level
            # proxy `_subject_is_kg_representable`. See tests/eval/
            # subsume_recall_loss_eval.yaml for the measured loss surface.
            if settings.memory_subsume_to_kg and category == MEMORY_CATEGORY_FACT and subject:
                if await self._should_subsume_fact(
                    subject, user_id, captured_kg_subjects
                ):
                    logger.debug(f"📥 Subsuming fact to KG (skip flat memory): subject={subject!r}")
                    continue
                logger.debug(
                    f"🛟 Subsume gate: keeping fact flat — no KG relation captured "
                    f"this turn for subject={subject!r}"
                )

            # Clamp importance to valid range
            try:
                importance = max(0.1, min(1.0, float(importance)))
            except (TypeError, ValueError):
                importance = 0.5

            # Validate trigger_pattern if provided (procedural memories only)
            if trigger_pattern and category == "procedural":
                try:
                    re.compile(trigger_pattern)
                except re.error:
                    trigger_pattern = None  # Invalid regex, discard

            if settings.memory_contradiction_resolution:
                memory = await self._apply_contradiction_resolution(
                    content=content,
                    category=category,
                    importance=importance,
                    user_id=user_id,
                    session_id=session_id,
                    lang=lang,
                )
            else:
                memory = await self.save(
                    content=content,
                    category=category,
                    user_id=user_id,
                    importance=importance,
                    source_session_id=session_id,
                    trigger_pattern=trigger_pattern if category == "procedural" else None,
                    subject=subject,
                )
            if memory:
                await self._bridge_subject_entity(memory, subject, category)
                saved.append(memory)

        return saved

    async def _bridge_subject_entity(
        self,
        memory: ConversationMemory,
        subject: str | None,
        category: str,
    ) -> None:
        """Phase 3 bridge: link a decomposable memory's subject to a canonical KG entity.

        Runs only here, in the background extraction path (never the synchronous
        turn). Decomposable facts/preferences are about a named subject (a person);
        resolve that subject to the canonical entity — reusing whatever the turn's
        KG extraction already created, since resolve_entity is name-idempotent — and
        store its id in subject_entity_id so "Was weiß ich über X" becomes
        deterministic. Non-decomposable categories (procedural/instruction/context)
        stay flat.

        Gated behind MEMORY_KG_BRIDGE_ENABLED (opt-in, dark by default). Best-effort:
        a resolve failure leaves subject_entity_id NULL (backfill or the next mention
        links it later). type-scoped + tier-pinned resolution (Phase 3a) prevents
        wrong-type links and self-tier leaks.
        """
        if not settings.memory_kg_bridge_enabled:
            return
        if memory is None or memory.user_id is None:
            return
        if not subject or category not in (MEMORY_CATEGORY_FACT, MEMORY_CATEGORY_PREFERENCE):
            return
        if memory.subject_entity_id is not None:
            return
        try:
            from services.knowledge_graph_service import KnowledgeGraphService
            ent = await KnowledgeGraphService(self.db).resolve_entity(
                subject, "person", memory.user_id,
                create_tier=memory.circle_tier,
                match_entity_type=True,
                use_embedding=False,  # bare names embed-conflate across people (Jutta→Anna); exact/surface/create only
            )
            memory.subject_entity_id = ent.id
            if not memory.subject_name:
                memory.subject_name = subject
            await self.db.commit()
        except Exception as e:  # noqa: BLE001 — bridge is best-effort; never break extraction
            await self.db.rollback()
            logger.warning(
                f"Memory KG bridge failed for memory #{getattr(memory, 'id', '?')} "
                f"subject={subject!r}: {e}"
            )

    async def _should_subsume_fact(
        self,
        subject: str | None,
        user_id: int | None,
        captured_kg_subjects: set[str] | None,
    ) -> bool:
        """Per-SUBJECT-per-turn subsume decision (Phase 3-subsume coordination).

        Returns True iff this fact may be dropped (its subject is demonstrably
        represented in the KG this turn). The PRIMARY signal is per-turn:
        ``captured_kg_subjects`` is the set of subject names the KG extractor
        actually saved a relation for THIS turn (threaded in by the chat handler
        after the `post_message` hook ran first in the same background
        coroutine). A fact is subsumed only when its subject is in that set — so
        a state/attribute fact about a subject for whom NO relation was saved
        this turn ("Anna ist müde", even for an already-related Anna) is kept
        flat. This closes the CROSS-TURN residual the subject-level proxy missed
        (the proxy keyed on PRIOR relations; this keys on THIS turn's capture).

        GRANULARITY CAVEAT — this is per-(subject, turn), NOT truly per-fact.
        The set holds subject NAMES, not (subject, object) pairs. So a single
        turn that yields TWO facts about the SAME subject — one with a named-
        entity object (relation saved → subject in the set) and one a state/
        attribute fact (no relation) — STILL subsumes the state fact, because
        the subject is in the set. That same-turn, same-subject, mixed-object
        case is a narrower residual loss that remains open (measured by the
        ``mixed-same-subject-*`` eval case). Truly per-fact would need a
        per-(subject, object) captured signal matched to each fact's object —
        not built here.

        Coordination invariants:
          * KG extraction runs exactly once (in the hook); the set is reused, not
            re-extracted.
          * ``memory_subsume_require_kg_relation`` off → legacy unguarded subsume
            (always True), unchanged.
          * ``captured_kg_subjects is None`` (uncoordinated / legacy caller) →
            fall back to the subject-level proxy ``_subject_is_kg_representable``
            so the harm-reduction behavior is preserved when no per-turn signal
            exists. The coordinated chat path always passes a set (possibly
            empty), so the proxy is no longer the primary gate there.

        MULTI-USER GAP (unchanged): subsume remains single-user only. The
        captured set is name-based and the subject came verbatim from the memory
        extractor; cross-user subject resolution + tier reach are NOT addressed
        here (see TODOS.md). Do not enable ``memory_subsume_to_kg`` multi-user.
        """
        if not settings.memory_subsume_require_kg_relation:
            return True  # legacy unguarded subsume
        if not subject:
            return False
        if captured_kg_subjects is not None:
            # Per-(subject, turn) signal (authoritative when coordination ran).
            # NOT per-(subject, object): a same-turn same-subject state fact is
            # still subsumed if any relation for the subject was saved (caveat
            # in the docstring).
            return subject.strip().lower() in captured_kg_subjects
        # Uncoordinated caller — fall back to the subject-level proxy.
        return await self._subject_is_kg_representable(subject, user_id)

    async def _subject_is_kg_representable(
        self, subject: str | None, user_id: int | None
    ) -> bool:
        """Subsume recall-loss guard: does the KG already hold a relation for this subject?

        This is a loss-REDUCTION proxy, NOT a per-fact guarantee. Recall loss is
        per-FACT: a state/feeling fact whose object is not a named entity ("Anna
        ist müde") produces no KG relation and is lost when subsumed — even if
        Anna is an already-related person, so this guard returns True and STILL
        lets that fact be dropped. What the guard actually protects is the
        common-case worst loss: a fact about a NEVER-BEFORE-RELATED subject
        (the subject entity carries 0 relations). For those it keeps the fact
        flat (recoverable) rather than subsuming on faith. The real per-fact fix
        is a follow-up (TODOS.md): subsume only when THIS turn captured a
        relation for the subject, which needs coordinating the currently
        uncoordinated memory-extract and KG-extract async tasks.

        Selection mirrors ``KnowledgeGraphService.resolve_entity``'s exact-name
        step so the same row is chosen: live + canonical (``canonical_id IS
        NULL``), own-or-unowned, restricted to ``entity_type == 'person'`` (the
        subsume target is always a named person), ordered
        ``circle_tier ASC, mention_count DESC`` so a same-name homonym /
        wrong-tier row can't be picked arbitrarily and cause a wrong-entity
        false-positive (which would subsume the wrong person's fact = loss).
        Note we do NOT chase surface-forms here (the subject came verbatim from
        the memory extractor, already an exact name); a surface-form-only miss
        just keeps the fact flat = fail-safe.

        Same-turn race is intentional: the KG extraction for THIS turn runs as a
        separate concurrent task on its own session, so relations it creates this
        turn are intentionally invisible here. Consequence: the first fact about
        a brand-new subject whose object IS an entity gets a harmless flat
        duplicate (recoverable), never a loss — the safe direction.

        Fail-safe: any miss / error / resolve-failure → False (keep the fact flat).
        Disabling ``memory_subsume_require_kg_relation`` reverts to the legacy
        unguarded behavior (always subsume a fact+subject). This guard does NOT
        make ``memory_subsume_to_kg`` safe for multi-user (see TODOS.md).
        """
        if not settings.memory_subsume_require_kg_relation:
            return True  # legacy unguarded subsume
        if not subject or user_id is None:
            return False
        try:
            from sqlalchemy import or_

            from models.database import KGEntity, KGRelation
            # Resolve the subject to a canonical PERSON entity WITHOUT creating
            # one — mirror resolve_entity's exact-name step (live + canonical,
            # own-or-unowned, person-typed, deterministic order).
            ent_row = await self.db.execute(
                select(KGEntity.id, KGEntity.canonical_id)
                .where(
                    func.lower(KGEntity.name) == subject.strip().lower(),
                    KGEntity.is_active == True,  # noqa: E712
                    KGEntity.canonical_id.is_(None),
                    KGEntity.entity_type == "person",
                    or_(KGEntity.user_id == user_id, KGEntity.user_id.is_(None)),
                )
                .order_by(KGEntity.circle_tier.asc(), KGEntity.mention_count.desc())
                .limit(1)
            )
            row = ent_row.first()
            if row is None:
                return False
            # canonical_id IS NULL is filtered above, so row.id is the survivor;
            # keep the tombstone-follow defensively in case the filter is relaxed.
            entity_id = row.canonical_id or row.id
            rel = await self.db.execute(
                select(KGRelation.id)
                .where(
                    KGRelation.user_id == user_id,
                    KGRelation.subject_id == entity_id,
                )
                .limit(1)
            )
            return rel.first() is not None
        except Exception as e:  # noqa: BLE001 — guard is best-effort, fail-safe to flat
            logger.warning(
                f"Subsume KG-representability probe failed for subject={subject!r}: {e}"
            )
            return False

    @staticmethod
    def _parse_extraction_response(raw_text: str) -> list[dict]:
        """Parse JSON array from LLM extraction response.

        Handles markdown code blocks, extra text around the JSON,
        and other common LLM output artifacts.
        """
        if not raw_text:
            return []

        text = raw_text.strip()

        # Remove markdown code blocks
        if "```" in text:
            match = re.search(r'```(?:json)?\s*(\[.*?\])\s*```', text, re.DOTALL)
            if match:
                text = match.group(1)
            else:
                parts = text.split("```")
                if len(parts) >= 2:
                    text = parts[1].strip()
                    if text.startswith("json"):
                        text = text[4:].strip()

        # Find balanced brackets for JSON array
        first_bracket = text.find('[')
        if first_bracket >= 0:
            depth = 0
            in_string = False
            escape_next = False
            end_pos = -1
            for i in range(first_bracket, len(text)):
                c = text[i]
                if escape_next:
                    escape_next = False
                    continue
                if c == '\\' and in_string:
                    escape_next = True
                    continue
                if c == '"' and not escape_next:
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if c == '[':
                    depth += 1
                elif c == ']':
                    depth -= 1
                    if depth == 0:
                        end_pos = i
                        break
            if end_pos > 0:
                text = text[first_bracket:end_pos + 1]

        try:
            data = json.loads(text)
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)]
            return []
        except (json.JSONDecodeError, TypeError):
            logger.debug(f"Memory extraction: could not parse JSON from: {raw_text[:200]}")
            return []

    # =========================================================================
    # Extract v2 — Mem0-style batched extraction (Lane B/2)
    # =========================================================================
    #
    # Single LLM tool call emits a MemoryOpsList for the whole turn (was: 1
    # extract call + N per-fact contradiction calls in v1). Schema enforced
    # by services/memory_ops.py. Prompt at prompts/memory.yaml:extraction_v2_*.
    #
    # Lock semantics: session-level pg_advisory_lock held only around
    # retrieve + apply, dropped for the LLM call. Optimistic concurrency:
    # at apply time, re-retrieve and check whether the candidate-id set has
    # drifted; if so, reject the batch and fall back to v1. Caller controls
    # the outer transaction; this method does NOT call self.db.commit().

    _LOCK_KEY_NAMESPACE = 0x4D454D30  # ASCII "MEM0"

    @staticmethod
    def _user_lock_key(user_id: int) -> int:
        """Build a 64-bit bigint key for pg_advisory_lock(bigint).

        High 32 bits namespace = "MEM0", low 32 bits = user_id (masked).
        Prevents collision with any future feature using advisory locks.
        """
        return (ConversationMemoryService._LOCK_KEY_NAMESPACE << 32) | (int(user_id) & 0xFFFFFFFF)

    async def _acquire_user_lock(self, user_id: int | None) -> None:
        """Session-level lock. Pair with `_release_user_lock` in try/finally."""
        if user_id is None:
            return
        await self.db.execute(
            text("SELECT pg_advisory_lock(:k)"),
            {"k": self._user_lock_key(user_id)},
        )

    async def _release_user_lock(self, user_id: int | None) -> None:
        if user_id is None:
            return
        try:
            await self.db.execute(
                text("SELECT pg_advisory_unlock(:k)"),
                {"k": self._user_lock_key(user_id)},
            )
        except Exception as e:
            # Don't propagate — the connection pool's `before_checkin`
            # event handler in services/database.py runs
            # pg_advisory_unlock_all() as a defensive sweep before the
            # connection returns to the pool, so a release-here failure
            # does NOT leak the lock to the next caller. The earlier
            # comment claiming "the lock releases when the session
            # disconnects anyway" was wrong: pg_advisory_lock is
            # session-level (= per-connection), and a pool checkin does
            # NOT disconnect the underlying connection. Without the
            # before_checkin handler this release failure would block
            # all subsequent v2 calls for the same user_id indefinitely.
            logger.warning(f"v2 extract: pg_advisory_unlock failed: {e}")

    async def _call_extract_v2_llm(
        self,
        user_message: str,
        assistant_response: str,
        existing_memories: list[dict],
        lang: str,
    ):
        """Build the v2 prompt, call the chat LLM, parse JSON → MemoryOpsList.

        Returns a MemoryOpsList on success, or None on any parse / schema /
        LLM failure. The caller treats None as schema-reject and falls
        back to v1.
        """
        import pydantic as _p
        from services.memory_ops import MemoryOpsList
        from services.prompt_manager import prompt_manager
        from utils.llm_client import extract_response_content, get_classification_chat_kwargs

        # Render the existing-memories block.
        if existing_memories:
            existing_text = "\n".join(
                f"- id={int(c.get('id'))}: {c.get('content', '')} "
                f"(category={c.get('category', '')}, importance={c.get('importance', 0.5)})"
                for c in existing_memories if c.get("id") is not None
            )
        else:
            existing_text = (
                "(keine bestehenden Erinnerungen)" if lang == "de"
                else "(no existing memories)"
            )

        try:
            prompt = prompt_manager.get(
                "memory", "extraction_v2_prompt", lang=lang,
                user_message=user_message,
                assistant_response=assistant_response,
                existing_memories=existing_text,
            )
            system_msg = prompt_manager.get("memory", "extraction_v2_system", lang=lang)
        except Exception as e:
            logger.warning(f"v2 extract: prompt render failed: {type(e).__name__}: {e}")
            return None

        llm_options = prompt_manager.get_config("memory", "llm_options") or {}

        try:
            client = await self._get_chat_client()
            extraction_model = settings.memory_extraction_model or settings.ollama_model
            response = await client.chat(
                model=extraction_model,
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": prompt},
                ],
                options=llm_options,
                **get_classification_chat_kwargs(extraction_model),
            )
            raw_text = extract_response_content(response)
        except Exception as e:
            logger.warning(f"v2 extract: LLM call failed: {type(e).__name__}")
            return None

        # Two-step: use v1's robust JSON-array parser to handle markdown
        # fences + extra prose, then validate via MemoryOpsList.
        ops_dicts = self._parse_extraction_response(raw_text)
        if not ops_dicts and raw_text.strip():
            logger.warning("v2 extract: parse_error on non-empty LLM response")
            return None

        try:
            return MemoryOpsList(root=ops_dicts)
        except _p.ValidationError as e:
            logger.warning(f"v2 extract: MemoryOpsList schema reject: {e}")
            return None

    async def _apply_add_v2(
        self,
        *,
        content: str,
        category: str,
        importance: float,
        user_id: int | None,
        session_id: str | None,
    ) -> ConversationMemory | None:
        """Insert a new memory + atom + history row WITHOUT committing.

        Differs from `save()` in three ways:
          1. No internal commit (caller's session controls).
          2. No dedup fast-path (v2 dedups via the LLM seeing candidates).
          3. No max-per-user enforcement (handled semantically by the
             batched LLM call).
        """
        if category not in MEMORY_CATEGORIES:
            logger.warning(f"v2 extract: invalid category {category!r}")
            return None

        embedding = None
        try:
            embedding = await self._get_embedding(content)
        except Exception as e:
            logger.warning(f"v2 extract: embedding failed on ADD: {type(e).__name__}")

        owner_id = await self._resolve_owner_user_id(user_id)
        default_tier = 0
        atom_id: str | None = None
        atom_svc = self._atom_service()
        if owner_id is not None:
            atom_id = await atom_svc.create_with_source(
                atom_type=ATOM_TYPE_CONVERSATION_MEMORY,
                owner_user_id=owner_id,
                tier=default_tier,
            )

        memory = ConversationMemory(
            content=content,
            category=category,
            user_id=owner_id,
            embedding=embedding,
            importance=importance,
            source_session_id=session_id,
            source=MEMORY_SOURCE_LLM_INFERRED,
            scope=MEMORY_SCOPE_USER,
            atom_id=atom_id,
            circle_tier=default_tier,
        )
        self.db.add(memory)
        await self.db.flush()
        if atom_id is not None:
            await atom_svc.finalize_source_id(atom_id, memory.id)
        await self._record_history(
            memory_id=memory.id,
            action=MEMORY_ACTION_CREATED,
            new_content=content,
            new_category=category,
            new_importance=importance,
            changed_by=MEMORY_CHANGED_BY_SYSTEM,
        )
        return memory

    async def _apply_update_v2(
        self,
        *,
        target_id: int,
        content: str,
        category: str | None,
        importance: float | None,
        user_id: int | None,
    ) -> bool:
        """Apply UPDATE op to an existing row. Re-embeds the new content.
        No internal commit.

        Defense-in-depth: when user_id is provided, the WHERE clause
        scopes the UPDATE to rows owned by that user. Even if the LLM
        produces a target_id that escaped the candidate-set membership
        check (poisoned retrieval, validator bug), this prevents
        cross-user mutation at the SQL layer.
        """
        new_embedding = None
        try:
            new_embedding = await self._get_embedding(content)
        except Exception as e:
            logger.warning(f"v2 extract: re-embedding failed on UPDATE: {type(e).__name__}")

        values: dict = {
            "content": content,
            "last_accessed_at": datetime.now(UTC).replace(tzinfo=None),
            "access_count": ConversationMemory.access_count + 1,
        }
        if category is not None:
            if category not in MEMORY_CATEGORIES:
                logger.warning(f"v2 UPDATE: invalid category {category!r}; preserving existing")
            else:
                values["category"] = category
        if importance is not None:
            values["importance"] = importance
        if new_embedding is not None:
            values["embedding"] = new_embedding

        stmt = update(ConversationMemory).where(ConversationMemory.id == target_id)
        if user_id is not None:
            stmt = stmt.where(ConversationMemory.user_id == user_id)
        result = await self.db.execute(stmt.values(**values))
        if result.rowcount > 0:
            await self._record_history(
                memory_id=target_id,
                action=MEMORY_ACTION_UPDATED,
                new_content=content,
                new_category=category,
                new_importance=importance,
                changed_by=MEMORY_CHANGED_BY_RESOLUTION,
            )
            return True
        return False

    async def _apply_delete_v2(self, *, target_id: int, user_id: int | None) -> bool:
        """Apply DELETE op — soft-delete via is_active=false. No internal commit.

        Per the retention-posture decision: DELETE fires ONLY on explicit
        user retraction. No automated process flips is_active. The row
        stays recoverable via `/admin/recall?include_inactive=true`.

        Defense-in-depth on user_id: same rationale as _apply_update_v2.
        """
        stmt = update(ConversationMemory).where(ConversationMemory.id == target_id)
        if user_id is not None:
            stmt = stmt.where(ConversationMemory.user_id == user_id)
        result = await self.db.execute(stmt.values(
            is_active=False,
            last_accessed_at=datetime.now(UTC).replace(tzinfo=None),
        ))
        if result.rowcount > 0:
            await self._record_history(
                memory_id=target_id,
                action=MEMORY_ACTION_DELETED,
                changed_by=MEMORY_CHANGED_BY_USER,
            )
            return True
        return False

    async def extract_and_save_v2(
        self,
        user_message: str,
        assistant_response: str,
        user_id: int | None = None,
        session_id: str | None = None,
        lang: str = "de",
        _ops_capture: list[str] | None = None,
    ) -> list[ConversationMemory]:
        """Mem0-style batched extraction.

        Replaces v1's per-fact contradiction-resolution loop with a single
        LLM tool call that emits a MemoryOpsList for the whole turn.
        Returns the list of memories added/updated (NOOP ops omitted).
        Caller is responsible for committing the session.

        Failure path: any LLM / parse / schema / drift error falls back to
        `extract_and_save` (v1). v1 commits internally — callers wanting
        strict rollback must wrap this in a savepoint.

        Required setting: `memory_extraction_retrieve_k` (default 5).
        Honors the same `should_extract_memories` injection /
        transactional gate as v1.

        `_ops_capture` is an opt-in side-channel for shadow mode: when a
        list is passed, the JSON-serialized MemoryOpsList from the LLM
        is appended to it before any apply or fallback. Used by
        `_extract_v2_shadow_only` to populate `v2_ops_json` on the
        shadow log row without changing this method's return type.
        """
        from services.memory_ops import OpType, validate_against_candidates
        from services.memory_retrieval import MemoryRetrieval

        if not self.should_extract_memories(user_message, assistant_response):
            return []

        retrieve_k = max(1, int(settings.memory_extraction_retrieve_k))
        # Lane D — extract pipeline uses its own (lower) threshold than chat
        # retrieval. See `memory_extract_retrieval_threshold` in utils/config.py
        # and `docs/lane-d-extract-retrieval-threshold.md` for the A/B that
        # locked the production default at 0.0.
        extract_threshold = float(settings.memory_extract_retrieval_threshold)

        # ---- Phase 1: lock + retrieve (no LLM call inside the lock) ----
        candidates: list[dict] = []
        await self._acquire_user_lock(user_id)
        try:
            candidates = await MemoryRetrieval(self.db).retrieve(
                message=user_message, user_id=user_id, limit=retrieve_k,
                threshold=extract_threshold,
                ranker="recency_aware",
            )
        finally:
            await self._release_user_lock(user_id)

        candidate_ids_initial: set[int] = {
            int(c["id"]) for c in candidates if c.get("id") is not None
        }

        # ---- Phase 2: LLM call (no lock held) ----
        ops_list = await self._call_extract_v2_llm(
            user_message=user_message,
            assistant_response=assistant_response,
            existing_memories=candidates,
            lang=lang,
        )
        if ops_list is None:
            logger.info("v2 extract: LLM/schema rejected → fallback to v1")
            # Call v1 impl directly — going through the dispatcher would
            # recurse if v2_authoritative is on.
            return await self._extract_and_save_v1_impl(
                user_message=user_message,
                assistant_response=assistant_response,
                user_id=user_id,
                session_id=session_id,
                lang=lang,
            )

        # Capture for shadow log: the LLM produced a valid ops list.
        # Capture BEFORE drift check so the shadow log records the LLM's
        # intent even when drift forces a v1 fallback.
        if _ops_capture is not None:
            try:
                _ops_capture.append(ops_list.model_dump_json())
            except Exception as e_dump:
                logger.warning(
                    "v2 extract: ops_capture serialization failed: %s",
                    type(e_dump).__name__,
                )

        # ---- Phase 3: re-lock + drift check + apply ops ----
        saved: list[ConversationMemory] = []
        drift_reject = False
        await self._acquire_user_lock(user_id)
        try:
            # Re-retrieve to detect candidate drift since the LLM was called.
            # Use the same threshold strategy as Phase 1 so the drift check
            # sees the same candidate ID set.
            fresh = await MemoryRetrieval(self.db).retrieve(
                message=user_message, user_id=user_id, limit=retrieve_k,
                threshold=extract_threshold,
                ranker="recency_aware",
            )
            fresh_ids = {int(c["id"]) for c in fresh if c.get("id") is not None}

            rejection = validate_against_candidates(ops_list, fresh_ids)
            if rejection is not None:
                logger.info(f"v2 extract: drift rejected ({rejection}) → fallback to v1")
                drift_reject = True
                # Fall through to release the lock, then run v1 OUTSIDE it.
                # Holding the advisory lock through v1's LLM latency would
                # serialise concurrent turns for this user.
            else:
                touched: set[int] = set()
                for op in ops_list.ops:
                    if op.op == OpType.NOOP:
                        continue
                    elif op.op == OpType.ADD:
                        memory = await self._apply_add_v2(
                            content=op.content,
                            category=op.category,
                            importance=op.importance if op.importance is not None else 0.5,
                            user_id=user_id,
                            session_id=session_id,
                        )
                        if memory is not None:
                            saved.append(memory)
                    elif op.op == OpType.UPDATE:
                        if op.target_id is not None and op.content and await self._apply_update_v2(
                            target_id=op.target_id,
                            content=op.content,
                            category=op.category,
                            importance=op.importance,
                            user_id=user_id,
                        ):
                            touched.add(op.target_id)
                    elif op.op == OpType.DELETE:
                        if op.target_id is not None and await self._apply_delete_v2(
                            target_id=op.target_id,
                            user_id=user_id,
                        ):
                            touched.add(op.target_id)

                # Bump last_accessed_at on retrieved-but-not-touched rows so the
                # recency-decay ranking (Lane C) reflects this turn's relevance.
                now = datetime.now(UTC).replace(tzinfo=None)
                untouched = candidate_ids_initial - touched
                for cid in untouched:
                    await self.db.execute(
                        update(ConversationMemory)
                        .where(ConversationMemory.id == cid)
                        .values(
                            last_accessed_at=now,
                            access_count=ConversationMemory.access_count + 1,
                        )
                    )

                await self.db.flush()  # surface FK / constraint errors before exit
        finally:
            await self._release_user_lock(user_id)

        if drift_reject:
            # Call v1 impl directly (see comment in the schema-reject branch above).
            return await self._extract_and_save_v1_impl(
                user_message=user_message,
                assistant_response=assistant_response,
                user_id=user_id,
                session_id=session_id,
                lang=lang,
            )

        return saved

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
        Retrieve relevant memories via cosine similarity — delegates to the
        circle-aware MemoryRetrieval module.
        """
        from services.memory_retrieval import MemoryRetrieval
        return await MemoryRetrieval(self.db).retrieve(
            message, user_id=user_id, limit=limit, threshold=threshold,
        )

    async def retrieve_essential(
        self,
        user_id: int | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """
        Retrieve high-importance memories regardless of query similarity —
        delegates to the circle-aware MemoryRetrieval module.
        """
        from services.memory_retrieval import MemoryRetrieval
        return await MemoryRetrieval(self.db).retrieve_essential(
            user_id=user_id, limit=limit,
        )

    # =========================================================================
    # Budget-aware retrieval for prompt injection
    # =========================================================================

    @staticmethod
    def _recency_score(
        created_at: datetime | None,
        half_life_days: float = 14.0,
    ) -> float:
        """Exponential decay score based on age. Returns 0.0-1.0."""
        if not created_at:
            return 0.5
        now = datetime.now(UTC).replace(tzinfo=None)
        age_days = max((now - created_at).total_seconds() / 86400, 0)
        return math.exp(-0.693 * age_days / half_life_days)

    async def retrieve_for_prompt(
        self,
        query: str,
        user_id: int | None = None,
        budget_chars: int | None = None,
    ) -> dict[str, list[dict]]:
        """
        Budget-aware memory retrieval organized by section.

        Lane C: always delegates to the extracted MemoryRetrieval module.
        The legacy scope/team-based path was retired with the circles v1
        rollout — circle_tier subsumes scope semantics, team_ids is parked
        for v2 named circles.

        Returns dict[section -> list[memory]] for essential / procedural /
        semantic / episodic, capped at `budget_chars` total.
        """
        from services.memory_retrieval import MemoryRetrieval
        return await MemoryRetrieval(self.db).retrieve_for_prompt(
            query, user_id=user_id, budget_chars=budget_chars,
        )

    # =========================================================================
    # Cleanup
    # =========================================================================

    async def cleanup(self) -> dict:
        """
        Deactivate expired and decayed memories.

        Returns counts of deactivated memories by reason.
        """
        now = datetime.now(UTC).replace(tzinfo=None)
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

        # 3. Confidence decay for unaccessed LLM-inferred memories
        confidence_cutoff = now - timedelta(days=30)
        result = await self.db.execute(
            update(ConversationMemory)
            .where(
                ConversationMemory.is_active == True,  # noqa: E712
                ConversationMemory.source == "llm_inferred",
                ConversationMemory.confidence > 0.3,
                ConversationMemory.last_accessed_at != None,  # noqa: E711
                ConversationMemory.last_accessed_at < confidence_cutoff,
            )
            .values(confidence=ConversationMemory.confidence * 0.95)
        )
        counts["confidence_decayed"] = result.rowcount

        # Also decay never-accessed llm_inferred memories older than 30 days
        result = await self.db.execute(
            update(ConversationMemory)
            .where(
                ConversationMemory.is_active == True,  # noqa: E712
                ConversationMemory.source == "llm_inferred",
                ConversationMemory.confidence > 0.3,
                ConversationMemory.last_accessed_at == None,  # noqa: E711
                ConversationMemory.created_at < confidence_cutoff,
            )
            .values(confidence=ConversationMemory.confidence * 0.95)
        )
        counts["confidence_decayed"] += result.rowcount

        # Deactivate memories with confidence below threshold
        result = await self.db.execute(
            update(ConversationMemory)
            .where(
                ConversationMemory.is_active == True,  # noqa: E712
                ConversationMemory.source == "llm_inferred",
                ConversationMemory.confidence <= 0.3,
            )
            .values(is_active=False)
        )
        counts["low_confidence_deactivated"] = result.rowcount

        await self.db.commit()

        total = sum(counts.values())
        if total > 0:
            logger.info(f"Memory cleanup: {counts}")

        # Update Prometheus metrics (best-effort)
        try:
            from utils.metrics import record_memory_cleanup, set_memory_total

            record_memory_cleanup(counts)
            active_count = await self.db.execute(
                select(func.count(ConversationMemory.id))
                .where(ConversationMemory.is_active == True)  # noqa: E712
            )
            set_memory_total(active_count.scalar() or 0)
        except Exception:
            pass  # Metrics should never break business logic

        return counts

    # =========================================================================
    # Delete / List
    # =========================================================================

    async def delete(
        self,
        memory_id: int,
        changed_by: str = MEMORY_CHANGED_BY_SYSTEM,
    ) -> bool:
        """Soft-delete a memory by setting is_active=False."""
        result = await self.db.execute(
            select(ConversationMemory).where(ConversationMemory.id == memory_id)
        )
        memory = result.scalar_one_or_none()
        if not memory:
            return False

        await self._record_history(
            memory_id=memory.id,
            action=MEMORY_ACTION_DELETED,
            old_content=memory.content,
            old_category=memory.category,
            old_importance=memory.importance,
            changed_by=changed_by,
        )

        memory.is_active = False
        await self.db.commit()
        return True

    async def delete_all_for_user(
        self,
        user_id: int,
        changed_by: str = "user",
    ) -> int:
        """Soft-delete ALL active memories for a user.

        Counts total first, then processes in batches of 100 via
        list_for_user + delete per item (with full audit history).
        """
        total = await self.get_count(user_id=user_id)
        if total == 0:
            return 0

        deleted = 0
        batch_size = 100
        for _ in range(0, total, batch_size):
            batch = await self.list_for_user(user_id, limit=batch_size)
            for m in batch:
                if await self.delete(m["id"], changed_by=changed_by):
                    deleted += 1

        logger.info(f"delete_all_for_user: {deleted}/{total} memories deleted for user_id={user_id}")
        return deleted

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
                "source": m.source,
                "confidence": m.confidence,
                "access_count": m.access_count,
                "created_at": m.created_at.isoformat() if m.created_at else None,
                "last_accessed_at": m.last_accessed_at.isoformat() if m.last_accessed_at else None,
                "subject_name": m.subject_name,
                "subject_entity_id": m.subject_entity_id,
            }
            for m in memories
        ]

    # =========================================================================
    # Update / Count
    # =========================================================================

    async def update(
        self,
        memory_id: int,
        content: str | None = None,
        category: str | None = None,
        importance: float | None = None,
        changed_by: str = "user",
    ) -> ConversationMemory | None:
        """Update a memory's content, category, or importance.

        Only updates fields that are not None. Returns the updated memory
        or None if not found.
        """
        result = await self.db.execute(
            select(ConversationMemory).where(
                ConversationMemory.id == memory_id,
                ConversationMemory.is_active == True,  # noqa: E712
            )
        )
        memory = result.scalar_one_or_none()
        if not memory:
            return None

        # Capture old values before modification
        old_content = memory.content
        old_category = memory.category
        old_importance = memory.importance

        if content is not None:
            memory.content = content
        if category is not None:
            if category not in MEMORY_CATEGORIES:
                logger.warning(f"Invalid memory category for update: {category}")
                return None
            memory.category = category
        if importance is not None:
            memory.importance = importance

        await self._record_history(
            memory_id=memory.id,
            action=MEMORY_ACTION_UPDATED,
            old_content=old_content,
            old_category=old_category,
            old_importance=old_importance,
            new_content=memory.content,
            new_category=memory.category,
            new_importance=memory.importance,
            changed_by=changed_by,
        )

        await self.db.commit()
        await self.db.refresh(memory)
        return memory

    async def get_count(
        self,
        user_id: int | None = None,
        category: str | None = None,
    ) -> int:
        """Count active memories with optional user and category filters."""
        query = select(func.count(ConversationMemory.id)).where(
            ConversationMemory.is_active == True,  # noqa: E712
        )
        if user_id is not None:
            query = query.where(ConversationMemory.user_id == user_id)
        if category:
            query = query.where(ConversationMemory.category == category)

        result = await self.db.execute(query)
        return result.scalar() or 0

    # =========================================================================
    # History
    # =========================================================================

    async def _record_history(
        self,
        memory_id: int,
        action: str,
        old_content: str | None = None,
        old_category: str | None = None,
        old_importance: float | None = None,
        new_content: str | None = None,
        new_category: str | None = None,
        new_importance: float | None = None,
        changed_by: str = MEMORY_CHANGED_BY_SYSTEM,
    ) -> None:
        """Record a history entry for a memory modification."""
        entry = MemoryHistory(
            memory_id=memory_id,
            action=action,
            old_content=old_content,
            old_category=old_category,
            old_importance=old_importance,
            new_content=new_content,
            new_category=new_category,
            new_importance=new_importance,
            changed_by=changed_by,
        )
        self.db.add(entry)

    async def get_history(self, memory_id: int, limit: int = 100) -> list[dict]:
        """Get modification history for a memory."""
        result = await self.db.execute(
            select(MemoryHistory)
            .where(MemoryHistory.memory_id == memory_id)
            .order_by(MemoryHistory.created_at.asc())
            .limit(limit)
        )
        entries = result.scalars().all()
        return [
            {
                "id": e.id,
                "memory_id": e.memory_id,
                "action": e.action,
                "old_content": e.old_content,
                "old_category": e.old_category,
                "old_importance": e.old_importance,
                "new_content": e.new_content,
                "new_category": e.new_category,
                "new_importance": e.new_importance,
                "changed_by": e.changed_by,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in entries
        ]

    # =========================================================================
    # Contradiction Resolution
    # =========================================================================

    async def _find_similar_memories(
        self,
        embedding: list[float],
        user_id: int | None,
    ) -> list[dict]:
        """Find memories in the contradiction similarity range (below dedup, above threshold).

        Returns memories with similarity in [contradiction_threshold, dedup_threshold).
        """
        lower = settings.memory_contradiction_threshold
        upper = settings.memory_dedup_threshold
        top_k = settings.memory_contradiction_top_k
        embedding_str = f"[{','.join(map(str, embedding))}]"

        user_filter = "AND user_id = :user_id" if user_id is not None else ""

        sql = text(f"""
            SELECT id, content, category, importance,
                   1 - (embedding <=> CAST(:embedding AS vector)) as similarity
            FROM conversation_memories
            WHERE is_active = true
              AND embedding IS NOT NULL
              {user_filter}
            ORDER BY embedding <=> CAST(:embedding AS vector)
            LIMIT :top_k
        """)

        params: dict = {"embedding": embedding_str, "top_k": top_k}
        if user_id is not None:
            params["user_id"] = user_id

        result = await self.db.execute(sql, params)
        rows = result.fetchall()

        similar = []
        for row in rows:
            sim = float(row.similarity) if row.similarity else 0
            if lower <= sim < upper:
                similar.append({
                    "id": row.id,
                    "content": row.content,
                    "category": row.category,
                    "importance": row.importance,
                    "similarity": round(sim, 3),
                })
        return similar

    async def _resolve_contradiction(
        self,
        new_fact: str,
        similar_memories: list[dict],
        lang: str,
    ) -> dict | None:
        """Call LLM to decide how a new fact relates to existing memories.

        Returns parsed resolution dict or None on failure.
        """
        from services.prompt_manager import prompt_manager

        # Format existing memories for the prompt
        mem_lines = []
        for m in similar_memories:
            mem_lines.append(
                f"- ID={m['id']}: \"{m['content']}\" "
                f"(category={m['category']}, similarity={m['similarity']})"
            )
        existing_str = "\n".join(mem_lines)

        prompt = prompt_manager.get(
            "memory", "contradiction_resolution_prompt", lang=lang,
            new_fact=new_fact,
            existing_memories=existing_str,
        )
        system_msg = prompt_manager.get(
            "memory", "contradiction_resolution_system", lang=lang,
        )
        llm_options = prompt_manager.get_config("memory", "contradiction_llm_options") or {}

        from utils.llm_client import extract_response_content, get_classification_chat_kwargs

        try:
            client = await self._get_chat_client()
            extraction_model = settings.memory_extraction_model or settings.ollama_model
            response = await client.chat(
                model=extraction_model,
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": prompt},
                ],
                options=llm_options,
                **get_classification_chat_kwargs(extraction_model),
            )
            raw_text = extract_response_content(response)
        except Exception as e:
            logger.warning(f"Contradiction resolution LLM call failed: {e}")
            return None

        return self._parse_resolution_response(raw_text, similar_memories)

    @staticmethod
    def _parse_resolution_response(
        raw_text: str,
        similar_memories: list[dict],
    ) -> dict | None:
        """Parse the LLM's contradiction resolution response.

        Validates action and target_memory_id against known memories.
        Returns dict with {action, target_memory_id, updated_content, reason} or None.
        """
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
        except (json.JSONDecodeError, TypeError):
            logger.debug(f"Contradiction resolution: could not parse JSON from: {raw_text[:200]}")
            return None

        if not isinstance(data, dict):
            return None

        action = data.get("action", "").upper()
        valid_actions = {"ADD", "UPDATE", "DELETE", "NOOP"}
        if action not in valid_actions:
            logger.debug(f"Contradiction resolution: invalid action '{action}'")
            return None

        target_id = data.get("target_memory_id")
        valid_ids = {m["id"] for m in similar_memories}

        # Validate target_memory_id for UPDATE/DELETE
        if action in ("UPDATE", "DELETE"):
            if target_id is None or target_id not in valid_ids:
                logger.debug(
                    f"Contradiction resolution: invalid target_memory_id "
                    f"{target_id} (valid: {valid_ids})"
                )
                return None

        return {
            "action": action,
            "target_memory_id": target_id,
            "updated_content": data.get("updated_content"),
            "reason": data.get("reason", ""),
        }

    async def _apply_contradiction_resolution(
        self,
        content: str,
        category: str,
        importance: float,
        user_id: int | None,
        session_id: str | None,
        lang: str,
    ) -> ConversationMemory | None:
        """Orchestrate full contradiction resolution for a single extracted fact.

        1. Generate embedding
        2. Check for exact duplicate (fast path, >= dedup threshold)
        3. Search for similar memories (contradiction range)
        4. If found -> call LLM for resolution
        5. Execute decision (ADD/UPDATE/DELETE/NOOP)
        6. All failures fall back to ADD
        """
        # Generate embedding
        embedding = None
        try:
            embedding = await self._get_embedding(content)
        except Exception as e:
            logger.warning(f"Contradiction resolution: embedding failed: {e}")

        # Fast path: exact duplicate check
        if embedding:
            duplicate = await self._find_duplicate(embedding, user_id)
            if duplicate:
                duplicate.access_count = (duplicate.access_count or 0) + 1
                duplicate.last_accessed_at = datetime.now(UTC).replace(tzinfo=None)
                await self.db.commit()
                await self.db.refresh(duplicate)
                logger.debug(f"Contradiction resolution: deduplicated (id={duplicate.id})")
                return duplicate

        # Search for similar memories in contradiction range
        similar = []
        if embedding:
            try:
                similar = await self._find_similar_memories(embedding, user_id)
            except Exception as e:
                logger.warning(f"Contradiction resolution: similar search failed: {e}")

        # No similar memories -> just save (ADD)
        if not similar:
            return await self.save(
                content=content,
                category=category,
                user_id=user_id,
                importance=importance,
                source_session_id=session_id,
            )

        # Call LLM for resolution
        resolution = await self._resolve_contradiction(content, similar, lang)

        if not resolution:
            # LLM failed -> fall back to ADD
            logger.debug("Contradiction resolution: LLM failed, falling back to ADD")
            return await self.save(
                content=content,
                category=category,
                user_id=user_id,
                importance=importance,
                source_session_id=session_id,
            )

        action = resolution["action"]
        target_id = resolution.get("target_memory_id")
        updated_content = resolution.get("updated_content")
        reason = resolution.get("reason", "")

        if action == "NOOP":
            logger.info(f"Contradiction resolution: NOOP — {reason}")
            return None

        if action == "ADD":
            logger.info(f"Contradiction resolution: ADD — {reason}")
            return await self.save(
                content=content,
                category=category,
                user_id=user_id,
                importance=importance,
                source_session_id=session_id,
            )

        if action == "UPDATE" and target_id is not None:
            new_content = updated_content or content
            logger.info(f"Contradiction resolution: UPDATE id={target_id} — {reason}")

            # Re-embed the updated content
            new_embedding = None
            try:
                new_embedding = await self._get_embedding(new_content)
            except Exception:
                pass

            # Update the target memory
            result = await self.db.execute(
                select(ConversationMemory).where(
                    ConversationMemory.id == target_id,
                    ConversationMemory.is_active == True,  # noqa: E712
                )
            )
            target = result.scalar_one_or_none()
            if target:
                old_content = target.content
                old_category = target.category
                old_importance = target.importance
                target.content = new_content
                if new_embedding:
                    target.embedding = new_embedding

                await self._record_history(
                    memory_id=target.id,
                    action=MEMORY_ACTION_UPDATED,
                    old_content=old_content,
                    old_category=old_category,
                    old_importance=old_importance,
                    new_content=target.content,
                    new_category=target.category,
                    new_importance=target.importance,
                    changed_by=MEMORY_CHANGED_BY_RESOLUTION,
                )
                await self.db.commit()
                await self.db.refresh(target)
                return target

            # Target not found -> fall back to ADD
            return await self.save(
                content=content,
                category=category,
                user_id=user_id,
                importance=importance,
                source_session_id=session_id,
            )

        if action == "DELETE" and target_id is not None:
            logger.info(f"Contradiction resolution: DELETE id={target_id} — {reason}")
            await self.delete(target_id, changed_by=MEMORY_CHANGED_BY_RESOLUTION)
            # Save the new fact
            return await self.save(
                content=content,
                category=category,
                user_id=user_id,
                importance=importance,
                source_session_id=session_id,
            )

        # Shouldn't get here, but fall back to ADD
        return await self.save(
            content=content,
            category=category,
            user_id=user_id,
            importance=importance,
            source_session_id=session_id,
        )

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
            SELECT id, content, category, importance, access_count,
                   last_accessed_at, is_active, user_id, source_session_id,
                   source_message_id, expires_at, created_at, embedding,
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
            # Merge into session as ORM object (avoids second query)
            return await self.db.get(ConversationMemory, row.id)

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
