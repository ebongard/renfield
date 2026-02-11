"""
Settings API Routes

Provides configuration endpoints for frontend components.
Supports both read (all users) and write (admin only) operations.
"""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import User
from models.permissions import Permission
from services.auth_service import require_permission
from services.database import get_db
from services.wakeword_config_manager import (
    AVAILABLE_KEYWORDS,
    VALID_KEYWORDS,
    get_wakeword_config_manager,
)
from utils.config import settings

router = APIRouter()


# =============================================================================
# Request/Response Models
# =============================================================================

class WakeWordUpdateRequest(BaseModel):
    """Request body for updating wake word settings"""
    keyword: str | None = Field(
        None,
        description="Wake word keyword ID (e.g., 'alexa', 'hey_jarvis')"
    )
    threshold: float | None = Field(
        None,
        ge=0.1,
        le=1.0,
        description="Detection threshold (0.1 - 1.0)"
    )
    cooldown_ms: int | None = Field(
        None,
        ge=500,
        le=10000,
        description="Cooldown between detections in milliseconds (500 - 10000)"
    )


class WakeWordSettingsResponse(BaseModel):
    """Response for wake word settings"""
    enabled: bool
    keyword: str
    threshold: float
    cooldown_ms: int
    available_keywords: list
    server_fallback_available: bool
    subscriber_count: int = 0


class DeviceSyncStatusResponse(BaseModel):
    """Response for device sync status"""
    device_id: str
    device_type: str
    synced: bool
    active_keywords: list[str] = []
    failed_keywords: list[str] = []
    last_ack_time: str | None = None
    error: str | None = None


class AllDeviceSyncStatusResponse(BaseModel):
    """Response for all device sync statuses"""
    config_version: int
    devices: list[DeviceSyncStatusResponse]
    all_synced: bool
    synced_count: int
    pending_count: int


class ModelInfoResponse(BaseModel):
    """Response for model information"""
    model_id: str
    available: bool
    model_type: str = "tflite"
    file_size: int | None = None
    download_url: str | None = None


# =============================================================================
# Wake Word Settings Endpoints
# =============================================================================

@router.get("/wakeword", response_model=WakeWordSettingsResponse)
async def get_wakeword_settings(
    db: AsyncSession = Depends(get_db),
):
    """
    Get current wake word detection configuration.

    Returns current settings from database (with fallback to env vars)
    and available options for the frontend to configure wake word detection.

    No authentication required - all users can view settings.
    """
    config_manager = get_wakeword_config_manager()
    config = await config_manager.get_config(db)

    return WakeWordSettingsResponse(
        enabled=settings.wake_word_enabled,
        keyword=config.keyword,
        threshold=config.threshold,
        cooldown_ms=config.cooldown_ms,
        available_keywords=AVAILABLE_KEYWORDS,
        server_fallback_available=_check_server_fallback(),
        subscriber_count=config_manager.get_subscriber_count(),
    )


@router.put("/wakeword", response_model=WakeWordSettingsResponse)
async def update_wakeword_settings(
    request: WakeWordUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.SETTINGS_MANAGE)),
):
    """
    Update wake word detection configuration.

    Updates settings in database and broadcasts to all connected devices
    (satellites, web panels, browsers).

    Requires SETTINGS_MANAGE permission (typically Admin role).
    """
    config_manager = get_wakeword_config_manager()

    # Check if at least one field is provided
    if request.keyword is None and request.threshold is None and request.cooldown_ms is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one field (keyword, threshold, or cooldown_ms) must be provided"
        )

    try:
        config = await config_manager.update_config(
            db=db,
            keyword=request.keyword,
            threshold=request.threshold,
            cooldown_ms=request.cooldown_ms,
            updated_by=current_user.id if current_user else None,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )

    return WakeWordSettingsResponse(
        enabled=settings.wake_word_enabled,
        keyword=config.keyword,
        threshold=config.threshold,
        cooldown_ms=config.cooldown_ms,
        available_keywords=AVAILABLE_KEYWORDS,
        server_fallback_available=_check_server_fallback(),
        subscriber_count=config_manager.get_subscriber_count(),
    )


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


