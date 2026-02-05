"""
Renfield - Persönlicher KI-Assistent
Hauptanwendung mit FastAPI
"""
import os
import sys
from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware

# Logging konfigurieren
logger.remove()
logger.add(sys.stderr, level=os.getenv("LOG_LEVEL", "INFO"))

# Lokale Imports
from api.lifecycle import lifespan
from api.routes import (
    auth,
    camera,
    chat,
    feedback,
    intents,
    knowledge,
    memory,
    notifications,
    plugins,
    preferences,
    roles,
    rooms,
    satellites,
    speakers,
    tasks,
    users,
    voice,
)
from api.routes import homeassistant as ha_routes
from api.routes import mcp as mcp_routes
from api.routes import settings as settings_routes
from api.websocket import chat_router, device_router, satellite_router
from models.permissions import Permission
from services.api_rate_limiter import setup_rate_limiter
from services.auth_service import require_permission
from services.database import AsyncSessionLocal
from services.device_manager import get_device_manager
from services.ollama_service import OllamaService
from services.websocket_auth import get_token_store
from utils.config import settings
from utils.metrics import setup_metrics

# FastAPI App erstellen
app = FastAPI(
    title="Renfield AI Assistant",
    description="Vollständig offline-fähiger persönlicher KI-Assistent",
    version="1.0.0",
    lifespan=lifespan
)

# Security Headers Middleware (OWASP recommendations)
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to all responses (OWASP best practices)."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        # Prevent MIME type sniffing
        response.headers["X-Content-Type-Options"] = "nosniff"

        # Prevent clickjacking
        response.headers["X-Frame-Options"] = "DENY"

        # XSS Protection (legacy, but still useful for older browsers)
        response.headers["X-XSS-Protection"] = "1; mode=block"

        # Referrer Policy
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        # Permissions Policy (restrict browser features)
        response.headers["Permissions-Policy"] = (
            "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
            "magnetometer=(), microphone=(self), payment=(), usb=()"
        )

        # Spectre/Meltdown protection + SharedArrayBuffer for WASM (Safari)
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Embedder-Policy"] = "require-corp"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"

        # Content Security Policy (allow self and common CDNs for development)
        # In production, this should be more restrictive
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "font-src 'self' data:; "
            "connect-src 'self' ws: wss:; "
            "media-src 'self' blob:; "
            "frame-ancestors 'none';"
        )

        return response


app.add_middleware(SecurityHeadersMiddleware)

# CORS Middleware - configured via settings
_cors_origins = (
    ["*"] if settings.cors_origins == "*"
    else [origin.strip() for origin in settings.cors_origins.split(",")]
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
)

# Prometheus Metrics (opt-in via METRICS_ENABLED=true)
setup_metrics(app)

# REST API Rate Limiting
setup_rate_limiter(app)

# REST API Routers
app.include_router(auth.router, prefix="/api/auth", tags=["Authentication"])
app.include_router(roles.router, prefix="/api/roles", tags=["Roles"])
app.include_router(users.router, prefix="/api/users", tags=["Users"])
app.include_router(chat.router, prefix="/api/chat", tags=["Chat"])
app.include_router(tasks.router, prefix="/api/tasks", tags=["Tasks"])
app.include_router(voice.router, prefix="/api/voice", tags=["Voice"])
app.include_router(camera.router, prefix="/api/camera", tags=["Camera"])
app.include_router(ha_routes.router, prefix="/api/homeassistant", tags=["Home Assistant"])
app.include_router(settings_routes.router, prefix="/api/settings", tags=["Settings"])
app.include_router(satellites.router, prefix="/api/satellites", tags=["Satellites"])
app.include_router(speakers.router, prefix="/api/speakers", tags=["Speakers"])
app.include_router(rooms.router, prefix="/api/rooms", tags=["Rooms"])
app.include_router(knowledge.router, prefix="/api/knowledge", tags=["Knowledge"])
app.include_router(memory.router, prefix="/api/memory", tags=["Memory"])
app.include_router(plugins.router, prefix="/api/plugins", tags=["Plugins"])
app.include_router(preferences.router, prefix="/api/preferences", tags=["Preferences"])
app.include_router(mcp_routes.router, prefix="/api/mcp", tags=["MCP"])
app.include_router(intents.router, prefix="/api/intents", tags=["Intents"])
app.include_router(feedback.router, prefix="/api/feedback", tags=["Feedback"])
app.include_router(notifications.router, prefix="/api/notifications", tags=["Notifications"])

# WebSocket Routers
app.include_router(chat_router, tags=["WebSocket Chat"])
app.include_router(satellite_router, tags=["WebSocket Satellite"])
app.include_router(device_router, tags=["WebSocket Device"])


