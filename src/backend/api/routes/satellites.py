"""
Satellite Monitoring API Routes

Provides endpoints for monitoring and debugging satellite voice assistants.
Includes live status, metrics, session history, and error logs.
"""
from datetime import datetime
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from loguru import logger

from services.satellite_manager import get_satellite_manager, SatelliteState
from utils.config import settings


router = APIRouter()


# =============================================================================
# Response Models
# =============================================================================

class SatelliteCapabilitiesResponse(BaseModel):
    """Satellite hardware capabilities"""
    local_wakeword: bool = True
    speaker: bool = True
    led_count: int = 3
    button: bool = True


class SatelliteMetricsResponse(BaseModel):
    """Live metrics from satellite heartbeat"""
    audio_rms: Optional[float] = Field(None, description="Audio RMS level (0-32768)")
    audio_db: Optional[float] = Field(None, description="Audio level in dB")
    is_speech: Optional[bool] = Field(None, description="Voice activity detected")
    cpu_percent: Optional[float] = Field(None, description="CPU usage percentage")
    memory_percent: Optional[float] = Field(None, description="Memory usage percentage")
    temperature: Optional[float] = Field(None, description="CPU temperature in Celsius")
    last_wakeword: Optional[Dict[str, Any]] = Field(None, description="Last wake word detection")
    session_count_1h: int = Field(0, description="Sessions in last hour")
    error_count_1h: int = Field(0, description="Errors in last hour")


class SatelliteSessionResponse(BaseModel):
    """Active session information"""
    session_id: str
    state: str
    started_at: datetime
    duration_seconds: float
    audio_chunks_count: int
    audio_buffer_bytes: int
    transcription: Optional[str] = None


class SatelliteResponse(BaseModel):
    """Full satellite status response"""
    satellite_id: str
    room: str
    room_id: Optional[int] = None
    state: str
    connected_at: datetime
    last_heartbeat: datetime
    uptime_seconds: float
    heartbeat_ago_seconds: float
    has_active_session: bool
    current_session: Optional[SatelliteSessionResponse] = None
    capabilities: SatelliteCapabilitiesResponse
    metrics: SatelliteMetricsResponse
    language: str = "de"
    # Version and update info
    version: str = "unknown"
    update_available: bool = False
    update_status: Optional[str] = None  # none, in_progress, completed, failed
    update_stage: Optional[str] = None  # downloading, verifying, backing_up, etc.
    update_progress: int = 0
    update_error: Optional[str] = None


class SatelliteListResponse(BaseModel):
    """Response for listing all satellites"""
    satellites: List[SatelliteResponse]
    total_count: int
    online_count: int
    active_sessions: int
    latest_version: str  # Latest available satellite version


class SatelliteEventResponse(BaseModel):
    """Satellite event for history"""
    timestamp: datetime
    event_type: str  # connected, disconnected, session_start, session_end, error, wakeword
    details: Dict[str, Any] = {}


class SatelliteHistoryResponse(BaseModel):
    """Satellite event history"""
    satellite_id: str
    events: List[SatelliteEventResponse]
    total_sessions: int
    successful_sessions: int
    failed_sessions: int
    average_session_duration: float


# =============================================================================
# Helper Functions
# =============================================================================

def _is_update_available(current_version: str, latest_version: str) -> bool:
    """Check if an update is available by comparing version strings"""
    if current_version == "unknown":
        return False

    try:
        # Parse version strings (e.g., "1.0.0" -> [1, 0, 0])
        current_parts = [int(x) for x in current_version.split(".")]
        latest_parts = [int(x) for x in latest_version.split(".")]

        # Pad shorter version with zeros
        while len(current_parts) < len(latest_parts):
            current_parts.append(0)
        while len(latest_parts) < len(current_parts):
            latest_parts.append(0)

        # Compare version parts
        return latest_parts > current_parts
    except (ValueError, AttributeError):
        return False


