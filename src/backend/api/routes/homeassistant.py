"""
Home Assistant API Routes

With RPBAC permission checks:
- ha.read: View device states
- ha.control: Turn devices on/off, set values
- ha.full: Call arbitrary services
"""
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel

from integrations.homeassistant import HomeAssistantClient
from models.database import User
from models.permissions import Permission
from services.auth_service import require_permission

router = APIRouter()
ha_client = HomeAssistantClient()

class ServiceCall(BaseModel):
    domain: str
    service: str
    entity_id: str | None = None
    service_data: dict | None = None

class SetValue(BaseModel):
    entity_id: str
    value: Any  # Geändert von 'any' zu 'Any'
    attribute: str = "value"

@router.get("/states")
async def get_all_states(
    user: User = Depends(require_permission(Permission.HA_READ))
):
    """
    Alle Entity States abrufen.

    Requires: ha.read permission
    """
    try:
        states = await ha_client.get_states()
        return {"states": states}
    except Exception as e:
        logger.error(f"❌ Get States Fehler: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/state/{entity_id}")
async def get_entity_state(
    entity_id: str,
    user: User = Depends(require_permission(Permission.HA_READ))
):
    """
    State einer bestimmten Entity.

    Requires: ha.read permission
    """
    try:
        state = await ha_client.get_state(entity_id)
        if not state:
            raise HTTPException(status_code=404, detail="Entity nicht gefunden")
        return state
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Get State Fehler: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/turn_on/{entity_id}")
async def turn_on(
    entity_id: str,
    user: User = Depends(require_permission(Permission.HA_CONTROL))
):
    """
    Gerät einschalten.

    Requires: ha.control permission
    """
    try:
        success = await ha_client.turn_on(entity_id)
        if not success:
            raise HTTPException(status_code=500, detail="Aktion fehlgeschlagen")
        return {"success": True, "action": "turn_on", "entity_id": entity_id}
    except Exception as e:
        logger.error(f"❌ Turn On Fehler: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/turn_off/{entity_id}")
async def turn_off(
    entity_id: str,
    user: User = Depends(require_permission(Permission.HA_CONTROL))
):
    """
    Gerät ausschalten.

    Requires: ha.control permission
    """
    try:
        success = await ha_client.turn_off(entity_id)
        if not success:
            raise HTTPException(status_code=500, detail="Aktion fehlgeschlagen")
        return {"success": True, "action": "turn_off", "entity_id": entity_id}
    except Exception as e:
        logger.error(f"❌ Turn Off Fehler: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/toggle/{entity_id}")
async def toggle(
    entity_id: str,
    user: User = Depends(require_permission(Permission.HA_CONTROL))
):
    """
    Gerät umschalten.

    Requires: ha.control permission
    """
    try:
        success = await ha_client.toggle(entity_id)
        if not success:
            raise HTTPException(status_code=500, detail="Aktion fehlgeschlagen")
        return {"success": True, "action": "toggle", "entity_id": entity_id}
    except Exception as e:
        logger.error(f"❌ Toggle Fehler: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/set_value")
async def set_value(
    request: SetValue,
    user: User = Depends(require_permission(Permission.HA_CONTROL))
):
    """
    Wert setzen (Helligkeit, Temperatur, etc.).

    Requires: ha.control permission
    """
    try:
        success = await ha_client.set_value(
            request.entity_id,
            request.value,
            request.attribute
        )
        if not success:
            raise HTTPException(status_code=500, detail="Aktion fehlgeschlagen")
        return {
            "success": True,
            "action": "set_value",
            "entity_id": request.entity_id,
            "value": request.value
        }
    except Exception as e:
        logger.error(f"❌ Set Value Fehler: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/service")
async def call_service(
    request: ServiceCall,
    user: User = Depends(require_permission(Permission.HA_FULL))
):
    """
    Beliebigen Service aufrufen.

    Requires: ha.full permission (most privileged HA permission)
    """
    try:
        success = await ha_client.call_service(
            request.domain,
            request.service,
            request.entity_id,
            request.service_data
        )
        if not success:
            raise HTTPException(status_code=500, detail="Service-Aufruf fehlgeschlagen")
        return {"success": True}
    except Exception as e:
        logger.error(f"❌ Call Service Fehler: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/search")
async def search_entities(
    query: str,
    user: User = Depends(require_permission(Permission.HA_READ))
):
    """
    Entities suchen.

    Requires: ha.read permission
    """
    try:
        results = await ha_client.search_entities(query)
        return {"results": results}
    except Exception as e:
        logger.error(f"❌ Search Fehler: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/domain/{domain}")
async def get_entities_by_domain(
    domain: str,
    user: User = Depends(require_permission(Permission.HA_READ))
):
    """
    Alle Entities eines Domains.

    Requires: ha.read permission
    """
    try:
        entities = await ha_client.get_entities_by_domain(domain)
        return {"entities": entities}
    except Exception as e:
        logger.error(f"❌ Get Domain Entities Fehler: {e}")
        raise HTTPException(status_code=500, detail=str(e))
