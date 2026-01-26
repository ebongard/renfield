"""
Satellite Manager Service for Renfield

Manages satellite voice assistants (Raspberry Pi Zero 2 W) that connect
via WebSocket for distributed voice control throughout the house.

Features:
- Session-based routing for multi-room support
- Concurrent request handling (different rooms in parallel)
- First-speaker-wins for same-room conflicts
- Audio buffer management for streaming
- Message size limits and buffer protection
"""

import asyncio
import base64
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Any, Tuple
from fastapi import WebSocket
from loguru import logger

from utils.config import settings


class SatelliteState(str, Enum):
    """Satellite operational states"""
    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"
    SPEAKING = "speaking"
    ERROR = "error"


@dataclass
class SatelliteCapabilities:
    """Hardware capabilities of a satellite"""
    local_wakeword: bool = True
    speaker: bool = True
    led_count: int = 3
    button: bool = True


@dataclass
class SatelliteMetrics:
    """Live metrics from satellite heartbeat"""
    audio_rms: Optional[float] = None
    audio_db: Optional[float] = None
    is_speech: Optional[bool] = None
    cpu_percent: Optional[float] = None
    memory_percent: Optional[float] = None
    temperature: Optional[float] = None
    last_wakeword: Optional[Dict[str, Any]] = None
    session_count_1h: int = 0
    error_count_1h: int = 0
    updated_at: float = field(default_factory=time.time)


class UpdateStatus(str, Enum):
    """Satellite update states"""
    NONE = "none"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class SatelliteInfo:
    """Information about a connected satellite"""
    satellite_id: str
    room: str
    websocket: WebSocket
    capabilities: SatelliteCapabilities
    state: SatelliteState = SatelliteState.IDLE
    connected_at: float = field(default_factory=time.time)
    last_heartbeat: float = field(default_factory=time.time)
    current_session_id: Optional[str] = None
    room_id: Optional[int] = None  # Database room ID (populated after DB sync)
    language: str = "de"  # Language code for STT/TTS (e.g., 'de', 'en')
    metrics: Dict[str, Any] = field(default_factory=dict)  # Live metrics from heartbeat
    # Version and update tracking
    version: str = "unknown"
    update_status: UpdateStatus = UpdateStatus.NONE
    update_stage: Optional[str] = None  # downloading, verifying, backing_up, etc.
    update_progress: int = 0  # 0-100
    update_error: Optional[str] = None


@dataclass
class SatelliteSession:
    """Active voice interaction session"""
    session_id: str
    satellite_id: str
    room: str
    state: SatelliteState
    audio_chunks: List[bytes] = field(default_factory=list)
    audio_sequence: int = 0
    started_at: float = field(default_factory=time.time)
    transcription: Optional[str] = None
    response_text: Optional[str] = None

    # Timeout settings
    max_duration_seconds: float = 30.0


