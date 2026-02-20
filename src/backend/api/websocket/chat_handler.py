"""
WebSocket handler for real-time chat (/ws endpoint).

This module handles:
- Real-time chat with streaming responses
- Intent extraction and action execution
- RAG (Retrieval-Augmented Generation) support
- Conversation persistence
- Room context auto-detection
"""

import asyncio

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from loguru import logger
from pydantic import ValidationError

from models.websocket_messages import WSChatMessage, WSErrorCode
from services.database import AsyncSessionLocal
from services.websocket_auth import WSAuthError, authenticate_websocket
from services.websocket_rate_limiter import get_rate_limiter
from utils.config import settings

from .shared import (
    ConversationSessionState,
    is_followup_question,
    register_ws_connection,
    send_ws_error,
    unregister_ws_connection,
)

router = APIRouter()

# Prevent background tasks from being garbage-collected
_background_tasks: set[asyncio.Task] = set()


def _parse_mcp_raw_data(data: list) -> any:
    """Parse MCP raw_data format: [{"type": "text", "text": "{JSON}"}] → parsed content.

    MCP execute_tool() returns data as a list of content blocks.
    The actual result data is a JSON string inside the "text" field.
    Returns the parsed data (dict or list) or None if not MCP format.
    """
    import json

    if not data or not isinstance(data, list):
        return None

    # Check if this looks like MCP raw_data format
    # MCP format: all items have "type" and "text" keys
    if not all(isinstance(item, dict) and "type" in item and "text" in item for item in data):
        return None

    # Concatenate all text blocks and try to parse as JSON
    combined_text = "\n".join(item.get("text", "") for item in data if item.get("type") == "text")
    if not combined_text:
        return None

    try:
        parsed = json.loads(combined_text)
        return parsed
    except (json.JSONDecodeError, TypeError):
        # Not valid JSON — return as plain text summary
        if len(combined_text) > 50:
            return {"text_summary": combined_text}
        return None


def _build_agent_action_result(tool_results: list) -> dict:
    """Build a synthetic action_result from agent tool results for conversation history.

    Agent loop responses don't have action_result like single-intent paths.
    This collects the most useful tool results (searches, not downloads) and
    builds a result dict that _build_action_summary can process.
    """
    # Prefer search/list results over download/send results for the summary
    # (search results contain IDs and titles needed for follow-ups)
    best_data = None
    best_intent = None
    for tool_name, data in tool_results:
        if not data:
            continue
        # Prioritize search/list tools (contain IDs the user might reference)
        if any(kw in (tool_name or "") for kw in ("search", "list", "get_")) or best_data is None:
            best_data = data
            best_intent = tool_name

    if best_data is None:
        return None

    return {
        "success": True,
        "data": best_data,
        "_agent_intent": best_intent,
    }


def _build_action_summary(intent: dict, action_result: dict, max_chars: int = 2000) -> str:
    """Build a compact summary of action results for conversation history.

    This enables follow-up references like "die letzte" or "schick das per mail"
    by including structured result data (IDs, titles, dates) in the history.
    """
    import json

    intent_name = intent.get("intent", "") if intent else ""
    data = action_result.get("data")
    if not data:
        return ""

    # For dict results with nested lists (e.g. {"results": [...], "count": 17})
    if isinstance(data, dict):
        results_list = data.get("results") or data.get("items") or data.get("documents")
        if isinstance(results_list, list):
            return _build_action_summary(
                intent, {"success": True, "data": results_list}, max_chars
            )
        # Simple dict — compact JSON
        compact = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        return f"{intent_name} → {compact[:max_chars]}"

    # For list results (e.g. search_documents, MCP tool results)
    if isinstance(data, list):
        # MCP tools return [{"type": "text", "text": "{JSON string}"}]
        # Parse the inner JSON to extract actual result data
        parsed_data = _parse_mcp_raw_data(data)
        if parsed_data is not None:
            return _build_action_summary(intent, {"success": True, "data": parsed_data}, max_chars)

        items = []
        for item in data[:10]:  # max 10 items
            if isinstance(item, dict):
                # Pick key fields: id, title, name, subject, date
                summary_parts = []
                for key in ("id", "title", "name", "subject", "created", "date"):
                    if key in item:
                        summary_parts.append(f"{key}={item[key]}")
                if summary_parts:
                    items.append(", ".join(summary_parts))
            else:
                items.append(str(item)[:100])
        if not items:
            return ""
        result = f"{intent_name} → {len(data)} Ergebnisse:\n" + "\n".join(f"  - {i}" for i in items)
        if len(data) > 10:
            result += f"\n  ... und {len(data) - 10} weitere"
        return result[:max_chars]

    return ""


