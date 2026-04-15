"""HA-specific admin endpoints.

Currently just `/admin/refresh-keywords` — a manual trigger that
reloads the Home Assistant keyword list. The endpoint used to live
inline in platform `main.py`, which was the last platform-side
reference to `integrations.homeassistant` after the Phase 1 W2
sweep. Extracted here and mounted via the `register_routes` hook
from `ha_glue/bootstrap.py::ha_glue_register_routes`.

Platform-only deploys (no ha_glue) don't mount this router — the
endpoint simply doesn't exist, and callers get a clean 404.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from models.permissions import Permission
from services.auth_service import require_permission


router = APIRouter(tags=["admin"])


@router.post("/admin/refresh-keywords")
async def refresh_keywords(user=Depends(require_permission(Permission.ADMIN))):
    """Reload Home Assistant keywords.

    Nützlich nach dem Hinzufügen neuer Geräte in HA. Requires admin
    permission when auth is enabled.
    """
    try:
        from ha_glue.integrations.homeassistant import HomeAssistantClient

        ha_client = HomeAssistantClient()
        keywords = await ha_client.get_keywords(refresh=True)

        return {
            "status": "success",
            "keywords_count": len(keywords),
            "sample_keywords": list(keywords)[:20],
        }
    except Exception as e:  # noqa: BLE001
        logger.error(f"❌ Keyword Refresh Fehler: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e
