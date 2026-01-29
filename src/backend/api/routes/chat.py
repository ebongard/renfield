"""
Chat API Routes
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from typing import List, Optional
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from services.database import get_db
from services.ollama_service import OllamaService
from services.auth_service import get_current_user
from services.api_rate_limiter import limiter
from utils.config import settings
from models.database import Conversation, Message, User
from datetime import datetime
import uuid

router = APIRouter()

class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    context: Optional[List[dict]] = None

class ChatResponse(BaseModel):
    message: str
    session_id: str
    intent: Optional[dict] = None

@router.post("/send", response_model=ChatResponse)
@limiter.limit(settings.api_rate_limit_chat)
async def send_message(
    request: Request,
    chat_request: ChatRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user)
):
    """Nachricht senden und Antwort erhalten"""
    try:
        logger.info(f"📨 Neue Nachricht: '{chat_request.message[:100]}'")

        # Session ID generieren falls nicht vorhanden
        session_id = chat_request.session_id or str(uuid.uuid4())

        # Conversation in DB speichern/laden
        result = await db.execute(
            select(Conversation).where(Conversation.session_id == session_id)
        )
        conversation = result.scalar_one_or_none()

        if not conversation:
            conversation = Conversation(session_id=session_id)
            db.add(conversation)
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

        # === Agent Loop Check ===
        if settings.agent_enabled:
            from services.complexity_detector import ComplexityDetector
            if ComplexityDetector.needs_agent(chat_request.message):
                logger.info(f"🤖 Agent Loop (REST) aktiviert für: '{chat_request.message[:80]}...'")

                from services.agent_tools import AgentToolRegistry
                from services.agent_service import AgentService
                from services.action_executor import ActionExecutor

                mcp_manager = getattr(app.state, 'mcp_manager', None)
                tool_registry = AgentToolRegistry(plugin_registry=app.state.plugin_registry, mcp_manager=mcp_manager)
                agent = AgentService(tool_registry)
                executor = ActionExecutor(plugin_registry=app.state.plugin_registry, mcp_manager=mcp_manager)

                async for step in agent.run(
                    message=chat_request.message,
                    ollama=ollama,
                    executor=executor,
                ):
                    if step.step_type == "final_answer":
                        response_text = step.content

                if not response_text:
                    response_text = "Entschuldigung, ich konnte die Anfrage nicht bearbeiten."

                # Save and return (skip single-intent path)
                assistant_msg = Message(
                    conversation_id=conversation.id,
                    role="assistant",
                    content=response_text,
                    message_metadata={"agent": True}
                )
                db.add(assistant_msg)
                conversation.updated_at = datetime.utcnow()
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

            executor = ActionExecutor(plugin_registry=app.state.plugin_registry, mcp_manager=mcp_mgr)
            candidate_result = await executor.execute(intent_candidate, user=current_user)

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

            response_text = await ollama.chat(enhanced_prompt, context=[])

        elif action_result and not action_result.get("success"):
            response_text = f"Entschuldigung, das konnte ich nicht ausführen: {action_result.get('message')}"

        else:
            response_text = await ollama.chat(chat_request.message, context)
        
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
        conversation.updated_at = datetime.utcnow()
        
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
    db: AsyncSession = Depends(get_db)
):
    """Chat-Historie abrufen"""
    try:
        result = await db.execute(
            select(Conversation).where(Conversation.session_id == session_id)
        )
        conversation = result.scalar_one_or_none()
        
        if not conversation:
            return {"messages": []}
        
        result = await db.execute(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.timestamp.asc())
            .limit(limit)
        )
        messages = result.scalars().all()
        
        return {
            "messages": [
                {
                    "role": msg.role,
                    "content": msg.content,
                    "timestamp": msg.timestamp.isoformat(),
                    "metadata": msg.message_metadata  # Spalte heißt message_metadata
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
    db: AsyncSession = Depends(get_db)
):
    """Chat-Session löschen"""
    try:
        result = await db.execute(
            select(Conversation).where(Conversation.session_id == session_id)
        )
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
    db: AsyncSession = Depends(get_db)
):
    """Liste aller Konversationen"""
    try:
        from main import app
        ollama: OllamaService = app.state.ollama

        conversations = await ollama.get_all_conversations(db, limit, offset)

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
    db: AsyncSession = Depends(get_db)
):
    """Zusammenfassung einer Konversation"""
    try:
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
    db: AsyncSession = Depends(get_db)
):
    """Suche in Konversationen"""
    try:
        if not q or len(q) < 2:
            raise HTTPException(status_code=400, detail="Suchanfrage muss mindestens 2 Zeichen lang sein")

        from main import app
        ollama: OllamaService = app.state.ollama

        results = await ollama.search_conversations(q, db, limit)

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

@router.get("/stats")
async def get_conversation_stats(
    db: AsyncSession = Depends(get_db)
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
        yesterday = datetime.utcnow() - timedelta(days=1)
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
    db: AsyncSession = Depends(get_db)
):
    """Lösche alte Konversationen (älter als X Tage)"""
    try:
        from datetime import timedelta
        cutoff_date = datetime.utcnow() - timedelta(days=days)

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