async def _route_chat_tts_output(
    room_context: dict,
    response_text: str,
    websocket: WebSocket
) -> bool:
    """
    Route TTS audio for chat WebSocket to the best available output device.

    Returns True if TTS was handled server-side (sent to HA media player),
    False if frontend should handle TTS.
    """
    room_id = room_context.get("room_id")
    device_id = room_context.get("device_id")

    if not room_id:
        return False

    try:
        from services.audio_output_service import get_audio_output_service
        from services.output_routing_service import OutputRoutingService

        async with AsyncSessionLocal() as db_session:
            routing_service = OutputRoutingService(db_session)

            # Get the best audio output device for this room
            decision = await routing_service.get_audio_output_for_room(
                room_id=room_id,
                input_device_id=device_id
            )

            logger.info(f"🔊 Chat output routing: {decision.reason} → {decision.target_type}:{decision.target_id}")

            # Only handle server-side if we have a configured HA output device
            # (Renfield devices would be the input device itself in this case)
            if decision.output_device and not decision.fallback_to_input and decision.target_type == "homeassistant":
                # Generate TTS
                from services.piper_service import get_piper_service
                piper = get_piper_service()
                tts_audio = await piper.synthesize_to_bytes(response_text)

                if tts_audio:
                    audio_output_service = get_audio_output_service()
                    success = await audio_output_service.play_audio(
                        audio_bytes=tts_audio,
                        output_device=decision.output_device,
                        session_id=f"chat-{room_id}-{device_id}"
                    )

                    if success:
                        logger.info(f"🔊 TTS sent to HA media player: {decision.target_id}")
                        return True
                    else:
                        logger.warning(f"Failed to send TTS to {decision.target_id}, frontend will handle")

            return False

    except Exception as e:
        logger.error(f"❌ Chat TTS routing failed: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False


async def _extract_memories_background(
    user_message: str,
    assistant_response: str,
    user_id: int | None,
    session_id: str | None,
    lang: str,
) -> None:
    """Background task: extract and save memories from a conversation exchange."""
    try:
        async with AsyncSessionLocal() as db:
            from services.conversation_memory_service import ConversationMemoryService
            service = ConversationMemoryService(db)
            memories = await service.extract_and_save(
                user_message=user_message,
                assistant_response=assistant_response,
                user_id=user_id,
                session_id=session_id,
                lang=lang,
            )
            if memories:
                logger.info(f"Extracted {len(memories)} memories from conversation")
    except Exception as e:
        logger.warning(f"Memory extraction failed: {e}")


def _format_file_size(size_bytes: int | None) -> str:
    """Format file size in human-readable form."""
    if not size_bytes:
        return "unknown size"
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


async def _fetch_document_context(attachment_ids: list[int], lang: str) -> str:
    """Fetch extracted text from uploaded documents and format as prompt context.

    Args:
        attachment_ids: List of ChatUpload IDs to include
        lang: Language for prompt templates (de/en)

    Returns:
        Formatted document context string, or empty string on error/no results.
    """
    if not attachment_ids:
        return ""

    try:
        from sqlalchemy import select

        from models.database import ChatUpload
        from services.prompt_manager import prompt_manager

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(ChatUpload).where(
                    ChatUpload.id.in_(attachment_ids),
                    ChatUpload.status == "completed",
                    ChatUpload.extracted_text.isnot(None),
                )
            )
            uploads = result.scalars().all()

        if not uploads:
            return ""

        max_chars = settings.chat_upload_max_context_chars

        if len(uploads) == 1:
            doc = uploads[0]
            text = doc.extracted_text[:max_chars] if doc.extracted_text else ""
            return prompt_manager.get(
                "chat", "document_context_section", lang=lang,
                filename=doc.filename or "document",
                file_size=_format_file_size(doc.file_size),
                document_text=text,
            )

        # Multiple documents — distribute max_chars evenly
        per_doc_chars = max_chars // len(uploads)
        doc_sections = []
        for doc in uploads:
            text = doc.extracted_text[:per_doc_chars] if doc.extracted_text else ""
            section = prompt_manager.get(
                "chat", "document_separator", lang=lang,
                filename=doc.filename or "document",
                file_size=_format_file_size(doc.file_size),
                document_text=text,
            )
            doc_sections.append(section)

        return prompt_manager.get(
            "chat", "document_context_multi_section", lang=lang,
            count=str(len(uploads)),
            documents="\n\n".join(doc_sections),
        )

    except Exception as e:
        logger.warning(f"Document context fetch failed: {e}")
        return ""


