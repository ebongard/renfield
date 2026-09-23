"""
Chat API Routes
"""
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import ChatUpload, Conversation, Message, User
from models.permissions import Permission
from services.api_rate_limiter import limiter
from services.auth_service import get_current_user, require_permission
from services.circle_sql import conversations_circles_filter
from services.conversation_service import ConversationService
from services.database import get_db
from services.input_guard import detect_injection
from services.ollama_service import OllamaService
from utils.config import settings

router = APIRouter()

class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    context: list[dict] | None = None

class ChatResponse(BaseModel):
    message: str
    session_id: str
    intent: dict | None = None

class ActiveLeafRequest(BaseModel):
    """Body for PUT /api/chat/{session_id}/active-leaf — switch active branch."""
    message_id: int

def _has_chat_all(current_user) -> bool:
    """Does this caller hold `chat.all`? (the admin, by role)

    A tier-2 room history is owned by the DEVICE account, so no person is its
    owner — without this escape nobody could ever delete or re-branch one
    (D-3: "Löschen, Umbenennen, Verzweigen nur durch den Admin").
    """
    if current_user is None:
        return False
    try:
        from models.permissions import has_permission

        return has_permission(current_user.get_permissions(), Permission.CHAT_ALL)
    except Exception as e:  # noqa: BLE001 — a permission read must not 500 a delete
        # Denying is the safe direction, but never the silent one: `get_permissions()`
        # reads `User.role`, and a lazy load on an async session raises
        # MissingGreenlet. Swallowed, that reads as "this admin has no chat.all"
        # and the shared room history becomes undeletable by anyone — the exact
        # failure mode that hid the satellite speaker-permission bug (P0 Nr. 3).
        logger.warning(f"⚠️ `chat.all` konnte nicht gelesen werden, verweigere: {e}")
        return False


def _reach_clause(current_user) -> tuple[str, dict] | None:
    """The four-branch circle filter for `conversations`, or None when it does
    not apply (auth off, or no caller identity).

    A conversation is a shared artifact (§8.1): the room history belongs to the
    device account, and every member whose tier reaches it reads the SAME
    thread. Owner equality would hide it from all of them.
    """
    if not settings.auth_enabled or current_user is None:
        return None
    return conversations_circles_filter(current_user.id, alias="conversations")


async def conversation_is_callers(conversation, current_user, db: AsyncSession) -> bool:
    """May this caller continue this conversation? (auth-on cutover §4.2, §8.1)

    The session id comes from the CLIENT and is never validated, so this route —
    the fallback the browser uses while the socket is not ready yet — would
    otherwise read the last 10 messages of any conversation it merely named and
    append to it. Same rule as the WS path, and the WS path now asks for REACH:
    a member who may continue the shared kitchen thread over the socket must not
    be pushed into a context-less new conversation merely because the socket was
    slow. An OWNERLESS conversation is still refused — every reach branch keys
    on the owner, so `reaches` says no. A conversation that does not exist yet is
    free to take, and auth-off (single trust domain) is unchanged.
    """
    if conversation is None or not settings.auth_enabled or current_user is None:
        return True
    return await ConversationService(db).reaches(conversation.id, current_user.id)


