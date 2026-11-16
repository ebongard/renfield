"""
Camera API Routes (Frigate Integration)

With RPBAC permission checks:
- cam.view: View events list and camera list
- cam.full: Access snapshots and full event details
"""

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from ha_glue.integrations.frigate import FrigateClient
from models.database import User
from models.permissions import Permission
from services.auth_service import require_permission
from utils.log_safety import error_text_without_content

router = APIRouter()
frigate = FrigateClient()


@router.get("/events")
async def get_camera_events(
    camera: str | None = None,
    label: str | None = None,
    limit: int = 10,
    user: User = Depends(require_permission(Permission.CAM_VIEW))
):
    """
    Fetch camera events.

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
        logger.error(f"❌ Camera events error: {error_text_without_content(e)}")
        raise HTTPException(status_code=500, detail=error_text_without_content(e))


@router.get("/cameras")
async def list_cameras(
    user: User = Depends(require_permission(Permission.CAM_VIEW))
):
    """
    List every camera.

    Requires: cam.view permission
    """
    try:
        cameras = await frigate.get_cameras()
        return {"cameras": cameras}
    except Exception as e:
        logger.error(f"❌ Camera list error: {error_text_without_content(e)}")
        raise HTTPException(status_code=500, detail=error_text_without_content(e))


@router.get("/snapshot/{event_id}")
async def get_snapshot(
    event_id: str,
    user: User = Depends(require_permission(Permission.CAM_FULL))
):
    """
    Snapshot of one event.

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
        logger.error(f"❌ Snapshot error: {error_text_without_content(e)}")
        raise HTTPException(status_code=500, detail=error_text_without_content(e))


@router.get("/latest/{label}")
async def get_latest_by_label(
    label: str = "person",
    user: User = Depends(require_permission(Permission.CAM_VIEW))
):
    """
    The most recent events of one specific type.

    Requires: cam.view permission
    """
    try:
        events = await frigate.get_latest_events_by_type(label)
        return {"events": events}
    except Exception as e:
        logger.error(f"❌ Latest events error: {error_text_without_content(e)}")
        raise HTTPException(status_code=500, detail=error_text_without_content(e))