async def _retrieve_memory_context(content: str, user_id: int | None, lang: str) -> str:
    """Retrieve relevant memories and format as prompt section."""
    from utils.hooks import run_hooks

    sections: list[str] = []

    # Built-in memory retrieval
    if settings.memory_enabled:
        from services.conversation_memory_service import ConversationMemoryService
        from services.prompt_manager import prompt_manager

        try:
            async with AsyncSessionLocal() as db:
                service = ConversationMemoryService(db)
                memories = await service.retrieve(content, user_id=user_id)
                if memories:
                    lines = []
                    for m in memories:
                        cat_label = m["category"].upper()
                        lines.append(f"- [{cat_label}] {m['content']}")
                    memories_str = "\n".join(lines)

                    sections.append(prompt_manager.get(
                        "chat", "memory_context_section", lang=lang,
                        memories=memories_str
                    ))
        except Exception as e:
            logger.warning(f"Memory retrieval failed: {e}")

    # Hook: retrieve_context — plugins can inject additional context (e.g. graph)
    try:
        hook_results = await run_hooks(
            "retrieve_context", query=content, user_id=user_id, lang=lang
        )
        for r in hook_results:
            if isinstance(r, str) and r.strip():
                sections.append(r)
    except Exception as e:
        logger.warning(f"retrieve_context hook failed: {e}")

    return "\n\n".join(sections)