@router.post("/send", response_model=ChatResponse)
@limiter.limit(settings.api_rate_limit_chat)
async def send_message(
    request: Request,
    chat_request: ChatRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user)
):
    """Nachricht senden und Antwort erhalten"""
    try:
        logger.info(f"📨 Neue Nachricht: '{chat_request.message[:100]}'")

        # Prompt injection check
        injection_result = detect_injection(chat_request.message)
        if injection_result.blocked:
            logger.warning(
                f"Blocked injection attempt: score={injection_result.score:.2f}, "
                f"patterns={injection_result.matched_patterns}"
            )
            raise HTTPException(status_code=400, detail="Request blocked by input guard.")

        # Session ID generieren falls nicht vorhanden
        session_id = chat_request.session_id or str(uuid.uuid4())

        # Conversation in DB speichern/laden
        result = await db.execute(
            select(Conversation).where(Conversation.session_id == session_id)
        )
        conversation = result.scalar_one_or_none()

        # Same ownership rule as the WS path: a conversation that is not the
        # caller's is not continued — the request gets a fresh one instead.
        if not await conversation_is_callers(conversation, current_user, db):
            logger.warning(
                f"🚫 Refused REST chat into a conversation out of the caller's "
                f"reach: owner={conversation.user_id} caller={current_user.id}"
            )
            conversation = None
            session_id = str(uuid.uuid4())

        if not conversation:
            conversation = Conversation(session_id=session_id)
            if current_user is not None:
                conversation.user_id = current_user.id
            db.add(conversation)
            await db.flush()
            # A browser chat is an atom like any other (§8.1). Registering it
            # HERE and not only in `save_message` is what keeps the invariant
            # whole: this route is the second creation path, and a conversation
            # without an atoms row can never be shared with one named person
            # (the explicit-grant branch joins through `atoms`).
            await ConversationService(db).ensure_atom(conversation)
            await db.commit()
            await db.refresh(conversation)

        # User Message speichern
        user_msg = Message(
            conversation_id=conversation.id,
            role="user",
            content=chat_request.message
        )
        db.add(user_msg)

        # Kontext aus DB laden falls nicht übergeben
        context = chat_request.context or []
        if not context:
            # Letzte 10 Nachrichten laden
            result = await db.execute(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.timestamp.desc())
                .limit(10)
            )
            messages = result.scalars().all()
            context = [
                {"role": msg.role, "content": msg.content}
                for msg in reversed(messages)
            ]

        # Ollama Service nutzen
        from main import app
        ollama: OllamaService = app.state.ollama

        response_text = ""
        intent = None
        action_result = None

        # === Per-user RBAC on the REST agent/executor path ===
        # This route runs the agent loop and the intent executor, both of which
        # reach MCP tools. Without the caller's permissions they run with
        # user_permissions=None, which mcp_client treats as allow-all — an
        # authenticated user could read/act beyond their role, bypassing the
        # WebSocket path's RBAC. Load permissions the same way chat_handler does
        # and thread them into both call sites. Fail closed: an authenticated
        # request whose permissions can't be resolved gets [] (deny), never None.
        user_id = current_user.id if current_user else None
        user_permissions = None
        if user_id is not None:
            try:
                # NOTE: `select` comes from the module-level import. A local
                # `from sqlalchemy import select` here made the name local to
                # the WHOLE function, so the use at the top of send_message
                # referenced it before assignment (ruff F823).
                from sqlalchemy.orm import selectinload

                from models.database import User as _RbacUser
                _res = await db.execute(
                    select(_RbacUser)
                    .options(selectinload(_RbacUser.role))
                    .where(_RbacUser.id == int(user_id))
                )
                _u = _res.scalar_one_or_none()
                if _u is not None:
                    user_permissions = _u.get_permissions()
            except Exception as e:
                logger.warning(f"Failed to load user permissions for /api/chat/send: {e}")
            if user_permissions is None and settings.auth_enabled:
                user_permissions = []  # authenticated but unresolved → deny

        # === Agent Loop Check ===
        if settings.agent_enabled:
            from services.complexity_detector import ComplexityDetector
            if await ComplexityDetector.needs_agent_with_feedback(chat_request.message):
                logger.info(f"🤖 Agent Loop (REST) aktiviert für: '{chat_request.message[:80]}...'")

                from services.action_executor import ActionExecutor
                from services.agent_service import AgentService
                from services.agent_tools import AgentToolRegistry

                mcp_manager = getattr(app.state, 'mcp_manager', None)
                tool_registry = await AgentToolRegistry.create(mcp_manager=mcp_manager)
                agent = AgentService(tool_registry)
                # session_id lets session-scoped tools reach this conversation
                # later (e.g. a background scan reporting its outcome back here).
                executor = ActionExecutor(mcp_manager=mcp_manager, session_id=session_id)

                async for step in agent.run(
                    message=chat_request.message,
                    ollama=ollama,
                    executor=executor,
                    session_id=session_id,
                    user_permissions=user_permissions,
                    user_id=user_id,
                ):
                    if step.step_type == "final_answer":
                        response_text = step.content

                if not response_text:
                    response_text = "Entschuldigung, ich konnte die Anfrage nicht bearbeiten."

                # check_output gate (#1269). The REST path never fired it, so a
                # plugin's redactor silently did not run here. No streaming on
                # this path — the answer exists in one piece, so the gate is a
                # plain call before persisting AND returning.
                from services.output_gate import apply_check_output_gate
                response_text = await apply_check_output_gate(
                    response_text, user_id=user_id
                )

                # Save and return (skip single-intent path)
                assistant_msg = Message(
                    conversation_id=conversation.id,
                    role="assistant",
                    content=response_text,
                    message_metadata={"agent": True}
                )
                db.add(assistant_msg)
                conversation.updated_at = datetime.now(UTC).replace(tzinfo=None)
                await db.commit()

                return ChatResponse(
                    message=response_text,
                    session_id=session_id,
                    intent={"intent": "agent.multi_step", "parameters": {}}
                )

        # === Ranked Intent Path with Fallback Chain ===
        logger.info("🔍 Extrahiere Ranked Intents...")
        ranked_intents = await ollama.extract_ranked_intents(chat_request.message)

        from services.action_executor import ActionExecutor
        mcp_mgr = getattr(app.state, 'mcp_manager', None)
        intent_used = None

        for intent_candidate in ranked_intents:
            intent_name = intent_candidate.get("intent", "general.conversation")
            logger.info(f"🎯 Versuche Intent: {intent_name} (confidence: {intent_candidate.get('confidence', 0):.2f})")

            if intent_name == "general.conversation":
                intent = intent_candidate
                intent_used = intent_candidate
                break

            executor = ActionExecutor(mcp_manager=mcp_mgr, session_id=session_id)
            candidate_result = await executor.execute(
                intent_candidate,
                user_permissions=user_permissions,
                user_id=user_id,
            )

            if candidate_result.get("success") and not candidate_result.get("empty_result"):
                intent = intent_candidate
                action_result = candidate_result
                intent_used = intent_candidate
                logger.info(f"✅ Intent {intent_name} erfolgreich: {candidate_result.get('message', '')[:80]}")
                break

            logger.info(f"⏭️ Intent {intent_name} lieferte kein Ergebnis, versuche nächsten...")

        # If no ranked intent worked, fall back to conversation
        if not intent_used:
            logger.info("💬 Alle Intents fehlgeschlagen, Fallback zu Konversation")
            intent = {"intent": "general.conversation", "parameters": {}, "confidence": 1.0}

        # Antwort generieren
        if action_result and action_result.get("success"):
            enhanced_prompt = f"""Du bist Renfield, ein persönlicher Assistent.

Der Nutzer hat gefragt: "{chat_request.message}"

Die Aktion wurde ausgeführt mit folgendem Ergebnis:
{action_result.get('message')}

Zusätzliche Details:
{action_result.get('data', {})}

Gib eine kurze, natürliche Antwort basierend auf dem Ergebnis.
WICHTIG: Gib NUR die Antwort, KEIN JSON, KEINE technischen Details!"""

            response_text = await ollama.chat(enhanced_prompt)

        elif action_result and not action_result.get("success"):
            response_text = f"Entschuldigung, das konnte ich nicht ausführen: {action_result.get('message')}"

        else:
            response_text = await ollama.chat(chat_request.message, history=context)

        # check_output gate (#1269) — same reasoning as the agent branch above.
        from services.output_gate import apply_check_output_gate
        response_text = await apply_check_output_gate(response_text, user_id=user_id)

        # Assistant Message speichern
        assistant_msg = Message(
            conversation_id=conversation.id,
            role="assistant",
            content=response_text,
            message_metadata={
                "intent": intent,
                "action_result": action_result
            }
        )
        db.add(assistant_msg)

        # Update conversation timestamp
        conversation.updated_at = datetime.now(UTC).replace(tzinfo=None)

        await db.commit()

        logger.info(f"✅ Antwort generiert: '{response_text[:100]}'")

        return ChatResponse(
            message=response_text,
            session_id=session_id,
            intent=intent
        )
    except Exception as e:
        logger.error(f"❌ Chat Fehler: {e}")
        import traceback
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/history/{session_id}")
async def get_history(
    session_id: str,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    """Chat-Historie abrufen"""
    try:
        query = select(Conversation).where(Conversation.session_id == session_id)
        reach = _reach_clause(current_user)
        if reach is not None:
            query = query.where(text(reach[0]).bindparams(**reach[1]))
        elif current_user is not None:
            query = query.where(Conversation.user_id == current_user.id)
        result = await db.execute(query)
        conversation = result.scalar_one_or_none()

        if not conversation:
            return {"messages": []}

        # Chat branching (Phase 1), seam 1: return the messages on the ACTIVE
        # branch (recursive walk of parent_message_id up from
        # active_leaf_message_id), NOT the flat conversation set — else an edited
        # turn's abandoned sibling would still show. For a linear (un-forked,
        # backfilled) conversation the active path == the flat timestamp-ASC
        # order, so this is byte-identical to pre-branching. Empty path (null
        # leaf / pre-backfill / sqlite) → flat fallback.
        from services.conversation_service import ConversationService

        _bsvc = ConversationService(db)
        active_ids = await _bsvc.active_path_message_ids(conversation)
        # Chat branching (Phase 2): per-message sibling info for the ‹n/m›
        # switcher. Empty for a linear/sqlite conversation → no switchers shown.
        branch_meta = await _bsvc.branch_metadata(conversation, active_ids)
        if active_ids:
            window_ids = active_ids[:limit]
            result = await db.execute(
                select(Message).where(Message.id.in_(window_ids))
            )
            by_id = {m.id: m for m in result.scalars().all()}
            messages = [by_id[i] for i in window_ids if i in by_id]
        else:
            result = await db.execute(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.timestamp.asc())
                .limit(limit)
            )
            messages = result.scalars().all()

        # Collect attachment IDs from user messages for bulk fetch
        all_attachment_ids = []
        for msg in messages:
            if msg.message_metadata and msg.role == "user":
                all_attachment_ids.extend(msg.message_metadata.get("attachment_ids", []))

        attachments_map = {}
        if all_attachment_ids:
            att_result = await db.execute(
                select(ChatUpload).where(ChatUpload.id.in_(all_attachment_ids))
            )
            for upload in att_result.scalars().all():
                attachments_map[upload.id] = {
                    "id": upload.id,
                    "filename": upload.filename,
                    "file_type": upload.file_type,
                    "file_size": upload.file_size,
                    "status": upload.status,
                }

        return {
            "messages": [
                {
                    "id": msg.id,  # chat branching: frontend carries it for edit/regen forks
                    "role": msg.role,
                    "content": msg.content,
                    "timestamp": msg.timestamp.isoformat(),
                    "metadata": msg.message_metadata,  # Spalte heißt message_metadata
                    # Chat branching (Phase 2): {index,count,sibling_ids} when this
                    # message has sibling branches; absent otherwise.
                    **({"branch": branch_meta[msg.id]} if msg.id in branch_meta else {}),
                    **({"attachments": [
                        attachments_map[aid]
                        for aid in msg.message_metadata.get("attachment_ids", [])
                        if aid in attachments_map
                    ]} if msg.role == "user" and msg.message_metadata
                        and msg.message_metadata.get("attachment_ids") else {}),
                }
                for msg in messages
            ]
        }
    except Exception as e:
        logger.error(f"❌ Fehler beim Laden der Historie: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/session/{session_id}")
