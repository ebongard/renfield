"""
Settings API Routes

Provides configuration endpoints for frontend components.
"""

from fastapi import APIRouter
from utils.config import settings

router = APIRouter()


@router.get("/wakeword")
async def get_wakeword_settings():
    """
    Get wake word detection configuration.

    Returns default settings and available options for the frontend
    to configure wake word detection.
    """
    return {
        "enabled": settings.wake_word_enabled,
        "default_keyword": settings.wake_word_default,
        "threshold": settings.wake_word_threshold,
        "cooldown_ms": settings.wake_word_cooldown_ms,
        "available_keywords": [
            {
                "id": "hey_jarvis",
                "label": "Hey Jarvis",
                "description": "Pre-trained wake word"
            },
            {
                "id": "alexa",
                "label": "Alexa",
                "description": "Pre-trained wake word"
            },
            {
                "id": "hey_mycroft",
                "label": "Hey Mycroft",
                "description": "Pre-trained wake word"
            },
            # Add custom trained wake words here:
            # {
            #     "id": "hey_renfield",
            #     "label": "Hey Renfield",
            #     "description": "Custom trained wake word"
            # },
        ],
        "server_fallback_available": _check_server_fallback(),
    }


@router.get("/wakeword/status")
async def get_wakeword_status():
    """
    Get server-side wake word service status.

    Used to check if server-side fallback is available and loaded.
    """
    try:
        from services.wakeword_service import get_wakeword_service
        service = get_wakeword_service()
        return service.get_status()
    except Exception as e:
        return {
            "available": False,
            "error": str(e)
        }


def _check_server_fallback() -> bool:
    """Check if server-side OpenWakeWord is available."""
    try:
        from services.wakeword_service import OPENWAKEWORD_AVAILABLE
        return OPENWAKEWORD_AVAILABLE
    except ImportError:
        return False