class SatelliteManager:
    """
    Manages satellite voice assistants connected via WebSocket.

    Handles:
    - Satellite registration and lifecycle
    - Session creation and routing
    - Audio buffer management
    - State synchronization
    """

    def __init__(self):
        self.satellites: Dict[str, SatelliteInfo] = {}
        self.sessions: Dict[str, SatelliteSession] = {}
        self._lock = asyncio.Lock()

        # Configuration - use settings from config
        from utils.config import settings
        self.default_wake_words = [settings.wake_word_default]
        self.default_threshold = settings.wake_word_threshold
        self.session_timeout = settings.device_session_timeout
        self.heartbeat_timeout = settings.device_heartbeat_timeout

        logger.info("📡 SatelliteManager initialized")

    async def register(
        self,
        satellite_id: str,
        room: str,
        websocket: WebSocket,
        capabilities: Dict[str, Any],
        language: str = "de",
        version: str = "unknown"
    ) -> bool:
        """
        Register a new satellite connection.

        Args:
            satellite_id: Unique identifier for the satellite
            room: Room name where satellite is located
            websocket: WebSocket connection to satellite
            capabilities: Hardware capabilities dict
            language: Language code for STT/TTS (e.g., 'de', 'en')
            version: Satellite software version

        Returns:
            True if registration successful
        """
        async with self._lock:
            # Check if satellite already connected (reconnection)
            if satellite_id in self.satellites:
                old_sat = self.satellites[satellite_id]
                logger.info(f"📡 Satellite {satellite_id} reconnecting (was in room: {old_sat.room})")
                # Close old connection if still open
                try:
                    await old_sat.websocket.close()
                except Exception:
                    pass  # Connection may already be closed

            # Create capability object
            caps = SatelliteCapabilities(
                local_wakeword=capabilities.get("local_wakeword", True),
                speaker=capabilities.get("speaker", True),
                led_count=capabilities.get("led_count", 3),
                button=capabilities.get("button", True)
            )

            # Register satellite
            self.satellites[satellite_id] = SatelliteInfo(
                satellite_id=satellite_id,
                room=room,
                websocket=websocket,
                capabilities=caps,
                language=language,
                version=version
            )

            logger.info(f"✅ Satellite registered: {satellite_id} in {room} (v{version})")
            logger.info(f"   Capabilities: wakeword={caps.local_wakeword}, speaker={caps.speaker}, leds={caps.led_count}")

            # Track event
            self._add_event(satellite_id, "connected", {
                "room": room,
                "capabilities": capabilities
            })

            return True

    async def unregister(self, satellite_id: str):
        """Remove a satellite from the registry"""
        async with self._lock:
            if satellite_id in self.satellites:
                sat = self.satellites[satellite_id]

                # End any active session
                if sat.current_session_id:
                    await self._end_session_internal(sat.current_session_id)

                del self.satellites[satellite_id]
                logger.info(f"👋 Satellite unregistered: {satellite_id}")

    async def start_session(
        self,
        satellite_id: str,
        keyword: str,
        confidence: float,
        session_id: Optional[str] = None
    ) -> Optional[str]:
        """
        Start a new voice interaction session after wake word detection.

        Args:
            satellite_id: ID of the satellite that detected wake word
            keyword: Wake word that was detected
            confidence: Detection confidence score
            session_id: Optional session ID from satellite (used to match audio chunks)

        Returns:
            Session ID if started, None if satellite is busy
        """
        async with self._lock:
            if satellite_id not in self.satellites:
                logger.warning(f"⚠️ Unknown satellite tried to start session: {satellite_id}")
                return None

            sat = self.satellites[satellite_id]

            # Check if satellite already has active session
            if sat.current_session_id:
                logger.warning(f"⚠️ Satellite {satellite_id} already has active session")
                return None

            # Use provided session ID or generate one
            if not session_id:
                session_id = f"{satellite_id}-{uuid.uuid4().hex[:8]}"

            # Create session
            session = SatelliteSession(
                session_id=session_id,
                satellite_id=satellite_id,
                room=sat.room,
                state=SatelliteState.LISTENING
            )

            self.sessions[session_id] = session
            sat.current_session_id = session_id
            sat.state = SatelliteState.LISTENING

            logger.info(f"🎙️ Session started: {session_id}")
            logger.info(f"   Room: {sat.room}, Wake word: {keyword} ({confidence:.2f})")

            # Track event
            self._add_event(satellite_id, "session_start", {
                "session_id": session_id,
                "keyword": keyword,
                "confidence": confidence
            })

            return session_id

    def buffer_audio(
        self,
        session_id: str,
        chunk_b64: str,
        sequence: int
    ) -> Tuple[bool, str]:
        """
        Buffer an audio chunk from a satellite.

        Args:
            session_id: Active session ID
            chunk_b64: Base64 encoded PCM audio data
            sequence: Sequence number for ordering

        Returns:
            Tuple of (success: bool, error_message: str)
        """
        if session_id not in self.sessions:
            logger.warning(f"⚠️ Audio for unknown session: {session_id}")
            return False, "Unknown session"

        session = self.sessions[session_id]

        # Check message size limit
        if len(chunk_b64) > settings.ws_max_message_size:
            logger.warning(f"⚠️ Audio chunk too large: {len(chunk_b64)} bytes (max: {settings.ws_max_message_size})")
            return False, f"Audio chunk too large (max: {settings.ws_max_message_size} bytes)"

        # Decode audio
        try:
            audio_bytes = base64.b64decode(chunk_b64)
        except Exception as e:
            logger.error(f"❌ Failed to decode audio chunk: {e}")
            return False, "Invalid base64 encoding"

        # Check buffer size limit
        current_size = sum(len(c) for c in session.audio_chunks)
        if current_size + len(audio_bytes) > settings.ws_max_audio_buffer_size:
            logger.warning(f"⚠️ Audio buffer full for session {session_id}: {current_size} bytes")
            return False, f"Audio buffer full (max: {settings.ws_max_audio_buffer_size} bytes)"

        # Buffer chunk
        session.audio_chunks.append(audio_bytes)
        session.audio_sequence = sequence

        return True, ""

    def get_audio_buffer(self, session_id: str) -> Optional[bytes]:
        """
        Get the complete audio buffer for a session.

        Args:
            session_id: Session to get audio for

        Returns:
            Concatenated audio bytes, or None if session not found
        """
        if session_id not in self.sessions:
            return None

        session = self.sessions[session_id]

        if not session.audio_chunks:
            return None

        # Concatenate all chunks
        return b"".join(session.audio_chunks)

    async def set_session_state(
        self,
        session_id: str,
        state: SatelliteState
    ):
        """Update session state and notify satellite"""
        if session_id not in self.sessions:
            return

        session = self.sessions[session_id]
        session.state = state

        # Update satellite state too
        if session.satellite_id in self.satellites:
            sat = self.satellites[session.satellite_id]
            sat.state = state

            # Notify satellite of state change
            try:
                await sat.websocket.send_json({
                    "type": "state",
                    "state": state.value
                })
            except Exception as e:
                logger.error(f"❌ Failed to send state to satellite: {e}")

    async def send_transcription(
        self,
        session_id: str,
        text: str
    ):
        """Send transcription result to satellite"""
        if session_id not in self.sessions:
            return

        session = self.sessions[session_id]
        session.transcription = text

        if session.satellite_id in self.satellites:
            sat = self.satellites[session.satellite_id]
            try:
                await sat.websocket.send_json({
                    "type": "transcription",
                    "session_id": session_id,
                    "text": text
                })
            except Exception as e:
                logger.error(f"❌ Failed to send transcription: {e}")

    async def send_action_result(
        self,
        session_id: str,
        intent: Dict[str, Any],
        success: bool
    ):
        """Send action execution result to satellite"""
        if session_id not in self.sessions:
            return

        session = self.sessions[session_id]

        if session.satellite_id in self.satellites:
            sat = self.satellites[session.satellite_id]
            try:
                await sat.websocket.send_json({
                    "type": "action",
                    "session_id": session_id,
                    "intent": intent,
                    "success": success
                })
            except Exception as e:
                logger.error(f"❌ Failed to send action result: {e}")

    async def send_tts_audio(
        self,
        session_id: str,
        audio_bytes: bytes,
        is_final: bool = True
    ):
        """
        Send TTS audio to satellite for playback.

        Args:
            session_id: Target session
            audio_bytes: WAV audio data
            is_final: Whether this is the final audio chunk
        """
        if session_id not in self.sessions:
            return

        session = self.sessions[session_id]

        if session.satellite_id in self.satellites:
            sat = self.satellites[session.satellite_id]

            # Update state to speaking
            if sat.state != SatelliteState.SPEAKING:
                await self.set_session_state(session_id, SatelliteState.SPEAKING)

            try:
                # Encode audio as base64
                audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")

                await sat.websocket.send_json({
                    "type": "tts_audio",
                    "session_id": session_id,
                    "audio": audio_b64,
                    "is_final": is_final
                })

                logger.info(f"🔊 Sent TTS audio to {sat.satellite_id} ({len(audio_bytes)} bytes)")

            except Exception as e:
                logger.error(f"❌ Failed to send TTS audio: {e}")

    async def end_session(self, session_id: str, reason: str = "completed"):
        """
        End an active session.

        Args:
            session_id: Session to end
            reason: Reason for ending (completed, timeout, error)
        """
        async with self._lock:
            await self._end_session_internal(session_id, reason)

    async def _end_session_internal(self, session_id: str, reason: str = "completed"):
        """Internal session end without lock"""
        if session_id not in self.sessions:
            return

        session = self.sessions[session_id]

        # Clear satellite's current session
        if session.satellite_id in self.satellites:
            sat = self.satellites[session.satellite_id]
            sat.current_session_id = None
            sat.state = SatelliteState.IDLE

            # Notify satellite to return to idle
            try:
                await sat.websocket.send_json({
                    "type": "state",
                    "state": "idle"
                })
            except Exception:
                pass  # Satellite may have disconnected

        # Calculate duration
        duration = time.time() - session.started_at

        # Track event and stats
        success = reason in ["completed", "silence"]
        self._add_event(session.satellite_id, "session_end", {
            "session_id": session_id,
            "reason": reason,
            "duration": duration,
            "success": success,
            "transcription": session.transcription
        })
        self._update_stats(session.satellite_id, duration, success)

        # Remove session
        del self.sessions[session_id]

        logger.info(f"✅ Session ended: {session_id} ({reason}, {duration:.1f}s)")

    def update_heartbeat(self, satellite_id: str, metrics: Optional[Dict[str, Any]] = None, version: Optional[str] = None):
        """
        Update satellite heartbeat timestamp and optional metrics.

        Args:
            satellite_id: ID of the satellite
            metrics: Optional metrics dict from heartbeat message
            version: Optional version from heartbeat message
        """
        if satellite_id in self.satellites:
            sat = self.satellites[satellite_id]
            sat.last_heartbeat = time.time()

            # Update version if provided
            if version and version != "unknown":
                sat.version = version

            # Update metrics if provided
            if metrics:
                sat.metrics = {
                    "audio_rms": metrics.get("audio_rms"),
                    "audio_db": metrics.get("audio_db"),
                    "is_speech": metrics.get("is_speech"),
                    "cpu_percent": metrics.get("cpu_percent"),
                    "memory_percent": metrics.get("memory_percent"),
                    "temperature": metrics.get("temperature"),
                    "last_wakeword": metrics.get("last_wakeword"),
                    "session_count_1h": metrics.get("session_count_1h", 0),
                    "error_count_1h": metrics.get("error_count_1h", 0),
                }

                # Track history
                self._add_event(satellite_id, "heartbeat", {
                    "state": sat.state.value,
                    "audio_rms": metrics.get("audio_rms"),
                    "is_speech": metrics.get("is_speech"),
                })

    def _add_event(self, satellite_id: str, event_type: str, details: Dict[str, Any] = None):
        """Add an event to satellite history"""
        if not hasattr(self, "_satellite_history"):
            self._satellite_history: Dict[str, List[Dict[str, Any]]] = {}

        if satellite_id not in self._satellite_history:
            self._satellite_history[satellite_id] = []

        event = {
            "timestamp": time.time(),
            "type": event_type,
            "details": details or {}
        }

        self._satellite_history[satellite_id].append(event)

        # Keep only last 1000 events per satellite
        if len(self._satellite_history[satellite_id]) > 1000:
            self._satellite_history[satellite_id] = self._satellite_history[satellite_id][-1000:]

    def _update_stats(self, satellite_id: str, session_duration: float, success: bool):
        """Update session statistics for a satellite"""
        if not hasattr(self, "_satellite_stats"):
            self._satellite_stats: Dict[str, Dict[str, Any]] = {}

        if satellite_id not in self._satellite_stats:
            self._satellite_stats[satellite_id] = {
                "total_sessions": 0,
                "successful_sessions": 0,
                "failed_sessions": 0,
                "total_duration": 0.0,
                "avg_duration": 0.0
            }

        stats = self._satellite_stats[satellite_id]
        stats["total_sessions"] += 1

        if success:
            stats["successful_sessions"] += 1
        else:
            stats["failed_sessions"] += 1

        stats["total_duration"] += session_duration
        stats["avg_duration"] = stats["total_duration"] / stats["total_sessions"]

    def get_satellite_by_session(self, session_id: str) -> Optional[SatelliteInfo]:
        """Get satellite info for a session"""
        if session_id not in self.sessions:
            return None

        session = self.sessions[session_id]
        return self.satellites.get(session.satellite_id)

    def get_session(self, session_id: str) -> Optional[SatelliteSession]:
        """Get session by ID"""
        return self.sessions.get(session_id)

    def get_all_satellites(self) -> List[Dict[str, Any]]:
        """Get status of all connected satellites"""
        result = []
        for sat_id, sat in self.satellites.items():
            result.append({
                "satellite_id": sat_id,
                "room": sat.room,
                "room_id": sat.room_id,
                "state": sat.state.value,
                "connected_at": sat.connected_at,
                "last_heartbeat": sat.last_heartbeat,
                "has_active_session": sat.current_session_id is not None,
                "capabilities": {
                    "local_wakeword": sat.capabilities.local_wakeword,
                    "speaker": sat.capabilities.speaker,
                    "led_count": sat.capabilities.led_count
                },
                # Version and update info
                "version": sat.version,
                "update_status": sat.update_status.value if sat.update_status else None,
                "update_stage": sat.update_stage,
                "update_progress": sat.update_progress,
                "update_error": sat.update_error
            })
        return result

    def set_room_id(self, satellite_id: str, room_id: int):
        """Set the database room ID for a satellite after DB sync"""
        if satellite_id in self.satellites:
            self.satellites[satellite_id].room_id = room_id

    def set_update_status(
        self,
        satellite_id: str,
        status: UpdateStatus,
        stage: Optional[str] = None,
        progress: int = 0,
        error: Optional[str] = None
    ):
        """
        Update the update status for a satellite.

        Args:
            satellite_id: ID of the satellite
            status: Current update status
            stage: Current update stage (downloading, verifying, etc.)
            progress: Progress percentage (0-100)
            error: Error message if update failed
        """
        if satellite_id in self.satellites:
            sat = self.satellites[satellite_id]
            sat.update_status = status
            sat.update_stage = stage
            sat.update_progress = progress
            sat.update_error = error
            logger.info(f"📡 Satellite {satellite_id} update: {status.value} - {stage} ({progress}%)")

    def clear_update_status(self, satellite_id: str):
        """Clear the update status for a satellite after completion or reset"""
        if satellite_id in self.satellites:
            sat = self.satellites[satellite_id]
            sat.update_status = UpdateStatus.NONE
            sat.update_stage = None
            sat.update_progress = 0
            sat.update_error = None

    def get_satellite(self, satellite_id: str) -> Optional[SatelliteInfo]:
        """Get satellite info by ID"""
        return self.satellites.get(satellite_id)

    async def cleanup_stale(self):
        """Remove stale satellites and timed-out sessions"""
        now = time.time()

        async with self._lock:
            # Check for timed-out sessions
            timed_out_sessions = [
                sid for sid, sess in self.sessions.items()
                if now - sess.started_at > sess.max_duration_seconds
            ]

            for session_id in timed_out_sessions:
                logger.warning(f"⏰ Session timed out: {session_id}")
                await self._end_session_internal(session_id, reason="timeout")

            # Check for stale satellites
            stale_satellites = [
                sat_id for sat_id, sat in self.satellites.items()
                if now - sat.last_heartbeat > self.heartbeat_timeout
            ]

            for sat_id in stale_satellites:
                logger.warning(f"💀 Satellite heartbeat timeout: {sat_id}")
                sat = self.satellites[sat_id]
                if sat.current_session_id:
                    await self._end_session_internal(sat.current_session_id, reason="disconnect")
                del self.satellites[sat_id]


# Global singleton instance
_satellite_manager: Optional[SatelliteManager] = None


def get_satellite_manager() -> SatelliteManager:
    """Get or create the global SatelliteManager instance"""
    global _satellite_manager
    if _satellite_manager is None:
        _satellite_manager = SatelliteManager()
    return _satellite_manager