async def delete_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    """Chat-Session löschen"""
    try:
        query = select(Conversation).where(Conversation.session_id == session_id)
        # Destructive: reach is NOT enough. A shared room history would
        # otherwise be deletable by every member it reaches — owner or
        # `chat.all` only (D-3).
        if current_user is not None and not _has_chat_all(current_user):
            query = query.where(Conversation.user_id == current_user.id)
        result = await db.execute(query)
        conversation = result.scalar_one_or_none()

        if conversation:
            await db.delete(conversation)
            await db.commit()

        return {"success": True}
    except Exception as e:
        logger.error(f"❌ Fehler beim Löschen der Session: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/conversations")
async def list_conversations(
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    """Liste aller Konversationen.

    Authenticated users see only their own conversations; in single-user mode
    (no auth) all conversations are returned. Both paths produce the same
    shape so the frontend ChatSidebar can render `preview` (truncated first
    user message) and `message_count` consistently.
    """
    try:
        from services.conversation_service import ConversationService

        service = ConversationService(db)
        conversations = await service.list_all(
            limit=limit,
            offset=offset,
            user_id=current_user.id if current_user is not None else None,
        )

        return {
            "conversations": conversations,
            "limit": limit,
            "offset": offset,
            "count": len(conversations)
        }
    except Exception as e:
        logger.error(f"❌ Fehler beim Laden der Konversationen: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/conversation/{session_id}/summary")
async def get_conversation_summary(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    """Zusammenfassung einer Konversation"""
    try:
        reach = _reach_clause(current_user)
        if current_user is not None:
            stmt = select(Conversation).where(Conversation.session_id == session_id)
            stmt = (
                stmt.where(text(reach[0]).bindparams(**reach[1])) if reach is not None
                else stmt.where(Conversation.user_id == current_user.id)
            )
            result = await db.execute(stmt)
            if not result.scalar_one_or_none():
                raise HTTPException(status_code=404, detail="Konversation nicht gefunden")

        from main import app
        ollama: OllamaService = app.state.ollama

        summary = await ollama.get_conversation_summary(session_id, db)

        if not summary:
            raise HTTPException(status_code=404, detail="Konversation nicht gefunden")

        return summary
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Fehler beim Laden der Zusammenfassung: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/search")
async def search_conversations(
    q: str,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    """Suche in Konversationen"""
    try:
        if not q or len(q) < 2:
            raise HTTPException(status_code=400, detail="Suchanfrage muss mindestens 2 Zeichen lang sein")

        from main import app
        ollama: OllamaService = app.state.ollama

        results = await ollama.search_conversations(q, db, limit)

        if current_user is not None:
            reach = _reach_clause(current_user)
            stmt = select(Conversation.session_id)
            stmt = (
                stmt.where(text(reach[0]).bindparams(**reach[1])) if reach is not None
                else stmt.where(Conversation.user_id == current_user.id)
            )
            res = await db.execute(stmt)
            user_session_ids = {row[0] for row in res.all()}
            results = [r for r in results if r.get("session_id") in user_session_ids]

        return {
            "query": q,
            "results": results,
            "count": len(results)
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Fehler bei der Suche: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/messages/search")
async def search_messages(
    q: str,
    session_id: str | None = None,
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    """Full-text search over chat messages (roadmap item 3).

    In-conversation when ``session_id`` is given, global (cross-conversation)
    otherwise. Ranked by Postgres FTS (``ts_rank``), paginated, and scoped by
    **conversation REACH** (auth-on §8.1). Messages are still not atoms, but
    the conversation is, so the filter rides on the conversation row through
    ``circle_sql`` — whoever may read a shared room history also finds what was
    said in it. (This reverses the earlier rule, which was owner equality: it
    predates conversations carrying a tier, and left the shared thread findable
    by nobody.) Under auth-off the owner filter stands.

    In single-user mode (AUTH_ENABLED=false → ``current_user is None``) all
    conversations are in scope, mirroring the other chat endpoints.

    Returns ``{query, results, count, has_more}``. Each result carries the
    owning ``session_id``, a 0-based ``message_index`` (position in the
    conversation's ``timestamp ASC`` history — what the client jumps to), the
    matched ``role`` / ``content``, a ``<mark>``-highlighted ``snippet``, the
    ``timestamp`` and the FTS ``rank``.
    """
    if not q or len(q.strip()) < 2:
        raise HTTPException(
            status_code=400,
            detail="Suchanfrage muss mindestens 2 Zeichen lang sein",
        )

    # Cap pagination to keep a single response bounded.
    limit = max(1, min(limit, 50))
    offset = max(0, offset)

    try:
        from services.conversation_service import ConversationService

        service = ConversationService(db)
        payload = await service.search_messages(
            q.strip(),
            user_id=current_user.id if current_user is not None else None,
            session_id=session_id,
            limit=limit,
            offset=offset,
        )
        return {"query": q, **payload}
    except Exception as e:
        logger.error(f"❌ Fehler bei der Message-Suche: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/{session_id}/active-leaf")
async def set_active_leaf(
    session_id: str,
    body: ActiveLeafRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    """Switch a conversation's active branch (chat branching, Phase 1).

    Repoints ``Conversation.active_leaf_message_id`` to the branch ending at
    ``message_id`` — no generation. Ownership-gated like the other chat routes:
    a missing/unowned conversation or a message that doesn't belong to it → 404
    (no existence oracle). Phase 1 ships this endpoint so edit/regenerate can set
    the leaf cleanly; the multi-sibling switcher UI is Phase 2.
    """
    try:
        from services.conversation_service import ConversationService

        ok = await ConversationService(db).set_active_leaf(
            session_id,
            body.message_id,
            user_id=current_user.id if current_user is not None else None,
            caller_has_chat_all=_has_chat_all(current_user),
        )
        if not ok:
            raise HTTPException(status_code=404, detail="Konversation oder Nachricht nicht gefunden")
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Fehler beim Setzen des aktiven Branch: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{session_id}/branch/{message_id}")
async def delete_branch(
    session_id: str,
    message_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    """Delete a branch — ``message_id`` and its whole subtree (chat branching,
    Phase 2).

    Ownership-gated like the other chat routes. A missing/unowned conversation
    or a message foreign to it → 404 (no existence oracle). A message on the
    CURRENT active path → 409 (switch to another branch before deleting the one
    you're viewing, so the active leaf can't be orphaned). Branch-local memories
    are removed with the branch; KG-relation provenance is detached.
    """
    try:
        from services.conversation_service import ConversationService

        status = await ConversationService(db).delete_branch(
            session_id,
            message_id,
            user_id=current_user.id if current_user is not None else None,
            caller_has_chat_all=_has_chat_all(current_user),
        )
        if status == "not_found":
            raise HTTPException(status_code=404, detail="Konversation oder Nachricht nicht gefunden")
        if status == "active":
            raise HTTPException(
                status_code=409,
                detail="Aktiver Branch kann nicht gelöscht werden — wechsle zuerst zu einem anderen Branch",
            )
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Fehler beim Löschen des Branch: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/stats")
async def get_conversation_stats(
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_permission(Permission.ADMIN)),
):
    """Statistiken über Konversationen"""
    try:
        # Gesamt-Anzahl Konversationen
        result = await db.execute(select(func.count(Conversation.id)))
        total_conversations = result.scalar()

        # Gesamt-Anzahl Nachrichten
        result = await db.execute(select(func.count(Message.id)))
        total_messages = result.scalar()

        # Durchschnittliche Nachrichten pro Konversation
        avg_messages = total_messages / total_conversations if total_conversations > 0 else 0

        # Letzte aktive Konversation
        result = await db.execute(
            select(Conversation)
            .order_by(Conversation.updated_at.desc())
            .limit(1)
        )
        latest_conversation = result.scalar_one_or_none()

        # Nachrichten der letzten 24h
        from datetime import timedelta
        yesterday = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)
        result = await db.execute(
            select(func.count(Message.id))
            .where(Message.timestamp >= yesterday)
        )
        messages_24h = result.scalar()

        return {
            "total_conversations": total_conversations,
            "total_messages": total_messages,
            "avg_messages_per_conversation": round(avg_messages, 2),
            "messages_last_24h": messages_24h,
            "latest_activity": latest_conversation.updated_at.isoformat() if latest_conversation else None
        }
    except Exception as e:
        logger.error(f"❌ Fehler beim Laden der Statistiken: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/conversations/cleanup")
async def cleanup_old_conversations(
    days: int = 30,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_permission(Permission.ADMIN)),
):
    """Lösche alte Konversationen (älter als X Tage)"""
    try:
        from datetime import timedelta
        cutoff_date = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)

        # Finde alte Konversationen
        result = await db.execute(
            select(Conversation)
            .where(Conversation.updated_at < cutoff_date)
        )
        old_conversations = result.scalars().all()

        deleted_count = 0
        for conv in old_conversations:
            await db.delete(conv)
            deleted_count += 1

        await db.commit()

        logger.info(f"🧹 Gelöscht: {deleted_count} Konversationen älter als {days} Tage")

        return {
            "success": True,
            "deleted_count": deleted_count,
            "cutoff_days": days
        }
    except Exception as e:
        logger.error(f"❌ Fehler beim Cleanup: {e}")
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
