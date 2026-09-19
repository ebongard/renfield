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
from typing import Any

from fastapi import WebSocket
from loguru import logger

from ha_glue.services.opus_transport import frame_packets
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
    """Hardware capabilities of a satellite.

    Defaults are conservative: a satellite running an older client that
    does not report the richer fields keeps working and simply shows the
    base badges. ``led_type`` is None / ``mic_count`` 1 / has_* False
    until the device reports its real config.
    """
    local_wakeword: bool = True
    speaker: bool = True
    led_count: int = 3
    button: bool = True
    led_type: str | None = None
    # Physical mic count from the hardware, NOT post-DSP capture channels
    # (XVF3800 / AC108 4-mic arrays deliver 1 channel after beamforming).
    mic_count: int = 1
    has_camera: bool = False
    has_display: bool = False
    has_enviro: bool = False


@dataclass
class SatelliteMetrics:
    """Live metrics from satellite heartbeat"""
    audio_rms: float | None = None
    audio_db: float | None = None
    is_speech: bool | None = None
    cpu_percent: float | None = None
    memory_percent: float | None = None
    temperature: float | None = None
    last_wakeword: dict[str, Any] | None = None
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
    current_session_id: str | None = None
    room_id: int | None = None  # Database room ID (populated after DB sync)
    # Whether this connection presented a valid enrollment PSK (security H1).
    # Gates the IRK push and protects an enrolled incumbent from eviction by an
    # unauthenticated newcomer.
    authenticated: bool = False
    language: str = "de"  # Language code for STT/TTS (e.g., 'de', 'en')
    metrics: dict[str, Any] = field(default_factory=dict)  # Live metrics from heartbeat
    # Version and update tracking
    version: str = "unknown"
    update_status: UpdateStatus = UpdateStatus.NONE
    update_stage: str | None = None  # downloading, verifying, backing_up, etc.
    update_progress: int = 0  # 0-100
    update_error: str | None = None
    # When the current update run began. Set while a run is IN_PROGRESS and
    # cleared the moment it reaches a terminal state, so `cleanup_stale` can
    # tell a genuinely running update from one that never terminated (#1209).
    update_started_at: float | None = None


