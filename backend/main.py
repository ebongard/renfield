"""
Renfield - Persönlicher KI-Assistent
Hauptanwendung mit FastAPI
"""
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from contextlib import asynccontextmanager
from loguru import logger
from datetime import datetime
import os
import sys
from typing import Optional

# Logging konfigurieren
logger.remove()
logger.add(sys.stderr, level=os.getenv("LOG_LEVEL", "INFO"))

# Lokale Imports
from api.routes import chat, tasks, voice, camera, homeassistant as ha_routes
from services.database import init_db
from services.ollama_service import OllamaService
from services.task_queue import TaskQueue
from utils.config import settings

# Lifecycle Management
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup und Shutdown Events"""
    logger.info("🚀 Renfield startet...")
    
    # Datenbank initialisieren
    await init_db()
    logger.info("✅ Datenbank initialisiert")
    
    # Ollama Service starten
    ollama = OllamaService()
    await ollama.ensure_model_loaded()
    app.state.ollama = ollama
    logger.info("✅ Ollama Service bereit")
    
    # Task Queue initialisieren
    task_queue = TaskQueue()
    app.state.task_queue = task_queue
    logger.info("✅ Task Queue bereit")

    # Plugin System (NEW)
    if settings.plugins_enabled:
        try:
            from integrations.core.plugin_loader import PluginLoader
            from integrations.core.plugin_registry import PluginRegistry

            loader = PluginLoader(settings.plugins_dir)
            plugins = loader.load_all_plugins()

            plugin_registry = PluginRegistry()
            plugin_registry.register_plugins(plugins)

            app.state.plugin_registry = plugin_registry
            logger.info(f"✅ Plugin System bereit: {len(plugins)} plugins geladen")
        except Exception as e:
            logger.error(f"❌ Plugin System konnte nicht geladen werden: {e}")
            app.state.plugin_registry = None
    else:
        app.state.plugin_registry = None
        logger.info("⏭️  Plugin System deaktiviert")

    # Whisper Service vorladen (für STT)
    try:
        from services.whisper_service import WhisperService
        import asyncio
        
        async def preload_whisper():
            """Lade Whisper-Modell im Hintergrund"""
            try:
                whisper_service = WhisperService()
                whisper_service.load_model()
                logger.info("✅ Whisper Service bereit (STT aktiviert)")
            except Exception as e:
                logger.warning(f"⚠️  Whisper konnte nicht vorgeladen werden: {e}")
                logger.warning("💡 Spracheingabe wird beim ersten Gebrauch geladen")
        
        # Starte im Hintergrund
        asyncio.create_task(preload_whisper())
    except Exception as e:
        logger.warning(f"⚠️  Whisper-Preloading fehlgeschlagen: {e}")
    
    # Home Assistant Keywords vorladen (optional, im Hintergrund)
    try:
        from integrations.homeassistant import HomeAssistantClient
        import asyncio
        
        async def preload_keywords():
            """Lade HA Keywords im Hintergrund"""
            try:
                ha_client = HomeAssistantClient()
                keywords = await ha_client.get_keywords()
                logger.info(f"✅ Home Assistant Keywords vorgeladen: {len(keywords)} Keywords")
            except Exception as e:
                logger.warning(f"⚠️  Keywords konnten nicht vorgeladen werden: {e}")
        
        # Starte im Hintergrund (blockiert Start nicht)
        asyncio.create_task(preload_keywords())
    except Exception as e:
        logger.warning(f"⚠️  Keyword-Preloading fehlgeschlagen: {e}")
    
    yield
    
    # Cleanup
    logger.info("👋 Renfield wird heruntergefahren...")

# FastAPI App erstellen
app = FastAPI(
    title="Renfield AI Assistant",
    description="Vollständig offline-fähiger persönlicher KI-Assistent",
    version="1.0.0",
    lifespan=lifespan
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In Produktion einschränken
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Router einbinden
app.include_router(chat.router, prefix="/api/chat", tags=["Chat"])
app.include_router(tasks.router, prefix="/api/tasks", tags=["Tasks"])
app.include_router(voice.router, prefix="/api/voice", tags=["Voice"])
app.include_router(camera.router, prefix="/api/camera", tags=["Camera"])
app.include_router(ha_routes.router, prefix="/api/homeassistant", tags=["Home Assistant"])

# WebSocket für Echtzeit-Chat
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket Verbindung für Echtzeit-Chat"""
    await websocket.accept()
    logger.info("✅ WebSocket Verbindung hergestellt")
    
    try:
        while True:
            # Nachricht empfangen
            data = await websocket.receive_json()
            message_type = data.get("type", "text")
            content = data.get("content", "")
            
            logger.info(f"📨 WebSocket Nachricht: {message_type} - '{content[:100]}'")
            
            # Ollama Service
            ollama = app.state.ollama
            plugin_registry = app.state.plugin_registry

            # Intent extrahieren
            logger.info("🔍 Extrahiere Intent...")
            intent = await ollama.extract_intent(content, plugin_registry)
            logger.info(f"🎯 Intent erkannt: {intent.get('intent')} | Entity: {intent.get('parameters', {}).get('entity_id', 'none')}")
            
            # Action ausführen falls nötig
            action_result = None
            if intent.get("intent") != "general.conversation":
                logger.info(f"⚡ Führe Aktion aus: {intent.get('intent')}")
                from services.action_executor import ActionExecutor
                executor = ActionExecutor(plugin_registry)
                action_result = await executor.execute(intent)
                logger.info(f"✅ Aktion: {action_result.get('success')} - {action_result.get('message')}")
                
                # Sende Action-Ergebnis an Frontend
                await websocket.send_json({
                    "type": "action",
                    "intent": intent,
                    "result": action_result
                })
            
            # Response generieren
            if action_result and action_result.get("success"):
                # Erfolgreiche Aktion - nutze Ergebnis
                result_info = action_result.get('message', '')

                # Füge Daten hinzu, falls vorhanden
                if action_result.get('data'):
                    import json
                    data_str = json.dumps(action_result['data'], ensure_ascii=False, indent=2)
                    result_info = f"{result_info}\n\nDaten:\n{data_str}"

                enhanced_prompt = f"""Der Nutzer hat gefragt: "{content}"

Die Aktion wurde ausgeführt:
{result_info}

Gib eine kurze, natürliche Antwort basierend auf den Daten.
WICHTIG: Nutze die ECHTEN Daten aus dem Ergebnis! Gib NUR die Antwort, KEIN JSON!"""
                
                # Stream die Antwort
                async for chunk in ollama.chat_stream(enhanced_prompt):
                    await websocket.send_json({
                        "type": "stream",
                        "content": chunk
                    })
            
            elif action_result and not action_result.get("success"):
                # Aktion fehlgeschlagen
                error_message = f"Entschuldigung, das konnte ich nicht ausführen: {action_result.get('message')}"
                await websocket.send_json({
                    "type": "stream",
                    "content": error_message
                })
            
            else:
                # Normale Konversation
                async for chunk in ollama.chat_stream(content):
                    await websocket.send_json({
                        "type": "stream",
                        "content": chunk
                    })
            
            # Stream beendet
            await websocket.send_json({
                "type": "done"
            })
            
            logger.info("✅ WebSocket Response gesendet")
            
    except WebSocketDisconnect:
        logger.info("👋 WebSocket Verbindung getrennt")
    except Exception as e:
        logger.error(f"❌ WebSocket Fehler: {e}")
        import traceback
        logger.error(traceback.format_exc())
        await websocket.close()

