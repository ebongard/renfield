"""
Device Manager Service for Renfield

Manages all connected devices (physical satellites and web clients) via WebSocket.
Provides unified handling for device registration, sessions, audio streaming,
and room-based operations.

Features:
- Unified device registration (satellites, web panels, tablets, browsers)
- Session-based routing for multi-room support
- Concurrent request handling (different rooms in parallel)
- First-speaker-wins for same-room conflicts
- Audio buffer management for streaming
- Capability-aware response routing
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

from models.database import (
    DEVICE_TYPE_SATELLITE, DEVICE_TYPE_WEB_BROWSER, DEVICE_TYPE_WEB_PANEL,
    DEVICE_TYPE_WEB_TABLET, DEVICE_TYPE_WEB_KIOSK
)
from utils.config import settings


class DeviceState(str, Enum):
    """Device operational states"""
    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"
    SPEAKING = "speaking"
    ERROR = "error"


@dataclass
class DeviceCapabilities:
    """Runtime capabilities of a connected device"""
    # Audio
    has_microphone: bool = False
    has_speaker: bool = False
    has_wakeword: bool = False
    wakeword_method: Optional[str] = None  # "openwakeword", "browser_wasm"

    # Visual
    has_display: bool = False
    display_size: Optional[str] = None  # "small", "medium", "large"
    supports_notifications: bool = False

    # Hardware (satellites only)
    has_leds: bool = False
    led_count: int = 0
    has_button: bool = False

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DeviceCapabilities":
        """Create from dictionary"""
        return cls(
            has_microphone=data.get("has_microphone", False),
            has_speaker=data.get("has_speaker", False),
            has_wakeword=data.get("has_wakeword", False),
            wakeword_method=data.get("wakeword_method"),
            has_display=data.get("has_display", False),
            display_size=data.get("display_size"),
            supports_notifications=data.get("supports_notifications", False),
            has_leds=data.get("has_leds", False),
            led_count=data.get("led_count", 0),
            has_button=data.get("has_button", False),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "has_microphone": self.has_microphone,
            "has_speaker": self.has_speaker,
            "has_wakeword": self.has_wakeword,
            "wakeword_method": self.wakeword_method,
            "has_display": self.has_display,
            "display_size": self.display_size,
            "supports_notifications": self.supports_notifications,
            "has_leds": self.has_leds,
            "led_count": self.led_count,
            "has_button": self.has_button,
        }


@dataclass
class ConnectedDevice:
    """Information about a connected device"""
    device_id: str
    device_type: str
    device_name: Optional[str]
    room: str
    room_id: Optional[int]
    websocket: WebSocket
    capabilities: DeviceCapabilities
    state: DeviceState = DeviceState.IDLE
    connected_at: float = field(default_factory=time.time)
    last_heartbeat: float = field(default_factory=time.time)
    current_session_id: Optional[str] = None
    is_stationary: bool = True
    user_agent: Optional[str] = None
    ip_address: Optional[str] = None

    @property
    def is_satellite(self) -> bool:
        return self.device_type == DEVICE_TYPE_SATELLITE

    @property
    def is_web_device(self) -> bool:
        return self.device_type in [
            DEVICE_TYPE_WEB_BROWSER,
            DEVICE_TYPE_WEB_PANEL,
            DEVICE_TYPE_WEB_TABLET,
            DEVICE_TYPE_WEB_KIOSK
        ]


@dataclass
class DeviceSession:
    """Active voice interaction session"""
    session_id: str
    device_id: str
    device_type: str
    room: str
    room_id: Optional[int]
    state: DeviceState
    audio_chunks: List[bytes] = field(default_factory=list)
    audio_sequence: int = 0
    started_at: float = field(default_factory=time.time)
    transcription: Optional[str] = None
    response_text: Optional[str] = None
    speaker_name: Optional[str] = None
    speaker_alias: Optional[str] = None

    # Timeout settings
    max_duration_seconds: float = 30.0


class DeviceManager:
    """
    Manages all connected devices via WebSocket.

    Handles:
    - Device registration and lifecycle
    - Session creation and routing
    - Audio buffer management
    - State synchronization
    - Room-based broadcasting
    """

    def __init__(self):
        self.devices: Dict[str, ConnectedDevice] = {}
        self.sessions: Dict[str, DeviceSession] = {}
        self._lock = asyncio.Lock()

        # Configuration
        from utils.config import settings
        self.default_wake_words = [settings.wake_word_default]
        self.default_threshold = settings.wake_word_threshold
        self.session_timeout = settings.device_session_timeout
        self.heartbeat_timeout = settings.device_heartbeat_timeout

        logger.info("📱 DeviceManager initialized")

    async def register(
        self,
        device_id: str,
        device_type: str,
        room: str,
        websocket: WebSocket,
        capabilities: Dict[str, Any],
        device_name: Optional[str] = None,
        is_stationary: bool = True,
        user_agent: Optional[str] = None,
        ip_address: Optional[str] = None
    ) -> bool:
        """
        Register a new device connection.

        Args:
            device_id: Unique identifier for the device
            device_type: Type of device (satellite, web_panel, etc.)
            room: Room name where device is located
            websocket: WebSocket connection to device
            capabilities: Device capabilities dict
            device_name: User-friendly name
            is_stationary: Whether device is stationary
            user_agent: Browser/client info
            ip_address: Client IP

        Returns:
            True if registration successful
        """
        async with self._lock:
            # Check if device already connected (reconnection)
            if device_id in self.devices:
                old_device = self.devices[device_id]
                logger.info(f"📱 Device {device_id} reconnecting (was in room: {old_device.room})")
                # Close old connection if still open
                try:
                    await old_device.websocket.close()
                except Exception:
                    pass  # Connection may already be closed

            # Create capability object
            caps = DeviceCapabilities.from_dict(capabilities)

            # Register device
            self.devices[device_id] = ConnectedDevice(
                device_id=device_id,
                device_type=device_type,
                device_name=device_name,
                room=room,
                room_id=None,  # Set later after DB sync
                websocket=websocket,
                capabilities=caps,
                is_stationary=is_stationary,
                user_agent=user_agent,
                ip_address=ip_address
            )

            type_emoji = "📡" if device_type == DEVICE_TYPE_SATELLITE else "📱"
            logger.info(f"{type_emoji} Device registered: {device_id} ({device_type}) in {room}")
            logger.debug(f"   Capabilities: mic={caps.has_microphone}, speaker={caps.has_speaker}, display={caps.has_display}")

            return True

    async def unregister(self, device_id: str):
        """Remove a device from the registry"""
        async with self._lock:
            if device_id in self.devices:
                device = self.devices[device_id]

                # End any active session
                if device.current_session_id:
                    await self._end_session_internal(device.current_session_id)

                del self.devices[device_id]
                logger.info(f"👋 Device unregistered: {device_id}")

    def set_room_id(self, device_id: str, room_id: int):
        """Set the database room ID for a device after DB sync"""
        if device_id in self.devices:
            self.devices[device_id].room_id = room_id

    async def start_session(
        self,
        device_id: str,
        keyword: Optional[str] = None,
        confidence: float = 0.0,
        session_id: Optional[str] = None
    ) -> Optional[str]:
        """
        Start a new voice interaction session.

        Args:
            device_id: ID of the device that initiated
            keyword: Wake word that was detected (optional)
            confidence: Detection confidence score
            session_id: Optional session ID (generated if not provided)

        Returns:
            Session ID if started, None if device is busy
        """
        async with self._lock:
            if device_id not in self.devices:
                logger.warning(f"⚠️ Unknown device tried to start session: {device_id}")
                return None

            device = self.devices[device_id]

            # Check if device already has active session
            if device.current_session_id:
                logger.warning(f"⚠️ Device {device_id} already has active session")
                return None

            # Generate session ID
            if not session_id:
                session_id = f"{device_id}-{uuid.uuid4().hex[:8]}"

            # Create session
            session = DeviceSession(
                session_id=session_id,
                device_id=device_id,
                device_type=device.device_type,
                room=device.room,
                room_id=device.room_id,
                state=DeviceState.LISTENING
            )

            self.sessions[session_id] = session
            device.current_session_id = session_id
            device.state = DeviceState.LISTENING

            trigger = f"wake word '{keyword}' ({confidence:.2f})" if keyword else "manual trigger"
            logger.info(f"🎙️ Session started: {session_id}")
            logger.info(f"   Room: {device.room}, Trigger: {trigger}")

            return session_id

    def buffer_audio(
        self,
        session_id: str,
        chunk_b64: str,
        sequence: int
    ) -> Tuple[bool, str]:
        """
        Buffer an audio chunk from a device.

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
        """Get the complete audio buffer for a session."""
        if session_id not in self.sessions:
            return None

        session = self.sessions[session_id]

        if not session.audio_chunks:
            return None

        return b"".join(session.audio_chunks)

    async def set_session_state(
        self,
        session_id: str,
        state: DeviceState
    ):
        """Update session state and notify device"""
        if session_id not in self.sessions:
            return

        session = self.sessions[session_id]
        session.state = state

        # Update device state too
        if session.device_id in self.devices:
            device = self.devices[session.device_id]
            device.state = state

            # Notify device of state change
            try:
                await device.websocket.send_json({
                    "type": "state",
                    "state": state.value
                })
            except Exception as e:
                logger.error(f"❌ Failed to send state to device: {e}")

    async def send_transcription(
        self,
        session_id: str,
        text: str,
        speaker_name: Optional[str] = None,
        speaker_alias: Optional[str] = None
    ):
        """Send transcription result to device"""
        if session_id not in self.sessions:
            return

        session = self.sessions[session_id]
        session.transcription = text
        session.speaker_name = speaker_name
        session.speaker_alias = speaker_alias

        if session.device_id in self.devices:
            device = self.devices[session.device_id]
            try:
                await device.websocket.send_json({
                    "type": "transcription",
                    "session_id": session_id,
                    "text": text,
                    "speaker_name": speaker_name,
                    "speaker_alias": speaker_alias
                })
            except Exception as e:
                logger.error(f"❌ Failed to send transcription: {e}")

    async def send_action_result(
        self,
        session_id: str,
        intent: Dict[str, Any],
        success: bool
    ):
        """Send action execution result to device"""
        if session_id not in self.sessions:
            return

        session = self.sessions[session_id]

        if session.device_id in self.devices:
            device = self.devices[session.device_id]
            try:
                await device.websocket.send_json({
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
        Send TTS audio to device for playback.

        Only sends to devices with speaker capability.
        """
        if session_id not in self.sessions:
            return

        session = self.sessions[session_id]

        if session.device_id in self.devices:
            device = self.devices[session.device_id]

            # Check capability
            if not device.capabilities.has_speaker:
                logger.debug(f"📵 Device {device.device_id} has no speaker, skipping TTS")
                return

            # Update state to speaking
            if device.state != DeviceState.SPEAKING:
                await self.set_session_state(session_id, DeviceState.SPEAKING)

            try:
                audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")

                await device.websocket.send_json({
                    "type": "tts_audio",
                    "session_id": session_id,
                    "audio": audio_b64,
                    "is_final": is_final
                })

                logger.info(f"🔊 Sent TTS audio to {device.device_id} ({len(audio_bytes)} bytes)")

            except Exception as e:
                logger.error(f"❌ Failed to send TTS audio: {e}")

    async def send_response_text(
        self,
        session_id: str,
        text: str,
        is_final: bool = True
    ):
        """
        Send text response to device (for display-based devices).

        Useful for web clients that show responses visually.
        """
        if session_id not in self.sessions:
            return

        session = self.sessions[session_id]
        session.response_text = text

        if session.device_id in self.devices:
            device = self.devices[session.device_id]

            try:
                await device.websocket.send_json({
                    "type": "response_text",
                    "session_id": session_id,
                    "text": text,
                    "is_final": is_final
                })
            except Exception as e:
                logger.error(f"❌ Failed to send response text: {e}")

    async def send_stream_chunk(
        self,
        session_id: str,
        chunk: str
    ):
        """Send streaming response chunk to device"""
        if session_id not in self.sessions:
            return

        session = self.sessions[session_id]

        if session.device_id in self.devices:
            device = self.devices[session.device_id]

            try:
                await device.websocket.send_json({
                    "type": "stream",
                    "session_id": session_id,
                    "content": chunk
                })
            except Exception as e:
                logger.error(f"❌ Failed to send stream chunk: {e}")

    async def end_session(self, session_id: str, reason: str = "completed"):
        """End an active session."""
        async with self._lock:
            await self._end_session_internal(session_id, reason)

    async def _end_session_internal(self, session_id: str, reason: str = "completed"):
        """Internal session end without lock"""
        if session_id not in self.sessions:
            return

        session = self.sessions[session_id]

        # Clear device's current session
        if session.device_id in self.devices:
            device = self.devices[session.device_id]
            device.current_session_id = None
            device.state = DeviceState.IDLE

            # Notify device to return to idle
            try:
                await device.websocket.send_json({
                    "type": "session_end",
                    "session_id": session_id,
                    "reason": reason
                })
                await device.websocket.send_json({
                    "type": "state",
                    "state": "idle"
                })
            except Exception:
                pass  # Device may have disconnected

        # Calculate duration
        duration = time.time() - session.started_at

        # Remove session
        del self.sessions[session_id]

        logger.info(f"✅ Session ended: {session_id} ({reason}, {duration:.1f}s)")

    def update_heartbeat(self, device_id: str):
        """Update device heartbeat timestamp"""
        if device_id in self.devices:
            self.devices[device_id].last_heartbeat = time.time()

    def get_device(self, device_id: str) -> Optional[ConnectedDevice]:
        """Get device info by ID"""
        return self.devices.get(device_id)

    def get_session(self, session_id: str) -> Optional[DeviceSession]:
        """Get session by ID"""
        return self.sessions.get(session_id)

    def get_device_by_session(self, session_id: str) -> Optional[ConnectedDevice]:
        """Get device info for a session"""
        if session_id not in self.sessions:
            return None

        session = self.sessions[session_id]
        return self.devices.get(session.device_id)

    def get_devices_in_room(self, room: str) -> List[ConnectedDevice]:
        """Get all connected devices in a room"""
        return [d for d in self.devices.values() if d.room == room]

    def get_devices_in_room_by_id(self, room_id: int) -> List[ConnectedDevice]:
        """Get all connected devices in a room by room ID"""
        return [d for d in self.devices.values() if d.room_id == room_id]

    async def broadcast_to_room(
        self,
        room: str,
        message: Dict[str, Any],
        exclude_device_id: Optional[str] = None,
        require_capability: Optional[str] = None
    ):
        """
        Broadcast a message to all devices in a room.

        Args:
            room: Room name
            message: Message to broadcast
            exclude_device_id: Device to exclude from broadcast
            require_capability: Only send to devices with this capability
        """
        devices = self.get_devices_in_room(room)

        for device in devices:
            if device.device_id == exclude_device_id:
                continue

            if require_capability:
                if not device.capabilities.to_dict().get(require_capability, False):
                    continue

            try:
                await device.websocket.send_json(message)
            except Exception as e:
                logger.error(f"❌ Failed to broadcast to {device.device_id}: {e}")

    def get_all_devices(self) -> List[Dict[str, Any]]:
        """Get status of all connected devices"""
        result = []
        for device_id, device in self.devices.items():
            result.append({
                "device_id": device_id,
                "device_type": device.device_type,
                "device_name": device.device_name,
                "room": device.room,
                "room_id": device.room_id,
                "state": device.state.value,
                "connected_at": device.connected_at,
                "last_heartbeat": device.last_heartbeat,
                "has_active_session": device.current_session_id is not None,
                "is_stationary": device.is_stationary,
                "capabilities": device.capabilities.to_dict()
            })
        return result

    async def cleanup_stale(self):
        """Remove stale devices and timed-out sessions"""
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

            # Check for stale devices
            stale_devices = [
                dev_id for dev_id, dev in self.devices.items()
                if now - dev.last_heartbeat > self.heartbeat_timeout
            ]

            for device_id in stale_devices:
                logger.warning(f"💀 Device heartbeat timeout: {device_id}")
                device = self.devices[device_id]
                if device.current_session_id:
                    await self._end_session_internal(device.current_session_id, reason="disconnect")
                del self.devices[device_id]


# Global singleton instance
_device_manager: Optional[DeviceManager] = None


def get_device_manager() -> DeviceManager:
    """Get or create the global DeviceManager instance"""
    global _device_manager
    if _device_manager is None:
        _device_manager = DeviceManager()
    return _device_manager


# Legacy compatibility aliases
SatelliteState = DeviceState
SatelliteCapabilities = DeviceCapabilities
SatelliteInfo = ConnectedDevice
SatelliteSession = DeviceSession
SatelliteManager = DeviceManager
get_satellite_manager = get_device_manager
