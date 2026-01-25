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


class SatelliteListResponse(BaseModel):
    """Response for listing all satellites"""
    satellites: List[SatelliteResponse]
    total_count: int
    online_count: int
    active_sessions: int


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
        language=getattr(sat, "language", "de") if sat else "de"
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
        active_sessions=active_sessions
    )


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