# =============================================================================
# Device Sync Status Endpoints
# =============================================================================

@router.get("/wakeword/sync-status", response_model=AllDeviceSyncStatusResponse)
async def get_device_sync_status():
    """
    Get synchronization status for all devices.

    Shows which devices have successfully applied the current wake word configuration.
    Useful for monitoring after a configuration change.
    """
    config_manager = get_wakeword_config_manager()
    status = config_manager.get_device_sync_status()
    return AllDeviceSyncStatusResponse(**status)


@router.get("/wakeword/sync-status/{device_id}", response_model=DeviceSyncStatusResponse)
async def get_single_device_sync_status(device_id: str):
    """
    Get synchronization status for a specific device.

    Args:
        device_id: The device identifier (e.g., "satellite-living-room")
    """
    config_manager = get_wakeword_config_manager()
    sync_status = config_manager.get_device_sync_status(device_id)

    if "error" in sync_status:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=sync_status["error"]
        )

    return DeviceSyncStatusResponse(**sync_status)


# =============================================================================
# Wake Word Model Endpoints (for satellite model download)
# =============================================================================

# Path to TFLite models for satellite download
# These are mounted from data/wakeword-models/ via docker-compose
TFLITE_MODELS_PATH = Path("/app/wakeword-models")


def _find_model_file(model_id: str) -> Path | None:
    """
    Find a wake word model file (TFLite or ONNX) for the given model ID.

    Searches in the wakeword-models directory.
    """
    if not TFLITE_MODELS_PATH.exists():
        logger.warning(f"Models path does not exist: {TFLITE_MODELS_PATH}")
        return None

    # Try exact match first (TFLite, then ONNX)
    for ext in [".tflite", ".onnx"]:
        exact_path = TFLITE_MODELS_PATH / f"{model_id}{ext}"
        if exact_path.exists():
            return exact_path

    # Try with version suffix
    for ext in [".tflite", ".onnx"]:
        versioned_path = TFLITE_MODELS_PATH / f"{model_id}_v0.1{ext}"
        if versioned_path.exists():
            return versioned_path

    # Search for any matching file
    for model_file in TFLITE_MODELS_PATH.glob("*"):
        if model_file.suffix not in (".tflite", ".onnx"):
            continue
        # Skip preprocessing models
        if model_file.stem in ["melspectrogram", "embedding_model"]:
            continue
        # Check if model_id is in the filename
        if model_id in model_file.stem.lower():
            return model_file

    return None


@router.get("/wakeword/models")
async def list_available_models():
    """
    List all wake word models available for download.

    Returns information about each model including download URL.
    Satellites can use this to check model availability before attempting download.
    """
    models = []

    for kw in AVAILABLE_KEYWORDS:
        model_id = kw["id"]
        model_file = _find_model_file(model_id)

        model_info = {
            "model_id": model_id,
            "label": kw["label"],
            "description": kw["description"],
            "available": model_file is not None,
            "model_type": model_file.suffix.lstrip(".") if model_file else "unknown",
            "file_size": model_file.stat().st_size if model_file else None,
            "download_url": f"/api/settings/wakeword/models/{model_id}" if model_file else None,
        }
        models.append(model_info)

    return {
        "models": models,
        "base_url": "/api/settings/wakeword/models",
    }


@router.get("/wakeword/models/{model_id}")
async def download_model(model_id: str):
    """
    Download a wake word model file (TFLite format).

    Used by satellites to download models they don't have locally.

    Args:
        model_id: The model identifier (e.g., "alexa", "hey_jarvis")

    Returns:
        TFLite model file as binary download
    """
    # Validate model_id
    if model_id not in VALID_KEYWORDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid model_id: {model_id}. Must be one of: {VALID_KEYWORDS}"
        )

    model_file = _find_model_file(model_id)

    if not model_file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Model file not found for: {model_id}"
        )

    suffix = model_file.suffix  # ".tflite" or ".onnx"
    filename = f"{model_id}{suffix}"
    logger.info(f"📦 Serving wake word model: {model_id} ({model_file.name})")

    return FileResponse(
        path=model_file,
        media_type="application/octet-stream",
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )
