"""
Camera API Routes (Frigate Integration)
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from loguru import logger

from integrations.frigate import FrigateClient

router = APIRouter()
frigate = FrigateClient()

@router.get("/events")
async def get_camera_events(
    camera: Optional[str] = None,
    label: Optional[str] = None,
    limit: int = 10
):
    """Kamera-Events abrufen"""
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
async def list_cameras():
    """Liste aller Kameras"""
    try:
        cameras = await frigate.get_cameras()
        return {"cameras": cameras}
    except Exception as e:
        logger.error(f"❌ Camera List Fehler: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/snapshot/{event_id}")
async def get_snapshot(event_id: str):
    """Snapshot eines Events"""
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
async def get_latest_by_label(label: str = "person"):
    """Letzte Events eines bestimmten Typs"""
    try:
        events = await frigate.get_latest_events_by_type(label)
        return {"events": events}
    except Exception as e:
        logger.error(f"❌ Latest Events Fehler: {e}")
        raise HTTPException(status_code=500, detail=str(e))