def _satellite_to_response(sat_id: str, sat_data: Dict[str, Any]) -> SatelliteResponse:
    """Convert satellite data to response model"""
    import time

    manager = get_satellite_manager()
    sat = manager.get_satellite(sat_id)

    now = time.time()
    connected_at = datetime.fromtimestamp(sat_data["connected_at"])
    last_heartbeat = datetime.fromtimestamp(sat_data["last_heartbeat"])

    # Get metrics from satellite info
    metrics_data = getattr(sat, "metrics", {}) if sat else {}

    # Build session response if active
    current_session = None
    if sat and sat.current_session_id:
        session = manager.get_session(sat.current_session_id)
        if session:
            current_session = SatelliteSessionResponse(
                session_id=session.session_id,
                state=session.state.value,
                started_at=datetime.fromtimestamp(session.started_at),
                duration_seconds=now - session.started_at,
                audio_chunks_count=len(session.audio_chunks),
                audio_buffer_bytes=sum(len(c) for c in session.audio_chunks),
                transcription=session.transcription
            )

    # Get version info
    version = sat_data.get("version", "unknown")
    update_available = _is_update_available(version, settings.satellite_latest_version)

    return SatelliteResponse(
        satellite_id=sat_id,
        room=sat_data["room"],
        room_id=sat_data.get("room_id"),
        state=sat_data["state"],
        connected_at=connected_at,
        last_heartbeat=last_heartbeat,
        uptime_seconds=now - sat_data["connected_at"],
        heartbeat_ago_seconds=now - sat_data["last_heartbeat"],
        has_active_session=sat_data["has_active_session"],
        current_session=current_session,
        capabilities=SatelliteCapabilitiesResponse(**sat_data["capabilities"]),
        metrics=SatelliteMetricsResponse(**metrics_data),
        language=getattr(sat, "language", "de") if sat else "de",
        # Version and update info
        version=version,
        update_available=update_available,
        update_status=sat_data.get("update_status"),
        update_stage=sat_data.get("update_stage"),
        update_progress=sat_data.get("update_progress", 0),
        update_error=sat_data.get("update_error")
    )


# =============================================================================
# API Endpoints
# =============================================================================

@router.get("", response_model=SatelliteListResponse)
async def list_satellites():
    """
    List all connected satellites with their current status.

    Returns live status including state, metrics, and active sessions.
    """
    manager = get_satellite_manager()
    satellites_data = manager.get_all_satellites()

    responses = []
    active_sessions = 0

    for sat_data in satellites_data:
        sat_id = sat_data["satellite_id"]
        response = _satellite_to_response(sat_id, sat_data)
        responses.append(response)
        if sat_data["has_active_session"]:
            active_sessions += 1

    return SatelliteListResponse(
        satellites=responses,
        total_count=len(responses),
        online_count=len(responses),  # All returned are online
        active_sessions=active_sessions,
        latest_version=settings.satellite_latest_version
    )


# =============================================================================
# OTA Update Endpoints (MUST be before /{satellite_id} to avoid path conflicts)
# =============================================================================

class VersionInfoResponse(BaseModel):
    """Version information response"""
    latest_version: str
    satellites: List[Dict[str, Any]]  # List of satellite version summaries


class UpdateInitiateResponse(BaseModel):
    """Response for update initiation"""
    success: bool
    message: str
    target_version: Optional[str] = None


class UpdateStatusResponse(BaseModel):
    """Response for update status query"""
    satellite_id: str
    version: str
    update_available: bool
    update_status: Optional[str] = None
    update_stage: Optional[str] = None
    update_progress: int = 0
    update_error: Optional[str] = None


@router.get("/versions", response_model=VersionInfoResponse)
async def get_versions():
    """
    Get version information for all satellites.

    Returns the latest available version and version status of all satellites.
    """
    from services.satellite_update_service import get_satellite_update_service

    update_service = get_satellite_update_service()
    manager = get_satellite_manager()

    satellites_data = manager.get_all_satellites()
    satellite_versions = []

    for sat_data in satellites_data:
        version = sat_data.get("version", "unknown")
        satellite_versions.append({
            "satellite_id": sat_data["satellite_id"],
            "version": version,
            "update_available": update_service.is_update_available(version),
            "update_status": sat_data.get("update_status")
        })

    return VersionInfoResponse(
        latest_version=update_service.get_latest_version(),
        satellites=satellite_versions
    )


@router.get("/update-package")
async def get_update_package():
    """
    Download the satellite update package.

    Returns the tarball containing the latest satellite code.
    """
    from fastapi.responses import FileResponse
    from services.satellite_update_service import get_satellite_update_service

    update_service = get_satellite_update_service()
    package_info = update_service.get_package_info()

    if not package_info or not package_info.get("path"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Update package not available"
        )

    package_path = package_info["path"]
    version = package_info["version"]

    return FileResponse(
        path=package_path,
        media_type="application/gzip",
        filename=f"renfield-satellite-{version}.tar.gz",
        headers={
            "X-Package-Version": version,
            "X-Package-Checksum": package_info["checksum"],
            "X-Package-Size": str(package_info["size"])
        }
    )


# =============================================================================
# Satellite-specific Endpoints
# =============================================================================

@router.get("/{satellite_id}", response_model=SatelliteResponse)
async def get_satellite(satellite_id: str):
    """
    Get detailed status for a specific satellite.

    Includes live metrics, current session info, and capabilities.
    """
    manager = get_satellite_manager()
    sat = manager.get_satellite(satellite_id)

    if not sat:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Satellite '{satellite_id}' not found or not connected"
        )

    # Build the response using the helper
    satellites_data = manager.get_all_satellites()
    sat_data = next((s for s in satellites_data if s["satellite_id"] == satellite_id), None)

    if not sat_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Satellite '{satellite_id}' data not found"
        )

    return _satellite_to_response(satellite_id, sat_data)


@router.get("/{satellite_id}/metrics", response_model=SatelliteMetricsResponse)
async def get_satellite_metrics(satellite_id: str):
    """
    Get live metrics for a specific satellite.

    Returns audio levels, system stats, and session counters.
    """
    manager = get_satellite_manager()
    sat = manager.get_satellite(satellite_id)

    if not sat:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Satellite '{satellite_id}' not found or not connected"
        )

    # Get metrics from satellite info
    metrics_data = getattr(sat, "metrics", {})

    return SatelliteMetricsResponse(**metrics_data)


@router.get("/{satellite_id}/session", response_model=SatelliteSessionResponse)
async def get_satellite_session(satellite_id: str):
    """
    Get current active session for a satellite.

    Returns session details including audio buffer status.
    """
    import time

    manager = get_satellite_manager()
    sat = manager.get_satellite(satellite_id)

    if not sat:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Satellite '{satellite_id}' not found or not connected"
        )

    if not sat.current_session_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Satellite '{satellite_id}' has no active session"
        )

    session = manager.get_session(sat.current_session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session data not found"
        )

    now = time.time()

    return SatelliteSessionResponse(
        session_id=session.session_id,
        state=session.state.value,
        started_at=datetime.fromtimestamp(session.started_at),
        duration_seconds=now - session.started_at,
        audio_chunks_count=len(session.audio_chunks),
        audio_buffer_bytes=sum(len(c) for c in session.audio_chunks),
        transcription=session.transcription
    )


@router.get("/{satellite_id}/history", response_model=SatelliteHistoryResponse)
async def get_satellite_history(satellite_id: str, limit: int = 50):
    """
    Get event history for a satellite.

    Returns recent events including connections, sessions, and errors.
    Note: History is kept in-memory and reset on backend restart.
    """
    manager = get_satellite_manager()
    sat = manager.get_satellite(satellite_id)

    # Get history from manager (if it exists)
    history = getattr(manager, "_satellite_history", {}).get(satellite_id, [])
    stats = getattr(manager, "_satellite_stats", {}).get(satellite_id, {})

    events = []
    for event in history[-limit:]:
        events.append(SatelliteEventResponse(
            timestamp=datetime.fromtimestamp(event.get("timestamp", 0)),
            event_type=event.get("type", "unknown"),
            details=event.get("details", {})
        ))

    return SatelliteHistoryResponse(
        satellite_id=satellite_id,
        events=events,
        total_sessions=stats.get("total_sessions", 0),
        successful_sessions=stats.get("successful_sessions", 0),
        failed_sessions=stats.get("failed_sessions", 0),
        average_session_duration=stats.get("avg_duration", 0.0)
    )


@router.post("/{satellite_id}/ping")
async def ping_satellite(satellite_id: str):
    """
    Send a ping to a satellite to check connectivity.

    Returns success if satellite responds within timeout.
    """
    manager = get_satellite_manager()
    sat = manager.get_satellite(satellite_id)

    if not sat:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Satellite '{satellite_id}' not found or not connected"
        )

    try:
        await sat.websocket.send_json({
            "type": "ping",
            "timestamp": datetime.now().isoformat()
        })
        return {"status": "sent", "satellite_id": satellite_id}
    except Exception as e:
        logger.error(f"Failed to ping satellite {satellite_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Failed to ping satellite: {str(e)}"
        )


@router.post("/{satellite_id}/update", response_model=UpdateInitiateResponse)
async def initiate_update(satellite_id: str):
    """
    Initiate an OTA update for a specific satellite.

    Sends an update request to the satellite which will then download,
    install, and restart with the new version.
    """
    from services.satellite_update_service import get_satellite_update_service

    update_service = get_satellite_update_service()
    result = await update_service.initiate_update(satellite_id)

    if not result["success"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=result["message"]
        )

    return UpdateInitiateResponse(
        success=result["success"],
        message=result["message"],
        target_version=result.get("target_version")
    )


@router.get("/{satellite_id}/update-status", response_model=UpdateStatusResponse)
async def get_update_status(satellite_id: str):
    """
    Get the current update status for a satellite.

    Returns version info and update progress if an update is in progress.
    """
    from services.satellite_update_service import get_satellite_update_service

    manager = get_satellite_manager()
    sat = manager.get_satellite(satellite_id)

    if not sat:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Satellite '{satellite_id}' not found or not connected"
        )

    update_service = get_satellite_update_service()

    return UpdateStatusResponse(
        satellite_id=satellite_id,
        version=sat.version,
        update_available=update_service.is_update_available(sat.version),
        update_status=sat.update_status.value if sat.update_status else None,
        update_stage=sat.update_stage,
        update_progress=sat.update_progress,
        update_error=sat.update_error
    )