# WebSocket for Wake Word Detection (Server-Side Fallback)
@app.websocket("/ws/wakeword")
async def wakeword_websocket(websocket: WebSocket):
    """
    WebSocket endpoint for server-side wake word detection.

    This is a fallback for clients where browser-based WASM detection
    is not available or performant.

    Protocol:
    - Client sends: Raw audio bytes (16-bit PCM, 16kHz, mono)
                   Expected: 2560 bytes per chunk (1280 samples * 2 bytes)
    - Server sends: JSON messages
                   {"type": "ready"} - Service is ready
                   {"type": "wakeword_detected", "keyword": str, "score": float}
                   {"type": "error", "message": str}
    """
    await websocket.accept()
    logger.info("🎤 Wake word WebSocket connection established")

    try:
        from services.wakeword_service import get_wakeword_service

        service = get_wakeword_service()

        # Check if service is available
        if not service.available:
            await websocket.send_json({
                "type": "error",
                "message": "OpenWakeWord not installed on server"
            })
            await websocket.close()
            return

        # Load model if not already loaded
        if not service.load_model():
            await websocket.send_json({
                "type": "error",
                "message": "Failed to load wake word model"
            })
            await websocket.close()
            return

        # Signal ready
        await websocket.send_json({
            "type": "ready",
            "keywords": service.keywords,
            "threshold": service.threshold
        })

        # Process audio chunks
        while True:
            # Receive audio chunk
            audio_bytes = await websocket.receive_bytes()

            # Process chunk
            result = service.process_audio_chunk(audio_bytes)

            # Send detection if wake word found
            if result.get("detected"):
                await websocket.send_json({
                    "type": "wakeword_detected",
                    "keyword": result["keyword"],
                    "score": result["score"]
                })

    except WebSocketDisconnect:
        logger.info("👋 Wake word WebSocket connection closed")
    except Exception as e:
        logger.error(f"❌ Wake word WebSocket error: {e}")
        try:
            await websocket.send_json({
                "type": "error",
                "message": str(e)
            })
        except Exception:
            pass  # WebSocket may already be closed
        await websocket.close()


# Health Check Endpoints
@app.get("/health")
async def health_check():
    """Quick health check for load balancers."""
    return {"status": "ok"}


@app.get("/health/ready")
async def readiness_check():
    """Kubernetes readiness probe - checks all dependencies."""
    from sqlalchemy import text

    checks = {}
    overall_healthy = True

    # Database check
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
        checks["database"] = {"status": "healthy"}
    except Exception as e:
        checks["database"] = {"status": "unhealthy", "error": str(e)}
        overall_healthy = False

    # Ollama check
    try:
        ollama = app.state.ollama if hasattr(app.state, "ollama") else None
        if ollama:
            checks["ollama"] = {"status": "healthy", "model": settings.ollama_model}
        else:
            checks["ollama"] = {"status": "degraded", "error": "Not initialized"}
    except Exception as e:
        checks["ollama"] = {"status": "unhealthy", "error": str(e)}
        overall_healthy = False

    # Redis check (optional)
    try:
        import redis.asyncio as redis
        r = redis.from_url(settings.redis_url)
        await r.ping()
        await r.close()
        checks["redis"] = {"status": "healthy"}
    except Exception as e:
        checks["redis"] = {"status": "degraded", "error": str(e)}
        # Redis is optional, don't fail health check

    # Connected devices count
    try:
        device_manager = get_device_manager()
        checks["devices"] = {
            "status": "healthy",
            "connected": len(device_manager.devices),
            "active_sessions": len(device_manager.sessions)
        }
    except Exception:
        checks["devices"] = {"status": "unknown"}

    status = "healthy" if overall_healthy else "unhealthy"
    status_code = 200 if overall_healthy else 503

    return JSONResponse(
        content={
            "status": status,
            "version": "1.0.0",
            "checks": checks,
            "timestamp": datetime.utcnow().isoformat()
        },
        status_code=status_code
    )


@app.get("/health/live")
async def liveness_check():
    """Kubernetes liveness probe - just checks if app is running."""
    return {"status": "alive", "timestamp": datetime.utcnow().isoformat()}


# WebSocket Token Generation Endpoint
@app.post("/api/ws/token")
async def create_ws_token(
    device_id: str = None,
    device_type: str = None
):
    """
    Generate a WebSocket authentication token.

    Only relevant when WS_AUTH_ENABLED=true.
    In production, this endpoint should be protected by authentication.
    """
    if not settings.ws_auth_enabled:
        return {
            "token": None,
            "message": "WebSocket authentication is disabled",
            "expires_in": None
        }

    token_store = get_token_store()
    token = token_store.create_token(
        device_id=device_id,
        device_type=device_type
    )

    return {
        "token": token,
        "expires_in": settings.ws_token_expire_minutes * 60,
        "protocol_version": settings.ws_protocol_version
    }


# Admin Endpoint: Refresh HA Keywords
@app.post("/admin/refresh-keywords")
async def refresh_keywords(
    user = Depends(require_permission(Permission.ADMIN))
):
    """
    Lade Home Assistant Keywords neu

    Nützlich nach dem Hinzufügen neuer Geräte in HA

    Requires: admin permission (when auth is enabled)
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
async def debug_intent(
    message: str,
    user = Depends(require_permission(Permission.ADMIN))
):
    """
    Teste Intent-Extraction für eine Nachricht

    Nützlich zum Debuggen von Intent-Erkennungsproblemen

    Requires: admin permission (when auth is enabled)
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
