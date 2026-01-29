"""
WebSocket handler for real-time chat (/ws endpoint).

This module handles:
- Real-time chat with streaming responses
- Intent extraction and action execution
- RAG (Retrieval-Augmented Generation) support
- Conversation persistence
- Room context auto-detection
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from pydantic import ValidationError
from loguru import logger

from services.database import AsyncSessionLocal
from services.websocket_auth import authenticate_websocket, WSAuthError
from services.websocket_rate_limiter import get_rate_limiter
from models.websocket_messages import WSChatMessage, WSErrorCode
from utils.config import settings

from .shared import (
    ConversationSessionState,
    is_followup_question,
    send_ws_error,
)

router = APIRouter()


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
        from services.output_routing_service import OutputRoutingService
        from services.audio_output_service import get_audio_output_service

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
                from services.piper_service import PiperService
                piper = PiperService()
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
    plugin_registry = app.state.plugin_registry

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
                request_id = msg.request_id
                use_rag = msg.use_rag
                knowledge_base_id = msg.knowledge_base_id
            except ValidationError as e:
                await send_ws_error(websocket, WSErrorCode.INVALID_MESSAGE, str(e))
                continue

            logger.info(f"📨 WebSocket Nachricht: {message_type} - '{content[:100]}' (RAG: {use_rag}, session: {msg_session_id})")

            # Handle session_id for conversation persistence
            if msg_session_id:
                # Load history from DB if this is the first message with this session_id
                if not session_state.history_loaded or session_state.db_session_id != msg_session_id:
                    session_state.db_session_id = msg_session_id
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

            # === Agent Loop Check ===
            # If agent is enabled and message is complex, use the Agent Loop
            agent_used = False
            full_response = ""
            intent = None
            action_result = None
            agent_steps_count = 0

            if settings.agent_enabled:
                from services.complexity_detector import ComplexityDetector
                if await ComplexityDetector.needs_agent_with_feedback(content):
                    agent_used = True
                    matched_patterns = ComplexityDetector.detect_patterns(content)
                    logger.info(f"🤖 Agent Loop aktiviert für: '{content[:80]}...' (patterns: {matched_patterns})")

                    from services.agent_tools import AgentToolRegistry
                    from services.agent_service import AgentService, step_to_ws_message
                    from services.action_executor import ActionExecutor

                    mcp_manager = getattr(app.state, 'mcp_manager', None)
                    tool_registry = AgentToolRegistry(plugin_registry=plugin_registry, mcp_manager=mcp_manager)
                    agent = AgentService(tool_registry)
                    executor = ActionExecutor(plugin_registry, mcp_manager=mcp_manager)

                    async for step in agent.run(
                        message=content,
                        ollama=ollama,
                        executor=executor,
                        conversation_history=session_state.conversation_history if session_state.conversation_history else None,
                        room_context=room_context,
                    ):
                        ws_msg = step_to_ws_message(step)
                        await websocket.send_json(ws_msg)

                        if step.step_type == "final_answer":
                            full_response = step.content
                        if step.step_type in ("tool_call", "tool_result"):
                            agent_steps_count += 1

                    logger.info(f"🤖 Agent Loop abgeschlossen: {agent_steps_count} Steps")

            if not agent_used:
                # === Ranked Intent Path with Fallback Chain ===
                logger.info("🔍 Extrahiere Ranked Intents...")
                ranked_intents = await ollama.extract_ranked_intents(
                    content,
                    plugin_registry,
                    room_context=room_context,
                    conversation_history=session_state.conversation_history if session_state.conversation_history else None
                )

                from services.action_executor import ActionExecutor
                mcp_mgr = getattr(app.state, 'mcp_manager', None)
                intent_used = None

                for intent_candidate in ranked_intents:
                    intent_name = intent_candidate.get("intent", "general.conversation")
                    logger.info(f"🎯 Versuche Intent: {intent_name} (confidence: {intent_candidate.get('confidence', 0):.2f})")

                    if intent_name == "general.conversation":
                        # Conversation intent — stream directly (optionally with RAG)
                        intent = intent_candidate
                        intent_used = intent_candidate
                        break

                    # Execute action for this intent
                    executor = ActionExecutor(plugin_registry, mcp_manager=mcp_mgr)
                    candidate_result = await executor.execute(intent_candidate)

                    # Check if result is usable (success AND not empty)
                    if candidate_result.get("success") and not candidate_result.get("empty_result"):
                        intent = intent_candidate
                        action_result = candidate_result
                        intent_used = intent_candidate
                        logger.info(f"✅ Intent {intent_name} erfolgreich: {candidate_result.get('message', '')[:80]}")

                        # Update action context for pronoun resolution
                        session_state.update_action_context(intent, action_result)

                        # Send action result to frontend
                        await websocket.send_json({
                            "type": "action",
                            "intent": intent,
                            "result": action_result
                        })
                        break

                    # Intent produced empty/failed result — try next
                    logger.info(f"⏭️ Intent {intent_name} lieferte kein Ergebnis, versuche nächsten...")

                # If no ranked intent worked, try Agent Loop or fall back to conversation
                if not intent_used:
                    if settings.agent_enabled:
                        logger.info("🤖 Alle Ranked Intents fehlgeschlagen, starte Agent Loop als Fallback...")
                        from services.agent_tools import AgentToolRegistry
                        from services.agent_service import AgentService, step_to_ws_message

                        agent_used = True
                        tool_registry = AgentToolRegistry(plugin_registry=plugin_registry, mcp_manager=mcp_mgr)
                        agent = AgentService(tool_registry)
                        fallback_executor = ActionExecutor(plugin_registry, mcp_manager=mcp_mgr)

                        async for step in agent.run(
                            message=content,
                            ollama=ollama,
                            executor=fallback_executor,
                            conversation_history=session_state.conversation_history if session_state.conversation_history else None,
                            room_context=room_context,
                        ):
                            ws_msg = step_to_ws_message(step)
                            await websocket.send_json(ws_msg)

                            if step.step_type == "final_answer":
                                full_response = step.content
                            if step.step_type in ("tool_call", "tool_result"):
                                agent_steps_count += 1

                        logger.info(f"🤖 Agent Fallback abgeschlossen: {agent_steps_count} Steps")
                    else:
                        # Absolute fallback: general.conversation
                        logger.info("💬 Alle Intents fehlgeschlagen, Fallback zu Konversation")
                        intent = {"intent": "general.conversation", "parameters": {}, "confidence": 1.0}
                        intent_used = intent

                # Generate response (only if agent didn't already produce one)
                if not agent_used or (agent_used and not full_response):
                    if action_result and action_result.get("success"):
                        # Successful action — generate response from result
                        result_info = action_result.get('message', '')

                        if action_result.get('data'):
                            import json
                            data_str = json.dumps(action_result['data'], ensure_ascii=False, indent=2)
                            result_info = f"{result_info}\n\nDaten:\n{data_str}"

                        enhanced_prompt = f"""Der Nutzer hat gefragt: "{content}"

Die Aktion wurde ausgeführt:
{result_info}

Gib eine kurze, natürliche Antwort basierend auf den Daten.
WICHTIG: Nutze die ECHTEN Daten aus dem Ergebnis! Gib NUR die Antwort, KEIN JSON!"""

                        async for chunk in ollama.chat_stream(enhanced_prompt, history=session_state.conversation_history):
                            full_response += chunk
                            await websocket.send_json({
                                "type": "stream",
                                "content": chunk
                            })

                    elif action_result and not action_result.get("success"):
                        # Action failed
                        full_response = f"Entschuldigung, das konnte ich nicht ausführen: {action_result.get('message')}"
                        await websocket.send_json({
                            "type": "stream",
                            "content": full_response
                        })

                    else:
                        # Normal conversation (optionally with RAG)
                        if use_rag and settings.rag_enabled:
                            # RAG-enhanced conversation with context persistence
                            try:
                                from services.rag_service import RAGService

                                rag_context = None
                                is_followup = is_followup_question(content, session_state.last_query)

                                # Check if this is a follow-up question and we have valid cached context
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
                                            rag_context = await rag_service.get_context(
                                                query=content,
                                                knowledge_base_id=knowledge_base_id
                                            )
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
                                        history=session_state.conversation_history if is_followup else None
                                    ):
                                        full_response += chunk
                                        await websocket.send_json({
                                            "type": "stream",
                                            "content": chunk
                                        })

                                    session_state.add_to_history("user", content)
                                    session_state.add_to_history("assistant", full_response)
                                else:
                                    logger.info("📚 Kein RAG Kontext gefunden, nutze normale Konversation")
                                    await websocket.send_json({
                                        "type": "rag_context",
                                        "has_context": False,
                                        "knowledge_base_id": knowledge_base_id
                                    })
                                    async for chunk in ollama.chat_stream(content, history=session_state.conversation_history):
                                        full_response += chunk
                                        await websocket.send_json({
                                            "type": "stream",
                                            "content": chunk
                                        })
                            except Exception as e:
                                logger.error(f"❌ RAG-Fehler, Fallback zu normaler Konversation: {e}")
                                import traceback
                                logger.error(traceback.format_exc())
                                async for chunk in ollama.chat_stream(content, history=session_state.conversation_history):
                                    full_response += chunk
                                    await websocket.send_json({
                                        "type": "stream",
                                        "content": chunk
                                    })
                        else:
                            # Standard conversation without RAG
                            async for chunk in ollama.chat_stream(content, history=session_state.conversation_history):
                                full_response += chunk
                                await websocket.send_json({
                                    "type": "stream",
                                    "content": chunk
                                })

            # Update conversation history with this exchange (in-memory)
            session_state.add_to_history("user", content)
            if full_response:
                session_state.add_to_history("assistant", full_response)

            # Persist messages to DB if session_id is provided
            if msg_session_id and full_response:
                try:
                    async with AsyncSessionLocal() as db_session:
                        # Save user message
                        await ollama.save_message(
                            msg_session_id, "user", content, db_session,
                            metadata={"room_context": room_context} if room_context else None
                        )
                        # Save assistant response
                        await ollama.save_message(
                            msg_session_id, "assistant", full_response, db_session,
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

            logger.info(f"✅ WebSocket Response gesendet (tts_handled={tts_handled_by_server})")

    except WebSocketDisconnect:
        logger.info("👋 WebSocket Verbindung getrennt")
    except Exception as e:
        logger.error(f"❌ WebSocket Fehler: {e}")
        import traceback
        logger.error(traceback.format_exc())
        await websocket.close()