async def _stream_rag_response(
    content: str,
    knowledge_base_id,
    ollama,
    session_state: "ConversationSessionState",
    websocket: WebSocket,
    memory_context: str = "",
    document_context: str = "",
) -> str:
    """Stream a RAG-enhanced or plain conversation response.

    Handles RAG context lookup, caching, follow-up detection, and fallback
    to plain conversation if no RAG context is found.

    Returns:
        The full response text.
    """
    full_response = ""

    if settings.rag_enabled:
        try:
            from services.rag_service import RAGService

            rag_context = None
            is_followup = is_followup_question(content, session_state.last_query)

            if is_followup and session_state.is_rag_context_valid():
                rag_context = session_state.last_rag_context
                logger.info(f"📚 RAG Follow-up erkannt, nutze gecachten Kontext ({len(rag_context)} Zeichen)")
            else:
                async with AsyncSessionLocal() as db_session:
                    rag_service = RAGService(db_session)

                    logger.info(f"📚 RAG Suche: query='{content[:50]}...', kb_id={knowledge_base_id}, is_followup={is_followup}")

                    search_results = await rag_service.search(
                        query=content,
                        knowledge_base_id=knowledge_base_id
                    )

                    if search_results:
                        rag_context = rag_service.format_context_from_results(search_results)
                        session_state.update_rag_context(
                            context=rag_context,
                            results=search_results,
                            query=content,
                            kb_id=knowledge_base_id
                        )
                        logger.info(f"📚 RAG Kontext gefunden und gecacht ({len(rag_context)} Zeichen)")

            if rag_context:
                await websocket.send_json({
                    "type": "rag_context",
                    "has_context": True,
                    "knowledge_base_id": knowledge_base_id,
                    "is_followup": is_followup
                })

                async for chunk in ollama.chat_stream_with_rag(
                    content,
                    rag_context,
                    history=session_state.conversation_history if is_followup else None,
                    memory_context=memory_context,
                    document_context=document_context,
                ):
                    full_response += chunk
                    await websocket.send_json({"type": "stream", "content": chunk})

                session_state.add_to_history("user", content)
                session_state.add_to_history("assistant", full_response)
                return full_response
            else:
                logger.info("📚 Kein RAG Kontext gefunden, nutze normale Konversation")
                await websocket.send_json({
                    "type": "rag_context",
                    "has_context": False,
                    "knowledge_base_id": knowledge_base_id
                })
        except Exception as e:
            logger.error(f"❌ RAG-Fehler, Fallback zu normaler Konversation: {e}")
            import traceback
            logger.error(traceback.format_exc())

    # Fallback: plain conversation
    async for chunk in ollama.chat_stream(content, history=session_state.conversation_history, memory_context=memory_context, document_context=document_context):
        full_response += chunk
        await websocket.send_json({"type": "stream", "content": chunk})

    return full_response


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str = Query(None, description="Authentication token")
):
    """WebSocket connection for real-time chat."""
    # Get client IP for rate limiting
    ip_address = websocket.client.host if websocket.client else "unknown"

    # Check authentication if enabled
    auth_result = await authenticate_websocket(websocket, token)
    if not auth_result:
        await websocket.close(code=WSAuthError.UNAUTHORIZED, reason="Authentication required")
        return

    await websocket.accept()
    logger.info(f"✅ WebSocket Verbindung hergestellt (IP: {ip_address})")

    # Get rate limiter
    rate_limiter = get_rate_limiter()

    # Initialize session state for conversation persistence
    session_state = ConversationSessionState()

    # Access app state through websocket.app
    app = websocket.app
    ollama = app.state.ollama
    # Try to auto-detect room context from IP address
    room_context = None
    try:
        if ip_address and ip_address != "unknown":
            from services.room_service import RoomService

            async with AsyncSessionLocal() as db_session:
                room_service = RoomService(db_session)
                room_context = await room_service.get_room_context_by_ip(ip_address)

            if room_context:
                logger.info(f"🏠 Auto-detected room from IP {ip_address}: {room_context.get('room_name')} (device: {room_context.get('device_name', room_context.get('device_id'))})")
            else:
                logger.debug(f"📍 No room context for IP {ip_address}")
    except Exception as e:
        logger.warning(f"⚠️ Failed to detect room context: {e}")

    try:
        while True:
            # Receive message
            data = await websocket.receive_json()

            # Rate limiting check
            allowed, reason = rate_limiter.check(ip_address)
            if not allowed:
                await send_ws_error(websocket, WSErrorCode.RATE_LIMITED, reason)
                continue

            # Validate message
            try:
                msg = WSChatMessage(**data)
                message_type = msg.type
                content = msg.content
                msg_session_id = msg.session_id
                use_rag = msg.use_rag
                knowledge_base_id = msg.knowledge_base_id
                attachment_ids = msg.attachment_ids or []
            except ValidationError as e:
                await send_ws_error(websocket, WSErrorCode.INVALID_MESSAGE, str(e))
                continue

            logger.info(f"📨 WebSocket Nachricht: {message_type} - '{content[:100]}' (RAG: {use_rag}, session: {msg_session_id})")

            # Handle session_id for conversation persistence
            if msg_session_id:
                # Load history from DB if this is the first message with this session_id
                if not session_state.history_loaded or session_state.db_session_id != msg_session_id:
                    session_state.db_session_id = msg_session_id
                    register_ws_connection(msg_session_id, websocket)
                    try:
                        async with AsyncSessionLocal() as db_session:
                            db_history = await ollama.load_conversation_context(
                                msg_session_id, db_session, max_messages=10
                            )
                            if db_history:
                                session_state.conversation_history = db_history
                                logger.info(f"📚 Conversation history loaded: {len(db_history)} messages for session {msg_session_id}")
                            session_state.history_loaded = True
                    except Exception as e:
                        logger.warning(f"⚠️ Failed to load conversation history: {e}")

            # Retrieve user info and permissions
            user_id = auth_result.get("user_id") if isinstance(auth_result, dict) else None
            user_permissions = None
            if user_id is not None:
                try:
                    from sqlalchemy import select

                    from models.database import User
                    async with AsyncSessionLocal() as db_session:
                        result = await db_session.execute(
                            select(User).where(User.id == int(user_id))
                        )
                        user_obj = result.scalar_one_or_none()
                        if user_obj:
                            user_permissions = user_obj.get_permissions()
                except Exception as e:
                    logger.warning(f"⚠️ Failed to load user permissions: {e}")

            # Register voice/auth presence if user is authenticated in a known room
            if (user_id and settings.presence_enabled
                    and room_context and room_context.get("room_id")):
                try:
                    from services.presence_service import get_presence_service
                    presence_svc = get_presence_service()
                    await presence_svc.register_voice_presence(
                        user_id=int(user_id),
                        room_id=room_context["room_id"],
                        room_name=room_context.get("room_name"),
                    )
                except Exception as e:
                    logger.warning(f"⚠️ Auth presence update failed: {e}")

            # Retrieve memory context (long-term user knowledge)
            memory_context = await _retrieve_memory_context(
                content, user_id=user_id, lang=ollama.default_lang
            )

            # Retrieve document context from uploaded attachments
            document_context = ""
            if attachment_ids:
                document_context = await _fetch_document_context(
                    attachment_ids, lang=ollama.default_lang
                )

            # === Unified Router / Legacy Dual-Path ===
            agent_used = False
            full_response = ""
            intent = None
            action_result = None
            agent_steps_count = 0

            # Get router from app state (initialized at startup if agent_enabled)
            agent_router = getattr(app.state, 'agent_router', None)

            if settings.agent_enabled and agent_router:
                # === Unified Router Path ===
                # Every message goes through router → specialized agent
                from services.action_executor import ActionExecutor
                from services.agent_service import AgentService, step_to_ws_message
                from services.agent_tools import AgentToolRegistry

                mcp_manager = getattr(app.state, 'mcp_manager', None)

                role = await agent_router.classify(
                    content, ollama,
                    conversation_history=session_state.conversation_history if session_state.conversation_history else None,
                    lang=ollama.default_lang,
                )
                logger.info(f"🎯 Router: '{content[:60]}...' → {role.name}")

                if role.name == "conversation":
                    # Direct LLM response — no tools, no agent loop
                    intent = {"intent": "general.conversation", "parameters": {}, "confidence": 1.0}

                    if use_rag and settings.rag_enabled:
                        # RAG-enhanced conversation
                        full_response = await _stream_rag_response(
                            content, knowledge_base_id, ollama, session_state, websocket,
                            memory_context=memory_context,
                            document_context=document_context,
                        )
                    else:
                        async for chunk in ollama.chat_stream(content, history=session_state.conversation_history, memory_context=memory_context, document_context=document_context):
                            full_response += chunk
                            await websocket.send_json({"type": "stream", "content": chunk})

                elif role.name == "knowledge":
                    # RAG search → LLM response (dedicated knowledge base path)
                    intent = {"intent": "knowledge.ask", "parameters": {}, "confidence": 1.0}

                    full_response = await _stream_rag_response(
                        content, knowledge_base_id, ollama, session_state, websocket,
                        memory_context=memory_context,
                        document_context=document_context,
                    )

                else:
                    # Specialized agent loop (smart_home, documents, media, research, workflow, general)
                    agent_used = True
                    tool_registry = AgentToolRegistry(
                        mcp_manager=mcp_manager,
                        server_filter=role.mcp_servers,
                        internal_filter=role.internal_tools,
                    )
                    agent = AgentService(tool_registry, role=role)
                    executor = ActionExecutor(mcp_manager=mcp_manager)

                    agent_tool_results = []
                    async for step in agent.run(
                        message=content,
                        ollama=ollama,
                        executor=executor,
                        conversation_history=session_state.conversation_history if session_state.conversation_history else None,
                        room_context=room_context,
                        memory_context=memory_context,
                        document_context=document_context,
                        user_permissions=user_permissions,
                        user_id=user_id,
                    ):
                        ws_msg = step_to_ws_message(step)
                        await websocket.send_json(ws_msg)

                        if step.step_type == "final_answer":
                            full_response = step.content
                        if step.step_type == "tool_result" and step.success and step.data:
                            agent_tool_results.append((step.tool, step.data))
                        if step.step_type in ("tool_call", "tool_result"):
                            agent_steps_count += 1

                    # Build action summary from agent tool results for conversation history
                    if agent_tool_results:
                        action_result = _build_agent_action_result(agent_tool_results)

                    logger.info(f"🤖 Agent [{role.name}] abgeschlossen: {agent_steps_count} Steps")

                    # Set intent info so frontend can show correction button
                    if not intent:
                        intent = {"intent": f"agent.{role.name}", "confidence": 1.0, "parameters": {}}

            else:
                # === Legacy Ranked Intent Path (agent_enabled=false or no router) ===
                logger.info("🔍 Extrahiere Ranked Intents...")
                ranked_intents = await ollama.extract_ranked_intents(
                    content,
                    room_context=room_context,
                    conversation_history=session_state.conversation_history if session_state.conversation_history else None
                )

                from services.action_executor import ActionExecutor
                mcp_mgr = getattr(app.state, 'mcp_manager', None)
                intent_used = None

                for intent_candidate in ranked_intents:
                    intent_name = intent_candidate.get("intent", "general.conversation")
                    logger.info(f"🎯 Versuche Intent: {intent_name} (confidence: {intent_candidate.get('confidence', 0):.2f})")

                    if intent_name == "general.unresolved":
                        logger.info("⏭️ Intent unresolved, skipping...")
                        continue

                    if intent_name == "general.conversation":
                        intent = intent_candidate
                        intent_used = intent_candidate
                        break

                    executor = ActionExecutor(mcp_manager=mcp_mgr)
                    candidate_result = await executor.execute(
                        intent_candidate, user_permissions=user_permissions,
                        user_id=user_id,
                    )

                    if candidate_result.get("success") and not candidate_result.get("empty_result"):
                        intent = intent_candidate
                        action_result = candidate_result
                        intent_used = intent_candidate
                        logger.info(f"✅ Intent {intent_name} erfolgreich: {candidate_result.get('message', '')[:80]}")

                        session_state.update_action_context(intent, action_result)

                        # Sanitize credentials before sending to frontend
                        from services.mcp_client import _sanitize_credentials
                        sanitized_result = dict(action_result)
                        if isinstance(sanitized_result.get("message"), str):
                            sanitized_result["message"] = _sanitize_credentials(sanitized_result["message"])

                        await websocket.send_json({
                            "type": "action",
                            "intent": intent,
                            "result": sanitized_result
                        })
                        break

                    logger.info(f"⏭️ Intent {intent_name} lieferte kein Ergebnis, versuche nächsten...")

                if not intent_used:
                    logger.info("💬 Alle Intents fehlgeschlagen, Fallback zu Konversation")
                    intent = {"intent": "general.conversation", "parameters": {}, "confidence": 1.0}
                    intent_used = intent

                # Generate response for legacy path
                if not agent_used:
                    if action_result and action_result.get("success"):
                        from services.mcp_client import _sanitize_credentials
                        result_info = _sanitize_credentials(action_result.get('message', ''))

                        if action_result.get('data'):
                            import json
                            data_str = json.dumps(action_result['data'], ensure_ascii=False, indent=2)
                            result_info = f"{result_info}\n\nDaten:\n{_sanitize_credentials(data_str)}"

                        enhanced_prompt = f"""Der Nutzer hat gefragt: "{content}"

Die Aktion wurde ausgeführt:
{result_info}

Gib eine kurze, natürliche Antwort basierend auf den Daten.
WICHTIG: Nutze die ECHTEN Daten aus dem Ergebnis! Gib NUR die Antwort, KEIN JSON!"""

                        async for chunk in ollama.chat_stream(enhanced_prompt, history=session_state.conversation_history, memory_context=memory_context, document_context=document_context):
                            full_response += chunk
                            await websocket.send_json({"type": "stream", "content": chunk})

                    elif action_result and not action_result.get("success"):
                        full_response = f"Entschuldigung, das konnte ich nicht ausführen: {action_result.get('message')}"
                        await websocket.send_json({"type": "stream", "content": full_response})

                    else:
                        if use_rag and settings.rag_enabled:
                            full_response = await _stream_rag_response(
                                content, knowledge_base_id, ollama, session_state, websocket,
                                memory_context=memory_context,
                                document_context=document_context,
                            )
                        else:
                            async for chunk in ollama.chat_stream(content, history=session_state.conversation_history, memory_context=memory_context, document_context=document_context):
                                full_response += chunk
                                await websocket.send_json({"type": "stream", "content": chunk})

            # Update conversation history with this exchange (in-memory)
            # Enrich assistant message with action result context for follow-up resolution.
            # Action summary goes FIRST so it survives truncation in conv_context (500-2000 chars).
            history_content = full_response
            if action_result and action_result.get("success") and action_result.get("data"):
                # Use agent's tool name if available (from _build_agent_action_result)
                summary_intent = intent
                if action_result.get("_agent_intent"):
                    summary_intent = {"intent": action_result["_agent_intent"]}
                action_summary = _build_action_summary(summary_intent, action_result)
                if action_summary:
                    history_content = f"[Aktionsergebnis — Verwende diese Daten für Folgeanfragen (IDs, Titel, etc.):\n{action_summary}]\n\n{full_response}"

            session_state.add_to_history("user", content)
            if full_response:
                session_state.add_to_history("assistant", history_content)

            # Persist messages to DB if session_id is provided
            if msg_session_id and full_response:
                try:
                    async with AsyncSessionLocal() as db_session:
                        # Save user message
                        user_metadata = {}
                        if room_context:
                            user_metadata["room_context"] = room_context
                        if attachment_ids:
                            user_metadata["attachment_ids"] = attachment_ids
                        await ollama.save_message(
                            msg_session_id, "user", content, db_session,
                            metadata=user_metadata if user_metadata else None
                        )
                        # Save assistant response (with action context for follow-ups)
                        await ollama.save_message(
                            msg_session_id, "assistant", history_content, db_session,
                            metadata={
                                "intent": intent.get("intent") if intent else None,
                                "action_success": action_result.get("success") if action_result else None
                            }
                        )
                        logger.debug(f"💾 Messages saved to DB: session_id={msg_session_id}")
                except Exception as e:
                    logger.warning(f"⚠️ Failed to save messages to DB: {e}")

            # Check for output routing if we have room context
            tts_handled_by_server = False
            if room_context and room_context.get("room_id") and full_response:
                tts_handled_by_server = await _route_chat_tts_output(
                    room_context, full_response, websocket
                )

            # Stream finished - tell frontend if TTS was handled server-side
            done_msg = {
                "type": "done",
                "tts_handled": tts_handled_by_server
            }
            if agent_used:
                done_msg["agent_steps"] = agent_steps_count
            # Include intent info for frontend feedback UI
            if intent:
                done_msg["intent"] = {
                    "intent": intent.get("intent"),
                    "confidence": intent.get("confidence", 0),
                }
            await websocket.send_json(done_msg)

            # Proactive feedback: ask user when action failed or returned empty
            should_request_feedback = False
            if intent and intent.get("intent") != "general.conversation":
                if action_result and (
                    not action_result.get("success") or action_result.get("empty_result")
                ):
                    should_request_feedback = True

            if should_request_feedback:
                await websocket.send_json({
                    "type": "intent_feedback_request",
                    "message_text": content,
                    "detected_intent": intent.get("intent"),
                    "confidence": intent.get("confidence", 0),
                    "feedback_type": "intent",
                })
                logger.info(f"📝 Proactive feedback requested for intent: {intent.get('intent')}")

            # Background: Extract memories from this exchange
            if settings.memory_enabled and settings.memory_extraction_enabled and full_response:
                task = asyncio.create_task(
                    _extract_memories_background(
                        user_message=content,
                        assistant_response=full_response,
                        user_id=user_id,
                        session_id=msg_session_id,
                        lang=ollama.default_lang,
                    )
                )
                _background_tasks.add(task)
                task.add_done_callback(_background_tasks.discard)

            # Hook: post_message (fire-and-forget for plugins like renfield-twin)
            from utils.hooks import run_hooks
            _pm_task = asyncio.create_task(run_hooks(
                "post_message",
                user_msg=content,
                assistant_msg=full_response,
                user_id=user_id,
                session_id=msg_session_id,
            ))
            _background_tasks.add(_pm_task)
            _pm_task.add_done_callback(_background_tasks.discard)

            logger.info(f"✅ WebSocket Response gesendet (tts_handled={tts_handled_by_server})")

    except WebSocketDisconnect:
        if session_state.db_session_id:
            unregister_ws_connection(session_state.db_session_id)
        logger.info("👋 WebSocket Verbindung getrennt")
    except Exception as e:
        if session_state.db_session_id:
            unregister_ws_connection(session_state.db_session_id)
        logger.error(f"❌ WebSocket Fehler: {e}")
        import traceback
        logger.error(traceback.format_exc())
        await websocket.close()
