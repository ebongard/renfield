"""
Conversation Service - Manages conversation persistence

Extracted from OllamaService for better separation of concerns.
Handles all database operations for conversations and messages.
"""
import re
from datetime import UTC, datetime
from typing import Any

from loguru import logger
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from models.database import Conversation, Message
from services.fts_languages import build_tsquery_union_sql

# Drop short tokens (≤2 chars) so single-letter / punctuation noise never
# reaches the tsquery. Word characters only (German alphabet + digits), so the
# joined " OR " string is safe to hand to websearch_to_tsquery verbatim (no
# metachars can survive — same security argument as lexical_retrieval).
_TOKEN_RE = re.compile(r"[A-Za-zÄÖÜäöüß0-9]{2,}")

# Snippet highlight sentinels: non-printable control chars (STX / ETX) that
# cannot occur in normal chat text. ts_headline wraps matched terms in these
# instead of HTML, so the frontend can split + render the highlight with a real
# React element — no HTML injection, no sanitizer dependency. Kept in sync with
# the HL_START / HL_END constants in components/chat/ChatMessageSearch.tsx.
_HL_START = chr(0x02)
_HL_END = chr(0x03)


def _significant_message_tokens(query: str) -> list[str]:
    """Tokenize a message-search query into FTS-safe tokens.

    Returns word-character tokens of length ≥2 — short enough to keep
    proper-noun prefixes, no metacharacters so the joined OR string is safe
    for ``websearch_to_tsquery``. Empty list ⇒ caller short-circuits to no
    results.
    """
    return _TOKEN_RE.findall(query or "")


