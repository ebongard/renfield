"""
Camera API Routes (Frigate Integration)

With RPBAC permission checks:
- cam.view: View events list and camera list
- cam.full: Access snapshots and full event details
"""
from fastapi import APIRouter, HTTPException, Depends
from typing import Optional
from loguru import logger

from integrations.frigate import FrigateClient
from services.auth_service import require_permission
from models.database import User
from models.permissions import Permission

router = APIRouter()
frigate = FrigateClient()


@router.get("/events")
async def get_camera_events(
    camera: Optional[str] = None,
    label: Optional[str] = None,
    limit: int = 10,
    user: User = Depends(require_permission(Permission.CAM_VIEW))
):
    """
    Kamera-Events abrufen.

    Requires: cam.view permission
    """
    try:
        events = await frigate.get_events(
            camera=camera,
            label=label,
            limit=limit
        )
        return {"events": events}
    except Exception as e:
        logger.error(f"❌ Camera Events Fehler: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/cameras")
async def list_cameras(
    user: User = Depends(require_permission(Permission.CAM_VIEW))
):
    """
    Liste aller Kameras.

    Requires: cam.view permission
    """
    try:
        cameras = await frigate.get_cameras()
        return {"cameras": cameras}
    except Exception as e:
        logger.error(f"❌ Camera List Fehler: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/snapshot/{event_id}")
async def get_snapshot(
    event_id: str,
    user: User = Depends(require_permission(Permission.CAM_FULL))
):
    """
    Snapshot eines Events.

    Requires: cam.full permission (contains actual image data)
    """
    try:
        from fastapi.responses import Response

        snapshot = await frigate.get_snapshot(event_id)
        if not snapshot:
            raise HTTPException(status_code=404, detail="Snapshot nicht gefunden")

        return Response(
            content=snapshot,
            media_type="image/jpeg"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Snapshot Fehler: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/latest/{label}")
async def get_latest_by_label(
    label: str = "person",
    user: User = Depends(require_permission(Permission.CAM_VIEW))
):
    """
    Letzte Events eines bestimmten Typs.

    Requires: cam.view permission
    """
    try:
        events = await frigate.get_latest_events_by_type(label)
        return {"events": events}
    except Exception as e:
        logger.error(f"❌ Latest Events Fehler: {e}")
        raise HTTPException(status_code=500, detail=str(e))