@dataclass
class SatelliteSession:
    """Active voice interaction session"""
    session_id: str
    satellite_id: str
    room: str
    state: SatelliteState
    audio_chunks: list[bytes] = field(default_factory=list)
    audio_sequence: int = 0
    # C1/C2: raw Opus packets for an opus-negotiated connection. The backend
    # buffers packets here (does NOT decode — decode moved to the voice-server,
    # design D6) and ships them at audio_end. Mutually exclusive with
    # audio_chunks (PCM), which the legacy base64/pcm path uses.
    opus_packets: list[bytes] = field(default_factory=list)
    opus_bytes: int = 0
    started_at: float = field(default_factory=time.time)
    transcription: str | None = None
    response_text: str | None = None

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
        self.satellites: dict[str, SatelliteInfo] = {}
        self.sessions: dict[str, SatelliteSession] = {}
        self._lock = asyncio.Lock()
        # On-demand camera snapshot requests: request_id → Future[image_b64|None].
        self._pending_snapshots: dict[str, asyncio.Future] = {}
        self._pending_bt_scans: dict[str, asyncio.Future] = {}
        self._pending_irk_captures: dict[str, asyncio.Future] = {}
        # Strong refs for fire-and-forget duck-on-listen hooks (a bare create_task
        # can be GC'd mid-flight — see the boot-reconnect strand fix).
        self._duck_bg_tasks: set[asyncio.Task] = set()

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
        capabilities: dict[str, Any],
        language: str = "de",
        version: str = "unknown",
        authenticated: bool = False,
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
            authenticated: True if the connection presented a valid enrollment
                PSK (security H1). An enrolled incumbent is NOT evicted by an
                unauthenticated newcomer — that blocks the room-hijack path even
                during the PERMISSIVE soak window.

        Returns:
            True if registration successful, False if refused (e.g. an
            unauthenticated connection tried to evict an authenticated incumbent)
        """
        async with self._lock:
            # Check if satellite already connected (reconnection)
            if satellite_id in self.satellites:
                old_sat = self.satellites[satellite_id]
                if old_sat.authenticated and not authenticated:
                    logger.warning(
                        f"🚫 Refusing to evict authenticated satellite "
                        f"'{satellite_id}' with an UNAUTHENTICATED connection "
                        f"(possible hijack attempt)."
                    )
                    return False
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
                button=capabilities.get("button", True),
                led_type=capabilities.get("led_type"),
                mic_count=capabilities.get("mic_count", 1),
                has_camera=capabilities.get("has_camera", False),
                has_display=capabilities.get("has_display", False),
                has_enviro=capabilities.get("has_enviro", False),
            )

            # Register satellite
            self.satellites[satellite_id] = SatelliteInfo(
                satellite_id=satellite_id,
                room=room,
                websocket=websocket,
                capabilities=caps,
                language=language,
                version=version,
                authenticated=authenticated,
            )

            logger.info(f"✅ Satellite registered: {satellite_id} in {room} (v{version})")
            logger.info(f"   Capabilities: wakeword={caps.local_wakeword}, speaker={caps.speaker}, leds={caps.led_count}")

            # Track event
            self._add_event(satellite_id, "connected", {
                "room": room,
                "capabilities": capabilities
            })

            # Push liveness so the kiosk reinstates a (re)connected satellite.
            # room_id may still be None here (DB sync + set_room_id run after
            # register); the delta carries the room NAME regardless, and the
            # kiosk resolves the precise room_id from the next satellite_state
            # delta / the next snapshot (handled Phase 3, frontend).
            await self._broadcast_satellite_liveness(
                satellite_id, room, self.satellites[satellite_id].room_id, online=True
            )

            return True

    async def unregister(self, satellite_id: str, websocket=None):
        """Remove a satellite from the registry.

        Pass the caller's ``websocket`` so a FAST RECONNECT is handled safely: if
        a new connection already re-registered this id (its ``register`` ran
        before this dying socket's ``finally``), the stored entry belongs to the
        NEW socket — deleting it would drop a live satellite and push a spurious
        ``satellite_offline``. The identity guard skips that. ``websocket=None``
        keeps the legacy unconditional behavior for any caller without it.
        """
        async with self._lock:
            sat = self.satellites.get(satellite_id)
            if sat is None:
                return
            if websocket is not None and sat.websocket is not websocket:
                # A newer connection already replaced this entry — leave it.
                return

            # End any active session
            if sat.current_session_id:
                await self._end_session_internal(sat.current_session_id)

            del self.satellites[satellite_id]
            logger.info(f"👋 Satellite unregistered: {satellite_id}")

            # Push liveness so the kiosk drops the departed satellite.
            await self._broadcast_satellite_liveness(
                satellite_id, sat.room, sat.room_id, online=False
            )

    async def start_session(
        self,
        satellite_id: str,
        keyword: str,
        confidence: float,
        session_id: str | None = None
    ) -> str | None:
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
            await self._set_satellite_state(sat, SatelliteState.LISTENING)

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
    ) -> tuple[bool, str]:
        """
        Buffer an audio chunk from a satellite.

        Args:
            session_id: Active session ID
            chunk_b64: Base64 encoded PCM audio data
            sequence: Sequence number for ordering

        Returns:
            Tuple of (success: bool, error_message: str)
        """
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

        return self.buffer_audio_bytes(session_id, audio_bytes, sequence)

    def buffer_audio_bytes(
        self,
        session_id: str,
        audio_bytes: bytes,
        sequence: int
    ) -> tuple[bool, str]:
        """Buffer an already-decoded PCM chunk.

        Shared tail of the legacy base64 path above and the C1 binary/Opus
        path, which decodes at the backend edge
        (docs/design/voice-identity-wakeword-verification.md §4).
        """
        if session_id not in self.sessions:
            logger.warning(f"⚠️ Audio for unknown session: {session_id}")
            return False, "Unknown session"

        session = self.sessions[session_id]

        # Check buffer size limit
        current_size = sum(len(c) for c in session.audio_chunks)
        if current_size + len(audio_bytes) > settings.ws_max_audio_buffer_size:
            logger.warning(f"⚠️ Audio buffer full for session {session_id}: {current_size} bytes")
            return False, f"Audio buffer full (max: {settings.ws_max_audio_buffer_size} bytes)"

        # Buffer chunk
        session.audio_chunks.append(audio_bytes)
        session.audio_sequence = sequence

        return True, ""

    def buffer_opus_packets(
        self, session_id: str, packets: list[bytes], sequence: int
    ) -> tuple[bool, str]:
        """Buffer raw Opus packets for an opus-negotiated session (C2 Phase 1).

        The backend does NOT decode — it forwards the packets to the voice-server
        at audio_end (decode moved to the media layer, design D6). Size-capped on
        total compressed bytes (Opus at 16 kHz ~ a few KB/s, so this bounds a
        very long or malicious stream well below the PCM cap).
        """
        if session_id not in self.sessions:
            logger.warning(f"⚠️ Opus audio for unknown session: {session_id}")
            return False, "Unknown session"
        session = self.sessions[session_id]
        added = sum(len(p) for p in packets)
        # Reuse the PCM buffer cap as the ceiling — compressed opus is far
        # smaller, so this is a generous but bounded guard.
        if session.opus_bytes + added > settings.ws_max_audio_buffer_size:
            logger.warning(f"⚠️ Opus buffer full for session {session_id}: {session.opus_bytes} bytes")
            return False, f"Audio buffer full (max: {settings.ws_max_audio_buffer_size} bytes)"
        session.opus_packets.extend(packets)
        session.opus_bytes += added
        session.audio_sequence = sequence
        return True, ""

    def get_opus_blob(self, session_id: str) -> bytes | None:
        """Serialize a session's buffered Opus packets to the `[uint16 len][packet]`
        framing the voice-server /api/voice/stt-opus endpoint decodes. None if no
        packets. Framing owned by opus_transport (single source of wire format)."""
        session = self.sessions.get(session_id)
        if not session or not session.opus_packets:
            return None
        return frame_packets(session.opus_packets)

    def has_session(self, session_id: str) -> bool:
        """Whether a session id is currently active (cheap membership test).

        Used by the C1 binary path to validate a frame's session BEFORE
        allocating a stateful Opus decoder, so unknown/stale session ids
        can't leak decoders.
        """
        return session_id in self.sessions

    def get_audio_buffer(self, session_id: str) -> bytes | None:
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

    async def _set_satellite_state(
        self, sat: "SatelliteInfo", state: SatelliteState
    ):
        """Single funnel for every satellite state transition.

        Mutates ``sat.state`` AND pushes a content-free ``satellite_state`` delta
        to the kiosk hub, so any future transition gets the push "for free". The
        broadcast is fire-and-forget: a hub failure must never break voice.
        """
        sat.state = state

        # Duck-on-listen (Phase 4): lower room DLNA media while a satellite listens
        # so the far-field mic doesn't capture the room's own audio (no AEC
        # reference for a networked speaker); restore when the turn ends. Fire-and-
        # forget + best-effort — it must never block or break voice.
        from ha_glue.utils.config import ha_glue_settings

        if ha_glue_settings.duck_on_listen_enabled and sat.room_id is not None:
            if state == SatelliteState.LISTENING:
                self._spawn_duck(sat.room_id, sat.room, duck=True)
            elif state == SatelliteState.IDLE:
                self._spawn_duck(sat.room_id, sat.room, duck=False)

        try:
            from api.websocket.kiosk_handler import broadcast_kiosk_event

            await broadcast_kiosk_event(
                {
                    "type": "satellite_state",
                    "satellite_id": sat.satellite_id,
                    "room": sat.room,
                    "room_id": sat.room_id,
                    "state": state.value,
                }
            )
        except Exception as e:
            logger.debug(f"kiosk satellite_state broadcast failed: {e}")

    def _spawn_duck(self, room_id: int, room_name: str | None, duck: bool) -> None:
        """Fire-and-forget duck (True) or restore (False) of room media, holding a
        strong ref so the task isn't GC'd mid-flight."""
        try:
            from ha_glue.services.duck_service import get_duck_service

            ds = get_duck_service()
            coro = ds.duck_room(room_id, room_name) if duck else ds.restore_room(room_id)
            task = asyncio.create_task(coro)
            self._duck_bg_tasks.add(task)
            task.add_done_callback(self._duck_bg_tasks.discard)
        except Exception as e:
            logger.debug(f"duck-on-listen spawn failed for room {room_id}: {e}")

    async def _broadcast_satellite_liveness(
        self, satellite_id: str, room: str | None, room_id: int | None, online: bool
    ):
        """Push a content-free ``satellite_online``/``satellite_offline`` delta.

        This is the Phase-2 LIVENESS signal — distinct from ``satellite_state``
        (which only carries the SESSION state of an already-connected satellite).
        It fires when a satellite REGISTERS (connect/reconnect → online) or DROPS
        (unregister / heartbeat-timeout → offline), so the kiosk drops a crashed
        satellite out of the constellation (stops it pinning the voice core) and
        reinstates a resumed one, instead of decaying frozen roster data against
        the wall clock. Fire-and-forget: a hub failure must never break voice.
        Called under ``self._lock`` — safe because ``broadcast_kiosk_event`` only
        enqueues (the hub offloads the real send).
        """
        try:
            from api.websocket.kiosk_handler import broadcast_kiosk_event

            await broadcast_kiosk_event(
                {
                    "type": "satellite_online" if online else "satellite_offline",
                    "satellite_id": satellite_id,
                    "room": room,
                    "room_id": room_id,
                    "online": online,
                }
            )
        except Exception as e:
            logger.debug(f"kiosk satellite liveness broadcast failed: {e}")

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
            await self._set_satellite_state(sat, state)

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
        intent: dict[str, Any],
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
            await self._set_satellite_state(sat, SatelliteState.IDLE)

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

    def update_heartbeat(self, satellite_id: str, metrics: dict[str, Any] | None = None, version: str | None = None):
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

    def _add_event(self, satellite_id: str, event_type: str, details: dict[str, Any] = None):
        """Add an event to satellite history"""
        if not hasattr(self, "_satellite_history"):
            self._satellite_history: dict[str, list[dict[str, Any]]] = {}

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
            self._satellite_stats: dict[str, dict[str, Any]] = {}

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

    def get_satellite_by_session(self, session_id: str) -> SatelliteInfo | None:
        """Get satellite info for a session"""
        if session_id not in self.sessions:
            return None

        session = self.sessions[session_id]
        return self.satellites.get(session.satellite_id)

    def get_session(self, session_id: str) -> SatelliteSession | None:
        """Get session by ID"""
        return self.sessions.get(session_id)

    def get_all_satellites(self) -> list[dict[str, Any]]:
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
                    "led_count": sat.capabilities.led_count,
                    "led_type": sat.capabilities.led_type,
                    "mic_count": sat.capabilities.mic_count,
                    "has_camera": sat.capabilities.has_camera,
                    "has_display": sat.capabilities.has_display,
                    "has_enviro": sat.capabilities.has_enviro,
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
        stage: str | None = None,
        progress: int = 0,
        error: str | None = None
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
            # Stamp the start of a run, and clear it on any terminal state. The
            # first IN_PROGRESS write wins, so progress frames during a run do
            # not keep pushing the deadline out — otherwise a satellite that
            # reports progress forever would never time out.
            if status == UpdateStatus.IN_PROGRESS:
                if sat.update_started_at is None:
                    sat.update_started_at = time.time()
            else:
                sat.update_started_at = None
            logger.info(f"📡 Satellite {satellite_id} update: {status.value} - {stage} ({progress}%)")

    # Stages the satellite sends as ordinary progress frames but which END a
    # run — each mapped to the state it ends in:
    #   * `failed`       — reported with the real cause before the rollback starts
    #   * `rolling_back` — the last frame a rolled-back run emits
    #   * `completed`    — reported at 100% BEFORE the restart, which usually kills
    #                      the process before `update_complete` can be sent
    #                      (update_manager.py:299 + satellite.py's own comment).
    #                      Leaving it IN_PROGRESS would let the stuck-run timeout
    #                      mark a SUCCESSFUL update as failed 15 minutes later.
    _TERMINAL_UPDATE_STAGES = {
        "failed": UpdateStatus.FAILED,
        "rolling_back": UpdateStatus.FAILED,
        "completed": UpdateStatus.COMPLETED,
    }

    # A device-supplied message becomes durable state served by the ADMIN API.
    # Bound it: the WS frame limit is 1 MB, and a malfunctioning or hostile
    # satellite must not be able to park that in the roster.
    _MAX_UPDATE_ERROR_CHARS = 2000

    @classmethod
    def _bounded_error(cls, message: object) -> str | None:
        """Coerce a device-supplied message into a bounded string, or None.

        The frame is JSON from a LAN device, so `message` may be any type. The
        REST response models type `update_error` as `str | None` and Pydantic v2
        does not coerce — an int or dict here would raise at response-build time,
        and `list_satellites` has no per-entry guard, so ONE bad entry 500s the
        whole admin list.
        """
        if message is None or message == "":
            return None
        text = message if isinstance(message, str) else str(message)
        return text[:cls._MAX_UPDATE_ERROR_CHARS]

    def apply_update_progress(
        self, satellite_id: str, stage: str, progress: int, message: str = ""
    ) -> None:
        """Fold a satellite `update_progress` frame into the run state.

        Two rules the old unconditional IN_PROGRESS write got wrong (#1209):

        1. A terminal stage ends the run here. The satellite may send nothing
           after `rolling_back`, so waiting for an `update_failed` that never
           comes left the status pinned at in_progress. The frame's message is
           the real cause and is kept — the old write passed `error=None` and
           so ERASED it, which is why the stuck rows showed a hanging update
           with no reason attached.
        2. A run that already ended is not dragged back. On the satellite the
           progress sends are scheduled fire-and-forget while the terminal
           message is awaited, so a frame arriving after the end is the normal
           case, not an anomaly.
        """
        sat = self.satellites.get(satellite_id)
        if sat is None:
            return

        # ONE guard, ahead of BOTH branches. A finished run is never dragged
        # back — not by an ordinary frame, and not by a duplicate terminal frame
        # either. This ordering is also what PRESERVES the failure cause: the
        # satellite reports `failed` carrying the real reason and only then
        # `rolling_back` with the fixed text "Rolling back..."
        # (update_manager.py:762), which would otherwise overwrite it on every
        # single rollback.
        if sat.update_status in (UpdateStatus.COMPLETED, UpdateStatus.FAILED):
            logger.debug(
                f"Späte update_progress von {satellite_id} verworfen "
                f"(Stufe: {stage}) — Lauf bereits {sat.update_status.value}"
            )
            return

        # The frame is JSON from a LAN device; neither field is trustworthy.
        stage = stage if isinstance(stage, str) else str(stage)
        progress = progress if isinstance(progress, int) and not isinstance(progress, bool) else 0

        terminal = self._TERMINAL_UPDATE_STAGES.get(stage)
        if terminal is not None:
            self.set_update_status(
                satellite_id,
                terminal,
                stage=stage,
                progress=progress,
                # A successful run carries no error; a failed one keeps the
                # device's own words, bounded.
                error=self._bounded_error(message) if terminal is UpdateStatus.FAILED else None,
            )
            return

        self.set_update_status(
            satellite_id, UpdateStatus.IN_PROGRESS, stage=stage, progress=progress
        )

    def clear_update_status(self, satellite_id: str):
        """Clear the update status for a satellite after completion or reset"""
        if satellite_id in self.satellites:
            sat = self.satellites[satellite_id]
            sat.update_status = UpdateStatus.NONE
            sat.update_stage = None
            sat.update_progress = 0
            sat.update_error = None
            # Without this a LATER run inherits an already-expired deadline and
            # is killed by the stuck-run sweep the moment it starts.
            sat.update_started_at = None

    def get_satellite(self, satellite_id: str) -> SatelliteInfo | None:
        """Get satellite info by ID"""
        return self.satellites.get(satellite_id)

    def is_connected(self, satellite_id: str) -> bool:
        """Whether a satellite currently has a live WS connection (this pod)."""
        return satellite_id in self.satellites

    def get_camera_satellite_for_room(self, room_id: int) -> SatelliteInfo | None:
        """Return a connected satellite in this room that has a camera, if any."""
        if room_id is None:
            return None
        for sat in self.satellites.values():
            if sat.room_id == room_id and sat.capabilities.has_camera:
                return sat
        return None

    async def request_snapshot(self, satellite_id: str, timeout: float = 8.0) -> str | None:
        """Ask a satellite to capture a camera snapshot NOW and return it (base64
        JPEG), or None on no-camera/timeout/error. Backend→satellite request over
        the WS; the reply arrives as a 'snapshot_result' message (resolved via
        resolve_snapshot). The image is transient — never persisted."""
        sat = self.satellites.get(satellite_id)
        if sat is None or not sat.capabilities.has_camera:
            return None
        request_id = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_snapshots[request_id] = fut
        try:
            await sat.websocket.send_json({
                "type": "capture_snapshot", "request_id": request_id,
            })
            return await asyncio.wait_for(fut, timeout=timeout)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            return None
        finally:
            self._pending_snapshots.pop(request_id, None)

    def resolve_snapshot(self, request_id: str, image_b64: str | None) -> None:
        """Resolve a pending request_snapshot() future with the satellite's reply."""
        fut = self._pending_snapshots.get(request_id)
        if fut is not None and not fut.done():
            fut.set_result(image_b64)

    async def request_bt_scan(
        self, satellite_id: str, params: dict | None = None, timeout: float = 30.0
    ) -> list | None:
        """Ask a satellite to run a broad Bluetooth discovery scan NOW and return
        the discovered device list, or None on unknown-satellite/timeout/error.
        Backend→satellite request over the WS; the reply arrives as a
        'bt_scan_result' message (resolved via resolve_bt_scan). Mirrors
        request_snapshot."""
        sat = self.satellites.get(satellite_id)
        if sat is None:
            return None
        request_id = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_bt_scans[request_id] = fut
        try:
            await sat.websocket.send_json({
                "type": "bt_scan_request",
                "request_id": request_id,
                "params": params or {},
            })
            return await asyncio.wait_for(fut, timeout=timeout)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            return None
        finally:
            self._pending_bt_scans.pop(request_id, None)

    def resolve_bt_scan(
        self, request_id: str, devices: list | None, error: str | None
    ) -> None:
        """Resolve a pending request_bt_scan() future with the satellite's reply.
        On a satellite-side error the device list is treated as empty."""
        fut = self._pending_bt_scans.get(request_id)
        if fut is not None and not fut.done():
            fut.set_result([] if error else (devices or []))

    async def request_irk_capture(
        self, satellite_id: str, label: str, window_seconds: int = 60,
        timeout: float | None = None,
    ) -> dict:
        """Ask a satellite to open a one-time pairing window and capture a phone's
        IRK. Returns {'irk','mac','name'} on success, {} on no-bond-in-window, or
        {'error': ...} on failure/timeout. Mirrors request_bt_scan; the reply
        arrives as an 'irk_capture_result' message (resolved via resolve_irk_capture)."""
        sat = self.satellites.get(satellite_id)
        if sat is None:
            return {"error": "satellite not connected"}
        if timeout is None:
            timeout = window_seconds + 15.0
        request_id = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_irk_captures[request_id] = fut
        try:
            await sat.websocket.send_json({
                "type": "irk_capture_request",
                "request_id": request_id,
                "params": {"label": label, "window_seconds": window_seconds},
            })
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            return {"error": "timed out waiting for the satellite"}
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}
        finally:
            self._pending_irk_captures.pop(request_id, None)

    def resolve_irk_capture(
        self, request_id: str, result: dict | None, error: str | None
    ) -> None:
        """Resolve a pending request_irk_capture() future with the satellite's reply."""
        fut = self._pending_irk_captures.get(request_id)
        if fut is not None and not fut.done():
            fut.set_result({"error": error} if error else (result or {}))

    async def cleanup_stale(self):
        """Time out recordings, evict dead satellites, fail stuck OTA runs.

        Three independent sweeps. Until #1209 this method had NO production
        caller anywhere in the tree — only a test invoked it — so none of them
        ever ran. The scheduler in `ha_glue.bootstrap` is what gives them
        effect, which is also why each is guarded here rather than trusted:
        they were written against assumptions no live system ever exercised.
        """
        now = time.time()

        async with self._lock:
            # 1. RECORDING timeout — not a turn timeout. `started_at` is stamped
            # at the wake word, and the whole turn (STT + agent + LLM + TTS) is
            # processed INLINE in the socket's receive loop, so timing the turn
            # out would destroy the session mid-answer; `send_tts_audio` then
            # drops the finished answer with `if session_id not in self.sessions:
            # return` — silently, with the satellite already back at idle. Only a
            # session still LISTENING can expire, bounded by the configurable
            # `device_session_timeout` (assigned since 2026-01 and never read).
            timed_out_sessions = [
                sid for sid, sess in self.sessions.items()
                if sess.state == SatelliteState.LISTENING
                and now - sess.started_at > self.session_timeout
            ]

            for session_id in timed_out_sessions:
                logger.warning(f"⏰ Recording timed out: {session_id}")
                await self._end_session_internal(session_id, reason="timeout")

            # 2. Heartbeat eviction, with two exemptions for devices that are
            # demonstrably alive but legitimately unable to answer:
            #   * a LIVE session — we are processing its audio right now, and its
            #     heartbeats sit unread in the socket buffer because the turn
            #     runs inline in the receive loop. (Keyed on the session really
            #     existing, so a dangling id cannot grant permanent immunity.)
            #   * a running OTA — the installer blocks the satellite's event loop
            #     for up to ~150s (pip 120s + systemctl restart 30s,
            #     update_manager.py:727/738) against a 60s deadline. Evicting
            #     there would delete the row that OWNS `update_started_at` and
            #     disarm the stuck-run timeout in exactly the case it exists for.
            stale_satellites = [
                sat_id for sat_id, sat in self.satellites.items()
                if now - sat.last_heartbeat > self.heartbeat_timeout
                and not (sat.current_session_id and sat.current_session_id in self.sessions)
                and sat.update_status != UpdateStatus.IN_PROGRESS
            ]

            for sat_id in stale_satellites:
                logger.warning(f"💀 Satellite heartbeat timeout: {sat_id}")
                sat = self.satellites[sat_id]
                if sat.current_session_id:
                    await self._end_session_internal(sat.current_session_id, reason="disconnect")
                room, room_id, websocket = sat.room, sat.room_id, sat.websocket
                del self.satellites[sat_id]
                # Push liveness so the kiosk drops a satellite that timed out.
                await self._broadcast_satellite_liveness(
                    sat_id, room, room_id, online=False
                )
                # CLOSE the socket. Removing the roster entry alone leaves the
                # receive loop running and still acking heartbeats, so the device
                # sees a healthy link, never re-registers (it only registers on
                # connect) and is mute FOREVER. Closing drops it into its own
                # reconnect-with-backoff loop.
                try:
                    await websocket.close(code=1001, reason="heartbeat timeout")
                except Exception:  # noqa: BLE001
                    pass

            # Time-bound a stuck OTA run (#1209). The handler now terminates the
            # stages the satellite actually reports, so this is the backstop
            # beneath it, not a substitute: it catches the run whose final frame
            # never arrived at all — the connection dropped mid-install, or the
            # satellite died between the last progress and its terminal message.
            # Without it such a run reads "wird aktualisiert" until the pod
            # restarts, which is exactly the blind spot the issue describes.
            from ha_glue.utils.config import ha_glue_settings

            update_timeout = ha_glue_settings.satellite_update_timeout
            for sat_id, sat in self.satellites.items():
                if (
                    sat.update_status == UpdateStatus.IN_PROGRESS
                    and sat.update_started_at is not None
                    and now - sat.update_started_at > update_timeout
                ):
                    logger.warning(
                        f"⏰ Update ohne Endzustand: {sat_id} "
                        f"(Stufe: {sat.update_stage or 'unbekannt'}, "
                        f"{update_timeout:.0f}s ohne Abschluss)"
                    )
                    # Go through the single writer rather than touching the
                    # four fields by hand: it owns the start-mark bookkeeping,
                    # and duplicating that here is what let `clear_update_status`
                    # drift out of sync in the first place.
                    self.set_update_status(
                        sat_id,
                        UpdateStatus.FAILED,
                        stage=sat.update_stage,
                        progress=sat.update_progress,
                        # Keep a cause the satellite already gave us; only invent
                        # one when there is none, and say plainly that the verdict
                        # is ours, not the device's.
                        error=sat.update_error or (
                            f"Update ohne Endzustand abgebrochen (letzte Stufe: "
                            f"{sat.update_stage or 'unbekannt'})"
                        ),
                    )


# Global singleton instance
_satellite_manager: SatelliteManager | None = None


def get_satellite_manager() -> SatelliteManager:
    """Get or create the global SatelliteManager instance"""
    global _satellite_manager
    if _satellite_manager is None:
        _satellite_manager = SatelliteManager()
    return _satellite_manager