class ConversationService:
    """
    Service für Konversations-Persistenz.

    Bietet:
    - Konversations-Kontext laden
    - Nachrichten speichern
    - Konversations-Management (Liste, Suche, Löschen)
    """

    def __init__(self, db: AsyncSession):
        """
        Initialisiert den Conversation Service.

        Args:
            db: AsyncSession für Datenbankoperationen
        """
        self.db = db

    async def active_path_message_ids(self, conversation: Conversation) -> list[int]:
        """Return the ids of the messages on the conversation's ACTIVE branch,
        ordered ``timestamp ASC, id ASC`` (the exact order
        ``/api/chat/history`` returns).

        Chat branching (Phase 1), the CORE primitive. The active branch is the
        recursive walk of ``messages.parent_message_id`` UPWARD from
        ``conversation.active_leaf_message_id``. For a linear (un-forked,
        backfilled) conversation this reproduces the historical flat ordering
        byte-for-byte, so it is safe to use everywhere "the messages on the
        active branch" is needed regardless of the CHAT_BRANCHING_ENABLED flag
        (CTE-always-on).

        ``active_leaf_message_id IS NULL`` (empty conversation, or a row that
        predates the backfill) → empty list, and callers fall back to their
        flat query (see ``load_context`` / the history route). Postgres-only
        recursive CTE; on a non-PG dialect (sqlite test harness) returns ``[]``
        so callers transparently keep the flat path.

        RISK NOTE (reviewer): this is a recursive CTE. ``parent_message_id`` is
        a strict-ancestor pointer maintained by ``save_message`` / the fork
        path, so the walk terminates at the root (NULL parent). A pathological
        cycle (should be impossible — we never set a parent to a descendant)
        would loop; Postgres caps recursion via ``max_stack_depth`` and we also
        bound the depth defensively below.
        """
        leaf_id = getattr(conversation, "active_leaf_message_id", None)
        if leaf_id is None:
            return []
        if not self._is_postgres():
            return []
        # Walk parent_message_id upward from the leaf. The CYCLE guard +
        # depth bound make this terminate even on corrupt data. Order the
        # final result by (timestamp, id) so a fork that reuses an earlier
        # timestamp still lands in chronological position (matches history).
        #
        # SECURITY (defense in depth): carry conversation_id through the CTE and
        # require the recursive step to stay WITHIN the seed conversation
        # (``p.conversation_id = b.conversation_id``). Scoping only the seed row
        # guards the first hop; a stray cross-conversation parent pointer
        # (however it got set) could otherwise make the walk climb into another
        # conversation. This clause makes that impossible.
        sql = text("""
            WITH RECURSIVE branch(id, parent_message_id, conversation_id, timestamp, depth) AS (
                SELECT m.id, m.parent_message_id, m.conversation_id, m.timestamp, 0
                FROM messages m
                WHERE m.id = :leaf_id
                  AND m.conversation_id = :conv_id
                UNION ALL
                SELECT p.id, p.parent_message_id, p.conversation_id, p.timestamp, b.depth + 1
                FROM messages p
                JOIN branch b ON p.id = b.parent_message_id
                WHERE b.depth < 10000
                  AND p.conversation_id = b.conversation_id
            )
            SELECT id FROM branch
            ORDER BY timestamp ASC, id ASC
        """)
        rows = await self.db.execute(
            sql, {"leaf_id": leaf_id, "conv_id": conversation.id}
        )
        return [int(r[0]) for r in rows.all()]

    async def _abandoned_subtree_message_ids(self, root_message_id: int) -> list[int]:
        """Return ``root_message_id`` + ALL its descendants via a DOWNWARD
        recursive CTE over ``parent_message_id``.

        Used by the fork path to collect the messages abandoned when a branch
        is replaced, so their derived state (memories, seam 3) can be
        deactivated. Postgres-only; ``[]`` on other dialects.

        RISK NOTE (reviewer): recursive CTE walking children. Same termination
        argument + depth bound as ``active_path_message_ids``.
        """
        if not self._is_postgres():
            return []
        # SECURITY (defense in depth): carry conversation_id through the CTE and
        # require each descendant to share the root's conversation
        # (``c.conversation_id = s.conversation_id``). A stray
        # cross-conversation child pointer could otherwise pull a foreign
        # message into the abandoned set (and thus deactivate a foreign
        # conversation's memories). This clause confines the walk to the root's
        # own conversation.
        sql = text("""
            WITH RECURSIVE subtree(id, conversation_id, depth) AS (
                SELECT m.id, m.conversation_id, 0 FROM messages m WHERE m.id = :root_id
                UNION ALL
                SELECT c.id, c.conversation_id, s.depth + 1
                FROM messages c
                JOIN subtree s ON c.parent_message_id = s.id
                WHERE s.depth < 10000
                  AND c.conversation_id = s.conversation_id
            )
            SELECT id FROM subtree
        """)
        rows = await self.db.execute(sql, {"root_id": root_message_id})
        return [int(r[0]) for r in rows.all()]

    async def _deepest_leaf_message_id(self, message_id: int) -> int:
        """Resolve ``message_id``'s subtree to its TIP — the descendant with the
        greatest ``(timestamp, id)`` (chat branching Phase 2, the switcher).

        Switching the active branch to a sibling means making that sibling's
        whole continuation active, so the active leaf must move to the tip of
        the sibling's subtree, not the sibling row itself. If the subtree has
        its own forks, the most recent message wins (deterministic). A message
        with no descendants resolves to itself. Postgres-only walk; on sqlite
        (test harness) returns ``message_id`` unchanged.
        """
        if not self._is_postgres():
            return message_id
        sql = text("""
            WITH RECURSIVE subtree(id, conversation_id, timestamp, depth) AS (
                SELECT m.id, m.conversation_id, m.timestamp, 0
                FROM messages m WHERE m.id = :root_id
                UNION ALL
                SELECT c.id, c.conversation_id, c.timestamp, s.depth + 1
                FROM messages c
                JOIN subtree s ON c.parent_message_id = s.id
                WHERE s.depth < 10000
                  AND c.conversation_id = s.conversation_id
            )
            SELECT id FROM subtree
            ORDER BY timestamp DESC, id DESC
            LIMIT 1
        """)
        row = (await self.db.execute(sql, {"root_id": message_id})).first()
        return int(row[0]) if row else message_id

    async def recompute_memory_activation(self, conversation: Conversation) -> int:
        """Symmetric memory-activation recompute (chat branching Phase 2).

        Sets ``is_active = (source_message_id ∈ active_path)`` for EVERY
        ``ConversationMemory`` whose source message belongs to this
        conversation. This SUBSUMES Phase-1's one-way
        ``deactivate_memories_for_abandoned_subtree`` and adds the missing
        half — **reactivation** when a branch is switched back — in one
        idempotent, deterministic pass keyed on the conversation's current
        active leaf.

        Because truth is re-derived from the active leaf on every call, this
        also closes the Phase-1 deactivate-at-fork RACE: a background extraction
        that wrongly (re)activated an abandoned-branch memory is corrected by the
        next fork/switch recompute. (The extraction write is additionally
        guarded to only set ``is_active=True`` for a source still on the active
        path — defense in depth.)

        Returns the number of rows whose ``is_active`` flipped. No internal
        commit — the caller commits as part of its transaction. Postgres-only
        (the active-path CTE is PG-only); a NULL leaf / sqlite / empty path is a
        safe no-op (it must NEVER deactivate everything just because the path
        couldn't be computed).
        """
        from sqlalchemy import update

        from models.database import ConversationMemory

        active_ids = await self.active_path_message_ids(conversation)
        if not active_ids:
            # NULL leaf, pre-backfill, sqlite, or empty conversation → nothing
            # to recompute. Crucially NOT "deactivate all" — an uncomputable
            # path must leave the is_active flags untouched.
            return 0
        active_set = set(active_ids)

        rows = (
            await self.db.execute(
                select(
                    ConversationMemory.id,
                    ConversationMemory.source_message_id,
                    ConversationMemory.is_active,
                )
                .join(Message, ConversationMemory.source_message_id == Message.id)
                .where(Message.conversation_id == conversation.id)
            )
        ).all()

        to_activate = [
            r.id for r in rows if r.source_message_id in active_set and not r.is_active
        ]
        to_deactivate = [
            r.id for r in rows if r.source_message_id not in active_set and r.is_active
        ]

        if to_activate:
            await self.db.execute(
                update(ConversationMemory)
                .where(ConversationMemory.id.in_(to_activate))
                .values(is_active=True)
            )
        if to_deactivate:
            await self.db.execute(
                update(ConversationMemory)
                .where(ConversationMemory.id.in_(to_deactivate))
                .values(is_active=False)
            )

        changed = len(to_activate) + len(to_deactivate)
        if changed:
            logger.info(
                "🌿 Branch recompute: %d memory(s) reactivated, %d deactivated "
                "(conversation %s, active path %d msgs)",
                len(to_activate), len(to_deactivate), conversation.id, len(active_ids),
            )
        return changed

    async def branch_metadata(
        self, conversation: Conversation, active_ids: list[int]
    ) -> dict[int, dict]:
        """Per active-path message, the sibling-branch info for the Phase-2
        ``‹ n/m ›`` switcher: ``{message_id: {index, count, sibling_ids}}``.

        A message has siblings when ≥2 messages share its ``parent_message_id``
        (NULL-parent roots are grouped together as conversation-level siblings).
        Messages with no siblings are omitted — the frontend renders a switcher
        only for the keys present. ``sibling_ids`` is ordered ``(timestamp, id)``
        so ◂/▸ navigation is stable, and ``index`` is this message's position in
        that order. Dialect-independent (no CTE); ``active_ids == []`` (sqlite /
        null leaf) yields ``{}``.
        """
        rows = (
            await self.db.execute(
                select(Message.id, Message.parent_message_id)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.timestamp.asc(), Message.id.asc())
            )
        ).all()
        children: dict[int | None, list[int]] = {}
        parent_of: dict[int, int | None] = {}
        for mid, pid in rows:
            children.setdefault(pid, []).append(mid)
            parent_of[mid] = pid

        out: dict[int, dict] = {}
        for mid in active_ids:
            sibs = children.get(parent_of.get(mid))
            if sibs and len(sibs) > 1:
                out[mid] = {
                    "index": sibs.index(mid),
                    "count": len(sibs),
                    "sibling_ids": sibs,
                }
        return out

    async def load_context(
        self,
        session_id: str,
        max_messages: int = 20
    ) -> list[dict[str, str]]:
        """
        Lade Konversationskontext aus der Datenbank.

        Args:
            session_id: Session ID der Konversation
            max_messages: Maximale Anzahl zu ladender Nachrichten

        Returns:
            Liste von Nachrichten im Format [{"role": "user|assistant", "content": "..."}]
        """
        try:
            # Finde Conversation
            result = await self.db.execute(
                select(Conversation).where(Conversation.session_id == session_id)
            )
            conversation = result.scalar_one_or_none()

            if not conversation:
                logger.debug(f"Keine Konversation gefunden für session_id: {session_id}")
                return []

            # Chat branching (Phase 1): the agent's conversation_history must
            # follow the ACTIVE branch, not the flat message set (else an edited
            # turn would still feed the abandoned sibling to the LLM). Resolve the
            # active-path ids (CTE-always-on) and load the last N of THOSE. Empty
            # path (null leaf / pre-backfill / sqlite) → fall back to the flat
            # query below, byte-identical to pre-branching.
            active_ids = await self.active_path_message_ids(conversation)
            if active_ids:
                window_ids = active_ids[-max_messages:]
                result = await self.db.execute(
                    select(Message).where(Message.id.in_(window_ids))
                )
                by_id = {m.id: m for m in result.scalars().all()}
                # Preserve the active-path order (ascending); reversed() below
                # expects newest-first, so hand it the descending slice.
                messages = [by_id[i] for i in reversed(window_ids) if i in by_id]
            else:
                # Lade letzte N Nachrichten
                result = await self.db.execute(
                    select(Message)
                    .where(Message.conversation_id == conversation.id)
                    .order_by(Message.timestamp.desc())
                    .limit(max_messages)
                )
                messages = result.scalars().all()

            # Konvertiere zu Chat-Format (älteste zuerst)
            # Reconstruct action summary prefix for LLM context (kept out of DB content for clean UI)
            context = []
            for msg in reversed(messages):
                content = msg.content
                if (msg.role == "assistant"
                        and msg.message_metadata
                        and msg.message_metadata.get("action_summary")):
                    summary = msg.message_metadata["action_summary"]
                    content = (
                        f"[Aktionsergebnis — Verwende diese Daten für "
                        f"Folgeanfragen (IDs, Titel, etc.):\n{summary}]\n\n{content}"
                    )
                context.append({
                    "role": msg.role,
                    "content": content,
                    "metadata": msg.message_metadata,
                })

            logger.info(f"Geladen: {len(context)} Nachrichten für Session {session_id}")
            return context

        except Exception as e:
            logger.error(f"Fehler beim Laden des Kontexts: {e}")
            return []

    async def save_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: dict | None = None,
        user_id: int | None = None,
        parent_message_id: int | None = None,
    ) -> Message:
        """
        Speichere eine einzelne Nachricht.

        Args:
            session_id: Session ID der Konversation
            role: "user" oder "assistant"
            content: Nachrichteninhalt
            metadata: Optional zusätzliche Metadaten
            parent_message_id: Chat branching (Phase 1). When given (a FORK), the
                new message is inserted as a SIBLING under that parent — preserving
                the message it replaces. When ``None`` (the normal append), the new
                message is chained onto the current tip
                (``conversation.active_leaf_message_id``). Either way the leaf is
                advanced to the new message id, so the tree stays walkable for
                EVERY conversation regardless of CHAT_BRANCHING_ENABLED
                (CTE-always-on).

        Returns:
            Gespeicherte Message
        """
        try:
            # Let plugins modify content/metadata before saving
            from utils.hooks import run_hooks
            hook_results = await run_hooks(
                "pre_save_message",
                role=role, content=content,
                metadata=metadata or {}, session_id=session_id,
            )
            if hook_results:
                result = hook_results[0]
                content = result.get("content", content)
                metadata = result.get("metadata", metadata)

            # Finde oder erstelle Conversation
            result = await self.db.execute(
                select(Conversation).where(Conversation.session_id == session_id)
            )
            conversation = result.scalar_one_or_none()

            if not conversation:
                conversation = Conversation(session_id=session_id, user_id=user_id)
                self.db.add(conversation)
                # Flush so ``conversation.id`` is populated before it's
                # used as ``Message.conversation_id`` below — otherwise
                # the first message of every brand-new session gets
                # saved with ``conversation_id=NULL`` and becomes
                # orphaned (invisible to any JOIN-based history or
                # message-count query).
                await self.db.flush()
            elif user_id and conversation.user_id is None:
                conversation.user_id = user_id
                await self.db.flush()

            # Chat branching (Phase 1): wire the message into the conversation
            # tree. Explicit parent (a fork) → sibling under that parent; else
            # chain onto the current tip. Then advance the active leaf to this
            # new message so the next append/CTE walk starts here.
            effective_parent = (
                parent_message_id
                if parent_message_id is not None
                else conversation.active_leaf_message_id
            )

            # Erstelle Message
            message = Message(
                conversation_id=conversation.id,
                role=role,
                content=content,
                message_metadata=metadata,
                parent_message_id=effective_parent,
            )
            self.db.add(message)
            # Flush so message.id is available to set as the new active leaf.
            await self.db.flush()
            conversation.active_leaf_message_id = message.id

            # Update conversation timestamp
            conversation.updated_at = datetime.now(UTC).replace(tzinfo=None)

            await self.db.commit()
            await self.db.refresh(message)

            logger.debug(f"Nachricht gespeichert: {role} - {content[:50]}...")
            return message

        except Exception as e:
            logger.error(f"Fehler beim Speichern der Nachricht: {e}")
            await self.db.rollback()
            raise

    async def deactivate_memories_for_abandoned_subtree(
        self, abandoned_root_message_id: int
    ) -> int:
        """Deactivate-at-fork (chat branching, seam 3).

        When a fork ABANDONS a branch, flip ``is_active=False`` on every
        ``ConversationMemory`` whose ``source_message_id`` is in the abandoned
        subtree — the message being replaced PLUS all its descendants (collected
        via the downward recursive CTE). ``retrieve_for_prompt`` already filters
        ``is_active == True``, so the abandoned turn's facts stop being injected
        without deleting anything (recoverable, mirrors the soft-delete posture
        in ``conversation_memory_service._apply_delete_v2``).

        Returns the number of rows deactivated. No internal commit — the caller
        commits as part of the fork transaction.

        NARROW RACE (documented, NOT solved in Phase 1): memory extraction runs
        in a BACKGROUND task AFTER the turn's ``done`` frame. A fork that lands
        WHILE the abandoned turn's extraction is still running could re-add an
        ``is_active=True`` memory after this deactivation pass. The window is
        small (extraction is fast and a user can't realistically edit a turn
        before its own extraction completes) and the failure mode is benign (one
        stale memory survives, not data loss). PHASE 2 will need either
        reactivation-on-branch-switch or active-path-scoped memory retrieval
        (so an inactive-branch memory is filtered at read time regardless of the
        flag) — that is the proper fix; do NOT paper over it here.

        Sqlite test harness: the subtree CTE returns ``[]`` (PG-only), so this
        is a no-op there; tests seed + assert the deactivation against PG.
        """
        from sqlalchemy import update

        from models.database import ConversationMemory

        subtree_ids = await self._abandoned_subtree_message_ids(
            abandoned_root_message_id
        )
        if not subtree_ids:
            return 0
        result = await self.db.execute(
            update(ConversationMemory)
            .where(ConversationMemory.source_message_id.in_(subtree_ids))
            .where(ConversationMemory.is_active.is_(True))
            .values(is_active=False)
        )
        count = result.rowcount or 0
        if count:
            logger.info(
                f"🌿 Fork: deactivated {count} memories from abandoned subtree "
                f"(root msg {abandoned_root_message_id}, {len(subtree_ids)} msgs)"
            )
        return count

    async def set_active_leaf(
        self, session_id: str, message_id: int, *, user_id: int | None = None
    ) -> bool:
        """Switch a conversation's active branch to the one passing through
        ``message_id`` (chat branching active-leaf endpoint; the Phase-2 `‹n/m›`
        switcher targets the sibling at the fork point).

        Validates that ``message_id`` belongs to the conversation (and, when
        ``user_id`` is given, that the caller owns the conversation), then
        repoints ``active_leaf_message_id`` to the **tip** of ``message_id``'s
        subtree (`_deepest_leaf_message_id`) so the whole continuation of the
        chosen branch becomes active — not just the fork-point row. A message
        that is already a leaf resolves to itself, so Phase-1's
        edit/regenerate (which targets the freshly-saved leaf) is unchanged.

        After repointing, **recompute memory activation** for the new branch so
        memories on the now-active path are reactivated and off-path ones
        deactivated. Returns False (caller → 404) when the conversation is
        missing, unowned, or the message is foreign. Commits the change.
        """
        result = await self.db.execute(
            select(Conversation).where(Conversation.session_id == session_id)
        )
        conversation = result.scalar_one_or_none()
        if not conversation:
            return False
        if user_id is not None and conversation.user_id != user_id:
            return False

        msg_result = await self.db.execute(
            select(Message.id).where(
                Message.id == message_id,
                Message.conversation_id == conversation.id,
            )
        )
        if msg_result.scalar_one_or_none() is None:
            return False

        leaf_id = await self._deepest_leaf_message_id(message_id)
        conversation.active_leaf_message_id = leaf_id
        conversation.updated_at = datetime.now(UTC).replace(tzinfo=None)
        # Reactivate/deactivate memories to match the newly-active branch.
        await self.recompute_memory_activation(conversation)
        await self.db.commit()
        return True

    async def delete_branch(
        self, session_id: str, message_id: int, *, user_id: int | None = None
    ) -> str:
        """Delete a branch — ``message_id`` plus its whole subtree (chat
        branching Phase 2, delete-branch).

        Returns a status string the route maps to HTTP:
        - ``"not_found"`` (→ 404): conversation missing/unowned, or the message
          is foreign to the conversation.
        - ``"active"`` (→ 409): ``message_id`` is ON the current active path —
          refused. The user must switch to a different branch before deleting
          the one they are viewing, so the active leaf can never be orphaned.
        - ``"ok"`` (→ 200): the message and all descendants are deleted.

        Two FK hazards are handled before the message delete (both
        ``messages.id`` references lack ``ON DELETE`` rules): branch-local
        ``conversation_memories`` (``source_message_id`` in the subtree) are
        **deleted** with the branch (they describe a turn that no longer
        exists), while ``kg_relations`` provenance is **detached**
        (``source_message_id`` → NULL) rather than deleting canonical graph
        data. Messages are then removed by explicit subtree-id list (dialect
        independent — does not rely on the self-FK CASCADE).
        """
        from sqlalchemy import delete as sa_delete
        from sqlalchemy import update

        from models.database import ConversationMemory, KGRelation

        result = await self.db.execute(
            select(Conversation).where(Conversation.session_id == session_id)
        )
        conversation = result.scalar_one_or_none()
        if not conversation:
            return "not_found"
        if user_id is not None and conversation.user_id != user_id:
            return "not_found"

        msg_result = await self.db.execute(
            select(Message.id).where(
                Message.id == message_id,
                Message.conversation_id == conversation.id,
            )
        )
        if msg_result.scalar_one_or_none() is None:
            return "not_found"

        # Refuse deleting any message on the active path (PG; on sqlite the path
        # is [] so the guard is inert and tests target PG for this property).
        active_ids = await self.active_path_message_ids(conversation)
        if message_id in set(active_ids):
            return "active"

        subtree_ids = await self._abandoned_subtree_message_ids(message_id)
        if not subtree_ids:
            # sqlite / no-CTE: fall back to the single row so the delete still
            # works in the test harness.
            subtree_ids = [message_id]

        # 1) Branch-local memories go with the branch.
        await self.db.execute(
            sa_delete(ConversationMemory).where(
                ConversationMemory.source_message_id.in_(subtree_ids)
            )
        )
        # 2) Detach KG-relation provenance (keep canonical graph data).
        await self.db.execute(
            update(KGRelation)
            .where(KGRelation.source_message_id.in_(subtree_ids))
            .values(source_message_id=None)
        )
        # 3) Delete the subtree messages explicitly.
        await self.db.execute(
            sa_delete(Message).where(Message.id.in_(subtree_ids))
        )
        conversation.updated_at = datetime.now(UTC).replace(tzinfo=None)
        await self.db.commit()
        logger.info(
            "🌿 Branch deleted: msg %s + %d descendant(s) in conversation %s",
            message_id, len(subtree_ids) - 1, conversation.id,
        )
        return "ok"

    async def associate_speaker(
        self,
        session_id: str,
        speaker_id: int,
        user_id: int | None = None,
    ) -> None:
        """Associate a conversation with a speaker and optionally a user.

        speaker_id: Always set (from speaker recognition, works without auth).
        user_id: Set when Speaker→User mapping exists (requires auth + linked user).
        Idempotent: only sets fields that are currently NULL.
        """
        try:
            result = await self.db.execute(
                select(Conversation).where(Conversation.session_id == session_id)
            )
            conv = result.scalar_one_or_none()
            if not conv:
                return

            changed = False
            if conv.speaker_id is None:
                conv.speaker_id = speaker_id
                changed = True
            if user_id and conv.user_id is None:
                conv.user_id = user_id
                changed = True

            if changed:
                await self.db.commit()
                logger.debug(f"Conversation {session_id} associated: speaker={speaker_id}, user={user_id}")
        except Exception as e:
            logger.warning(f"Failed to associate speaker with conversation: {e}")
            await self.db.rollback()

    async def save_context_vars(
        self,
        session_id: str,
        vars_dict: dict,
    ) -> None:
        """Merge-update conversation context variables (pinned state).

        Existing keys are preserved; new keys are added; keys set to None are removed.
        """
        try:
            result = await self.db.execute(
                select(Conversation).where(Conversation.session_id == session_id)
            )
            conversation = result.scalar_one_or_none()
            if not conversation:
                return

            current = dict(conversation.context_vars or {})
            for k, v in vars_dict.items():
                if v is None:
                    current.pop(k, None)
                else:
                    current[k] = v
            conversation.context_vars = current
            await self.db.commit()
            logger.debug(f"Context vars updated for {session_id}: {list(current.keys())}")
        except Exception as e:
            logger.warning(f"Failed to save context vars: {e}")
            await self.db.rollback()

    async def load_context_vars(self, session_id: str) -> dict:
        """Load conversation context variables. Returns empty dict if none."""
        try:
            result = await self.db.execute(
                select(Conversation.context_vars).where(
                    Conversation.session_id == session_id
                )
            )
            row = result.scalar_one_or_none()
            return dict(row) if row else {}
        except Exception as e:
            logger.warning(f"Failed to load context vars: {e}")
            return {}

    async def update_summary(
        self,
        session_id: str,
        llm_client,
        model: str,
        threshold: int = 10,
        keep_recent: int = 4,
    ) -> str | None:
        """Generate an LLM summary of older messages when conversation grows.

        Triggers only when message count exceeds *threshold*. Summarizes all
        messages except the most recent *keep_recent* ones. Stores the result
        in ``conversations.summary``.

        Returns the summary text, or None if not triggered / failed.
        """
        try:
            result = await self.db.execute(
                select(Conversation).where(Conversation.session_id == session_id)
            )
            conversation = result.scalar_one_or_none()
            if not conversation:
                return None

            # Count messages
            result = await self.db.execute(
                select(func.count(Message.id)).where(
                    Message.conversation_id == conversation.id
                )
            )
            msg_count = result.scalar() or 0
            if msg_count < threshold:
                return conversation.summary  # return existing summary if any

            # Load oldest messages (all except keep_recent)
            result = await self.db.execute(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.timestamp.asc())
                .limit(msg_count - keep_recent)
            )
            old_messages = result.scalars().all()
            if not old_messages:
                return conversation.summary

            # Build text to summarize
            lines = []
            for msg in old_messages:
                role = "User" if msg.role == "user" else "Assistant"
                content = msg.content[:300] if msg.content else ""
                lines.append(f"{role}: {content}")
            conversation_text = "\n".join(lines)

            # LLM summarization
            from utils.llm_client import extract_response_content
            response = await llm_client.chat(
                model=model,
                messages=[
                    {"role": "system", "content": (
                        "Summarize this conversation in 3-5 sentences. "
                        "Preserve key decisions, entity names/IDs, and open questions. "
                        "Write in the same language as the conversation."
                    )},
                    {"role": "user", "content": conversation_text},
                ],
            )
            summary = extract_response_content(response)

            conversation.summary = summary
            await self.db.commit()
            logger.info(f"Conversation summary updated for {session_id} ({len(summary)} chars)")
            return summary

        except Exception as e:
            logger.warning(f"Failed to update summary: {e}")
            await self.db.rollback()
            return None

    async def load_summary(self, session_id: str) -> str | None:
        """Load the conversation summary. Returns None if none exists."""
        try:
            result = await self.db.execute(
                select(Conversation.summary).where(
                    Conversation.session_id == session_id
                )
            )
            return result.scalar_one_or_none()
        except Exception as e:
            logger.warning(f"Failed to load summary: {e}")
            return None

    async def get_summary(
        self,
        session_id: str
    ) -> dict | None:
        """
        Hole Zusammenfassung einer Konversation.

        Args:
            session_id: Session ID der Konversation

        Returns:
            Dict mit session_id, created_at, updated_at, message_count, first_message, last_message
        """
        try:
            result = await self.db.execute(
                select(Conversation).where(Conversation.session_id == session_id)
            )
            conversation = result.scalar_one_or_none()

            if not conversation:
                return None

            # Zähle Nachrichten
            result = await self.db.execute(
                select(func.count(Message.id))
                .where(Message.conversation_id == conversation.id)
            )
            message_count = result.scalar()

            # Hole erste und letzte Nachricht
            result = await self.db.execute(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.timestamp.asc())
                .limit(1)
            )
            first_message = result.scalar_one_or_none()

            result = await self.db.execute(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.timestamp.desc())
                .limit(1)
            )
            last_message = result.scalar_one_or_none()

            return {
                "session_id": session_id,
                "created_at": conversation.created_at.isoformat(),
                "updated_at": conversation.updated_at.isoformat(),
                "message_count": message_count,
                "first_message": first_message.content[:100] if first_message else None,
                "last_message": last_message.content[:100] if last_message else None
            }

        except Exception as e:
            logger.error(f"Fehler beim Laden der Zusammenfassung: {e}")
            return None

    async def delete(
        self,
        session_id: str
    ) -> bool:
        """
        Lösche eine komplette Konversation.

        Args:
            session_id: Session ID der zu löschenden Konversation

        Returns:
            True wenn gelöscht, False wenn nicht gefunden
        """
        try:
            result = await self.db.execute(
                select(Conversation).where(Conversation.session_id == session_id)
            )
            conversation = result.scalar_one_or_none()

            if conversation:
                await self.db.delete(conversation)
                await self.db.commit()
                logger.info(f"Konversation gelöscht: {session_id}")
                return True

            return False

        except Exception as e:
            logger.error(f"Fehler beim Löschen der Konversation: {e}")
            await self.db.rollback()
            return False

    async def list_all(
        self,
        limit: int = 50,
        offset: int = 0,
        user_id: int | None = None,
    ) -> list[dict]:
        """
        Hole Liste aller Konversationen.

        Args:
            limit: Maximale Anzahl
            offset: Pagination-Offset
            user_id: Wenn gesetzt, nur Konversationen dieses Users (auth mode).
                Wenn None, werden alle Konversationen zurueckgegeben (single-user mode).

        Returns:
            Liste von Konversations-Zusammenfassungen mit `preview` (gekuerzte
            erste User-Nachricht) und `message_count`.
        """
        try:
            # Count subquery: message count per conversation
            count_subq = (
                select(
                    Message.conversation_id,
                    func.count(Message.id).label("message_count")
                )
                .group_by(Message.conversation_id)
                .subquery()
            )

            # Preview subquery: first user message per conversation
            preview_subq = (
                select(
                    Message.conversation_id,
                    Message.content,
                    func.row_number().over(
                        partition_by=Message.conversation_id,
                        order_by=Message.timestamp.asc()
                    ).label("rn")
                )
                .where(Message.role == "user")
                .subquery()
            )
            first_msg = aliased(preview_subq)

            # Main query joining both subqueries
            stmt = (
                select(
                    Conversation,
                    func.coalesce(count_subq.c.message_count, 0).label("message_count"),
                    first_msg.c.content.label("preview_content"),
                )
                .outerjoin(count_subq, Conversation.id == count_subq.c.conversation_id)
                .outerjoin(first_msg, (Conversation.id == first_msg.c.conversation_id) & (first_msg.c.rn == 1))
                .order_by(Conversation.updated_at.desc())
                .limit(limit)
                .offset(offset)
            )
            if user_id is not None:
                stmt = stmt.where(Conversation.user_id == user_id)

            result = await self.db.execute(stmt)
            rows = result.all()

            summaries = []
            for conv, message_count, preview_content in rows:
                summaries.append({
                    "session_id": conv.session_id,
                    "created_at": conv.created_at.isoformat(),
                    "updated_at": conv.updated_at.isoformat(),
                    "message_count": message_count,
                    "preview": preview_content[:100] if preview_content else "Leere Konversation"
                })

            logger.info(f"Geladen: {len(summaries)} Konversationen")
            return summaries

        except Exception as e:
            logger.error(f"Fehler beim Laden der Konversationen: {e}")
            return []

    async def search(
        self,
        query: str,
        limit: int = 20
    ) -> list[dict]:
        """
        Suche in Konversationen nach Text.

        Args:
            query: Suchbegriff
            limit: Maximale Anzahl Ergebnisse

        Returns:
            Liste von Konversationen mit passenden Nachrichten
        """
        try:
            # Search messages and join conversations in a single query
            result = await self.db.execute(
                select(Message, Conversation)
                .join(Conversation, Message.conversation_id == Conversation.id)
                .where(Message.content.ilike(f"%{query}%"))
                .order_by(Message.timestamp.desc())
                .limit(limit)
            )
            rows = result.all()

            # Group by conversation
            from collections import OrderedDict
            conv_groups: OrderedDict[int, dict] = OrderedDict()
            for msg, conv in rows:
                if conv.id not in conv_groups:
                    conv_groups[conv.id] = {
                        "session_id": conv.session_id,
                        "created_at": conv.created_at.isoformat(),
                        "updated_at": conv.updated_at.isoformat(),
                        "matching_messages": []
                    }
                conv_groups[conv.id]["matching_messages"].append({
                    "role": msg.role,
                    "content": msg.content,
                    "timestamp": msg.timestamp.isoformat()
                })

            results = list(conv_groups.values())
            logger.info(f"Gefunden: {len(results)} Konversationen mit '{query}'")
            return results

        except Exception as e:
            logger.error(f"Fehler bei der Suche: {e}")
            return []

    def _is_postgres(self) -> bool:
        """True when the bound dialect is Postgres (FTS available)."""
        try:
            return self.db.bind.dialect.name == "postgresql"
        except Exception:
            return True

    async def search_messages(
        self,
        query: str,
        *,
        user_id: int | None,
        session_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Full-text message search, scoped by conversation OWNERSHIP.

        Roadmap item 3 (chat-UI modernization). Ranks matching ``messages``
        with Postgres FTS (``ts_rank`` over the GENERATED multilingual
        ``search_vector`` column) and returns enough per match to jump to it:
        the owning ``session_id`` + a ``message_index`` (0-based position in the
        conversation's ``timestamp ASC`` ordering — the SAME ordering
        ``/api/chat/history`` returns, so the frontend can scroll to
        ``messages[message_index]``) + a highlighted snippet.

        Scoping (critical): ``messages`` is NOT an atom — it has no
        ``circle_tier`` / ``atom_id``. This does NOT route through
        ``services/circle_sql.py``. Access = the asker OWNS the conversation
        (``Conversation.user_id == user_id``). In single-user mode
        (``user_id is None``, AUTH_ENABLED=false) all conversations are in
        scope, mirroring ``list_all`` / the existing ``/api/chat/search``
        ownership filter.

        Args:
            query: search text (caller enforces a min length).
            user_id: the asker's id; ``None`` = single-user mode (no filter).
            session_id: when given, restrict to that one conversation
                (in-conversation search); otherwise global (cross-conversation).
            limit / offset: pagination.

        Returns a dict ``{"results": [...], "count": n, "has_more": bool}``.
        Each result: ``session_id``, ``message_index``, ``role``, ``content``,
        ``snippet`` (HTML-safe, ``<mark>``-highlighted), ``timestamp``,
        ``rank``. Returns an empty result set (never raises) on thin queries,
        no significant tokens, or any DB error.
        """
        tokens = _significant_message_tokens(query)
        if not tokens:
            return {"results": [], "count": 0, "has_more": False}

        try:
            if not self._is_postgres():
                return await self._search_messages_sqlite(
                    tokens, user_id=user_id, session_id=session_id,
                    limit=limit, offset=offset,
                )

            # Multilingual union (same pattern as the chunk/memory/fact paths).
            # ``tokens`` are word-chars only, so " OR " join is websearch-safe.
            or_query = " OR ".join(tokens)
            tsq = build_tsquery_union_sql("or_query")

            params: dict[str, Any] = {
                "or_query": or_query,
                "limit": limit,
                # Over-fetch by one to compute has_more without a COUNT(*).
                "fetch": limit + 1,
                "offset": offset,
            }

            owner_clause = ""
            if user_id is not None:
                owner_clause = "AND c.user_id = :user_id"
                params["user_id"] = user_id

            session_clause = ""
            if session_id is not None:
                session_clause = "AND c.session_id = :session_id"
                params["session_id"] = session_id

            # ts_headline highlight markers: NON-PRINTABLE control sentinels
            # (STX/ETX), NOT HTML. The frontend splits on them and renders the
            # highlight with a real React element — so the snippet is never
            # interpreted as markup (no dangerouslySetInnerHTML, no XSS surface,
            # no HTML sanitizer dependency). STX/ETX cannot occur in normal chat
            # text. Passed as a bound parameter (kept out of the f-string).
            params["hl_opts"] = (
                f"StartSel={_HL_START}, StopSel={_HL_END}, "
                "MaxFragments=2, MaxWords=18, MinWords=5, ShortWord=2"
            )

            # Chat branching (Phase 1), seam 4: search is restricted to the
            # ACTIVE branch of each conversation, and message_index is the ordinal
            # WITHIN that branch (not the global position) — else jump-to-message
            # would scroll to the wrong row now that /api/chat/history only returns
            # the active path.
            #
            # `active` walks parent_message_id UPWARD from each conversation's
            # active_leaf_message_id (recursive CTE seeded per owned/scoped
            # conversation). `indexed` then numbers ONLY those active-branch
            # messages by (timestamp ASC, id ASC) — the same ordering history
            # returns — so message_index aligns with the rendered thread. For a
            # linear (un-forked, backfilled) conversation the active branch == the
            # full message set, so this is byte-identical to the pre-branching
            # query.
            #
            # RISK NOTE (reviewer): recursive CTE. Bounded by depth < 10000 and
            # terminates at the root (NULL parent). Conversations with a NULL
            # active_leaf (pre-backfill / empty) contribute no rows — acceptable
            # (search over an un-backfilled conversation returns nothing rather
            # than wrong indices; the backfill makes this a non-issue in prod).
            sql = text(f"""
                WITH RECURSIVE scoped_conv AS (
                    SELECT c.id, c.session_id, c.active_leaf_message_id
                    FROM conversations c
                    WHERE c.active_leaf_message_id IS NOT NULL
                      {owner_clause} {session_clause}
                ),
                active(conversation_id, id, parent_message_id, timestamp, depth) AS (
                    SELECT sc.id, m.id, m.parent_message_id, m.timestamp, 0
                    FROM scoped_conv sc
                    JOIN messages m ON m.id = sc.active_leaf_message_id
                    UNION ALL
                    SELECT a.conversation_id, p.id, p.parent_message_id, p.timestamp, a.depth + 1
                    FROM messages p
                    JOIN active a ON p.id = a.parent_message_id
                    WHERE a.depth < 10000
                      -- SECURITY (defense in depth): confine the upward walk to
                      -- the seed conversation, so a stray cross-conversation
                      -- parent pointer can never pull a foreign message into the
                      -- search results (the seed JOIN only guards the first hop).
                      AND p.conversation_id = a.conversation_id
                ),
                indexed AS (
                    SELECT
                        m.id AS id,
                        m.role AS role,
                        m.content AS content,
                        m.timestamp AS timestamp,
                        m.search_vector AS search_vector,
                        sc.session_id AS session_id,
                        (row_number() OVER (
                            PARTITION BY a.conversation_id
                            ORDER BY m.timestamp ASC, m.id ASC
                        ) - 1) AS message_index
                    FROM active a
                    JOIN messages m ON m.id = a.id
                    JOIN scoped_conv sc ON sc.id = a.conversation_id
                )
                SELECT
                    session_id,
                    message_index,
                    role,
                    content,
                    timestamp,
                    ts_rank(search_vector, ({tsq})) AS rank,
                    ts_headline('simple', content, ({tsq}), :hl_opts) AS snippet
                FROM indexed
                WHERE search_vector IS NOT NULL
                  AND search_vector @@ ({tsq})
                ORDER BY rank DESC, timestamp DESC
                LIMIT :fetch OFFSET :offset
            """)

            result = await self.db.execute(sql, params)
            rows = result.all()

            has_more = len(rows) > limit
            rows = rows[:limit]

            results = [
                {
                    "session_id": r.session_id,
                    "message_index": int(r.message_index),
                    "role": r.role,
                    "content": r.content,
                    "snippet": r.snippet,
                    "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                    "rank": float(r.rank) if r.rank is not None else 0.0,
                }
                for r in rows
            ]
            logger.info(
                f"Message-Suche: {len(results)} Treffer für '{query[:50]}' "
                f"(session={session_id or 'all'})"
            )
            return {"results": results, "count": len(results), "has_more": has_more}

        except Exception as e:
            logger.error(f"Fehler bei der Message-Suche: {e}")
            return {"results": [], "count": 0, "has_more": False}

    async def _search_messages_sqlite(
        self,
        tokens: list[str],
        *,
        user_id: int | None,
        session_id: str | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        """Sqlite test-harness fallback: token-OR LIKE, ownership-scoped.

        No tsvector on sqlite. Match ANY token against ``content``, compute the
        per-conversation message_index in Python (timestamp ASC, id tiebreak),
        and a naive ``<mark>``-wrapped snippet. Rank = number of distinct tokens
        matched. Same ownership scope as the Postgres path.
        """
        # Pull the owned (and optionally session-scoped) conversations and ALL
        # their messages ordered timestamp-ASC so message_index is exact.
        conv_q = select(Conversation.id, Conversation.session_id)
        if user_id is not None:
            conv_q = conv_q.where(Conversation.user_id == user_id)
        if session_id is not None:
            conv_q = conv_q.where(Conversation.session_id == session_id)
        conv_rows = (await self.db.execute(conv_q)).all()
        sessions_by_conv = {cid: sid for cid, sid in conv_rows}
        if not sessions_by_conv:
            return {"results": [], "count": 0, "has_more": False}

        msg_q = (
            select(Message)
            .where(Message.conversation_id.in_(list(sessions_by_conv.keys())))
            .order_by(Message.conversation_id, Message.timestamp.asc(), Message.id.asc())
        )
        msgs = (await self.db.execute(msg_q)).scalars().all()

        lowered = [t.lower() for t in tokens]
        matches: list[dict[str, Any]] = []
        index_by_conv: dict[int, int] = {}
        for m in msgs:
            idx = index_by_conv.get(m.conversation_id, 0)
            index_by_conv[m.conversation_id] = idx + 1
            content = m.content or ""
            cl = content.lower()
            hit_count = sum(1 for t in lowered if t in cl)
            if hit_count == 0:
                continue
            # Naive snippet: a window around the first matched token.
            first_pos = min(
                (cl.find(t) for t in lowered if cl.find(t) >= 0),
                default=0,
            )
            start = max(0, first_pos - 30)
            snippet = content[start:start + 120]
            matches.append({
                "session_id": sessions_by_conv[m.conversation_id],
                "message_index": idx,
                "role": m.role,
                "content": content,
                "snippet": snippet,
                "timestamp": m.timestamp.isoformat() if m.timestamp else None,
                "rank": float(hit_count),
            })

        matches.sort(key=lambda r: (-r["rank"], r["timestamp"] or ""), reverse=False)
        # Re-sort: highest rank first, then newest first.
        matches.sort(key=lambda r: (r["rank"], r["timestamp"] or ""), reverse=True)
        window = matches[offset:offset + limit + 1]
        has_more = len(window) > limit
        window = window[:limit]
        return {"results": window, "count": len(window), "has_more": has_more}