# Health Check
@app.get("/health")
async def health_check():
    """System Health Check"""
    return {
        "status": "healthy",
        "services": {
            "ollama": "ok",
            "database": "ok",
            "redis": "ok"
        }
    }

# Admin Endpoint: Refresh HA Keywords
@app.post("/admin/refresh-keywords")
async def refresh_keywords():
    """
    Lade Home Assistant Keywords neu
    
    Nützlich nach dem Hinzufügen neuer Geräte in HA
    """
    try:
        from integrations.homeassistant import HomeAssistantClient
        ha_client = HomeAssistantClient()
        keywords = await ha_client.get_keywords(refresh=True)
        
        return {
            "status": "success",
            "keywords_count": len(keywords),
            "sample_keywords": list(keywords)[:20]
        }
    except Exception as e:
        logger.error(f"❌ Keyword Refresh Fehler: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Debug Endpoint: Test Intent Extraction
@app.post("/debug/intent")
async def debug_intent(message: str):
    """
    Teste Intent-Extraction für eine Nachricht
    
    Nützlich zum Debuggen von Intent-Erkennungsproblemen
    """
    try:
        ollama: OllamaService = app.state.ollama
        intent = await ollama.extract_intent(message)
        
        return {
            "message": message,
            "intent": intent,
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        logger.error(f"❌ Intent Debug Fehler: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Root Endpoint
@app.get("/")
async def root():
    """API Root"""
    return {
        "name": "Renfield AI Assistant",
        "version": "1.0.0",
        "status": "online",
        "docs": "/docs"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )
