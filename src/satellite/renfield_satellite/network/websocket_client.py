"""
WebSocket Client for Renfield Satellite

Handles communication with the Renfield backend server.
Provides auto-reconnection and message routing.
"""

import asyncio
import base64
import json
import ssl
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from renfield_satellite import __version__

try:
    import websockets
    from websockets.client import WebSocketClientProtocol
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    websockets = None
    WebSocketClientProtocol = None
    WEBSOCKETS_AVAILABLE = False
    print("Warning: websockets not installed. Network disabled.")


# Maximum TTS audio payload size (2MB) to prevent OOM on constrained devices
MAX_AUDIO_PAYLOAD_BYTES = 2 * 1024 * 1024


class ConnectionState(str, Enum):
    """WebSocket connection states"""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"


@dataclass
class ServerConfig:
    """Configuration received from server"""
    wake_words: List[str]
    threshold: float
    cooldown_ms: int = 2000
    protocol_version: str = "1.0"
    room_id: Optional[int] = None


class WebSocketClient:
    """
    WebSocket client for satellite-server communication.

    Handles:
    - Auto-reconnection with exponential backoff
    - Message serialization/deserialization
    - Heartbeat management
    - Event callbacks for incoming messages
    """

    def __init__(
        self,
        satellite_id: str,
        room: str,
        server_url: Optional[str] = None,
        reconnect_interval: int = 5,
        heartbeat_interval: int = 30,
        language: str = "de",
    ):
        """
        Initialize WebSocket client.

        Args:
            satellite_id: Unique satellite identifier
            room: Room name for this satellite
            server_url: WebSocket URL (ws://...) - optional if using auto-discovery
            reconnect_interval: Seconds between reconnect attempts
            heartbeat_interval: Seconds between heartbeats
            language: Language code for STT/TTS (e.g., 'de', 'en')
        """
        self.server_url = server_url
        self.satellite_id = satellite_id
        self.room = room
        self.reconnect_interval = reconnect_interval
        self.heartbeat_interval = heartbeat_interval
        self.language = language

        self._ws: Optional["WebSocketClientProtocol"] = None
        self._state = ConnectionState.DISCONNECTED
        self._running = False
        self._server_config: Optional[ServerConfig] = None

        # Authentication
        self._auth_token: Optional[str] = None

        # TLS verification
        self._verify_tls: bool = True

        # Protocol version
        self._protocol_version: str = "1.0"

        # Callbacks
        self._on_state_change: Optional[Callable[[str], None]] = None
        self._get_metrics: Optional[Callable[[], Dict[str, Any]]] = None  # Callback to get current metrics
        self._on_transcription: Optional[Callable[[str, str], None]] = None
        self._on_action: Optional[Callable[[str, Dict, bool], None]] = None
        self._on_tts_audio: Optional[Callable[[str, bytes, bool], None]] = None
        self._on_connected: Optional[Callable[[ServerConfig], None]] = None
        self._on_disconnected: Optional[Callable[[], None]] = None
        self._on_error: Optional[Callable[[str], None]] = None
        self._on_config_update: Optional[Callable[[ServerConfig], None]] = None
        self._on_update_request: Optional[Callable[[str, str, str, int], None]] = None  # version, url, checksum, size

        # Tasks
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._receive_task: Optional[asyncio.Task] = None

        # Session tracking
        self._current_session_id: Optional[str] = None
        self._audio_sequence: int = 0
        self._start_time: float = time.time()

    @property
    def is_connected(self) -> bool:
        """Check if connected to server"""
        return self._state == ConnectionState.CONNECTED and self._ws is not None

    @property
    def state(self) -> ConnectionState:
        """Get current connection state"""
        return self._state

    @property
    def server_config(self) -> Optional[ServerConfig]:
        """Get configuration received from server"""
        return self._server_config

    def set_server_url(self, url: str):
        """
        Set the server URL (for use with auto-discovery).

        Args:
            url: WebSocket URL to connect to
        """
        self.server_url = url

    def set_auth_token(self, token: str):
        """
        Set authentication token for WebSocket connection.

        Args:
            token: Auth token from /api/ws/token endpoint
        """
        self._auth_token = token

    def set_verify_tls(self, verify: bool):
        """Set whether to verify TLS certificates."""
        self._verify_tls = verify

    def on_state_change(self, callback: Callable[[str], None]):
        """Register callback for state changes from server"""
        self._on_state_change = callback

    def on_transcription(self, callback: Callable[[str, str], None]):
        """Register callback for transcription results (session_id, text)"""
        self._on_transcription = callback

    def on_action(self, callback: Callable[[str, Dict, bool], None]):
        """Register callback for action results (session_id, intent, success)"""
        self._on_action = callback

    def on_tts_audio(self, callback: Callable[[str, bytes, bool], None]):
        """Register callback for TTS audio (session_id, audio_bytes, is_final)"""
        self._on_tts_audio = callback

    def on_connected(self, callback: Callable[[ServerConfig], None]):
        """Register callback for successful connection"""
        self._on_connected = callback

    def on_disconnected(self, callback: Callable[[], None]):
        """Register callback for disconnection"""
        self._on_disconnected = callback

    def on_error(self, callback: Callable[[str], None]):
        """Register callback for errors"""
        self._on_error = callback

    def on_config_update(self, callback: Callable[["ServerConfig"], None]):
        """Register callback for server config updates (wake word settings)"""
        self._on_config_update = callback

    def on_update_request(self, callback: Callable[[str, str, str, int], None]):
        """Register callback for OTA update requests (version, url, checksum, size)"""
        self._on_update_request = callback

    def set_metrics_callback(self, callback: Callable[[], Dict[str, Any]]):
        """Register callback to get current metrics for heartbeat"""
        self._get_metrics = callback

    async def connect(self) -> bool:
        """
        Connect to server.

        Returns:
            True if connected successfully
        """
        if not WEBSOCKETS_AVAILABLE:
            print("WebSockets not available")
            return False

        if not self.server_url:
            print("No server URL configured - use auto-discovery or set URL")
            return False

        # Cancel any existing tasks before reconnecting
        await self._cancel_background_tasks()

        self._running = True
        self._state = ConnectionState.CONNECTING

        ws_url = self.server_url

        print(f"Connecting to {self.server_url}...")

        try:
            # Build connection kwargs
            connect_kwargs = {
                "ping_interval": 20,
                "ping_timeout": 10,
            }

            # Pass auth token via header instead of URL query parameter
            if self._auth_token:
                connect_kwargs["extra_headers"] = {
                    "Authorization": f"Bearer {self._auth_token}"
                }

            # Enable SSL for wss:// URLs
            if ws_url.startswith("wss://"):
                if self._verify_tls:
                    ssl_context = ssl.create_default_context()
                else:
                    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                    ssl_context.check_hostname = False
                    ssl_context.verify_mode = ssl.CERT_NONE
                connect_kwargs["ssl"] = ssl_context

            self._ws = await websockets.connect(
                ws_url,
                **connect_kwargs,
            )

            # Register with server
            await self._register()

            self._state = ConnectionState.CONNECTED
            print("Connected to server")

            # Start background tasks
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            self._receive_task = asyncio.create_task(self._receive_loop())

            return True

        except Exception as e:
            print(f"Connection failed: {e}")
            self._state = ConnectionState.DISCONNECTED
            if self._on_error:
                self._on_error(str(e))
            return False

    async def _cancel_background_tasks(self):
        """Cancel any running background tasks"""
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
            self._receive_task = None

        # Close existing WebSocket connection
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def _register(self):
        """Send registration message to server"""
        message = {
            "type": "register",
            "satellite_id": self.satellite_id,
            "room": self.room,
            "language": self.language,
            "version": __version__,
            "capabilities": {
                "local_wakeword": True,
                "speaker": True,
                "led_count": 3,
                "button": True,
            },
            "protocol_version": self._protocol_version
        }

        await self._send(message)

        # Wait for ack
        response = await self._ws.recv()
        data = json.loads(response)

        if data.get("type") == "register_ack":
            if data.get("success"):
                config = data.get("config", {})
                server_protocol = data.get("protocol_version", "1.0")
                room_id = data.get("room_id")

                self._server_config = ServerConfig(
                    wake_words=config.get("wake_words", ["hey_jarvis"]),
                    threshold=config.get("threshold", 0.5),
                    cooldown_ms=config.get("cooldown_ms", 2000),
                    protocol_version=server_protocol,
                    room_id=room_id
                )
                print(f"Registered successfully. Server protocol: {server_protocol}")
                print(f"Config: wake_words={self._server_config.wake_words}, threshold={self._server_config.threshold}")

                if self._on_connected:
                    self._on_connected(self._server_config)
            else:
                error_msg = data.get("message", "Registration rejected by server")
                raise Exception(error_msg)
        elif data.get("type") == "error":
            error_code = data.get("code", "UNKNOWN")
            error_msg = data.get("message", "Unknown error")
            raise Exception(f"Registration failed [{error_code}]: {error_msg}")

    async def disconnect(self):
        """Disconnect from server"""
        self._running = False

        # Cancel all background tasks and close connection
        await self._cancel_background_tasks()

        self._state = ConnectionState.DISCONNECTED

        if self._on_disconnected:
            self._on_disconnected()

        print("Disconnected from server")

    async def reconnect(self):
        """Attempt to reconnect with exponential backoff"""
        self._state = ConnectionState.RECONNECTING
        attempts = 0
        max_backoff = 60  # Max seconds between attempts

        while self._running:
            attempts += 1
            backoff = min(self.reconnect_interval * (2 ** (attempts - 1)), max_backoff)

            print(f"Reconnecting in {backoff}s (attempt {attempts})...")
            await asyncio.sleep(backoff)

            if not self._running:
                break

            if await self.connect():
                return True

        return False

    async def _send(self, message: Dict[str, Any]):
        """Send JSON message to server"""
        if self._ws:
            await self._ws.send(json.dumps(message))

    async def _receive_loop(self):
        """Background task receiving messages from server"""
        try:
            while self._running and self._ws:
                try:
                    message = await self._ws.recv()
                    data = json.loads(message)
                    await self._handle_message(data)

                except websockets.ConnectionClosed:
                    print("Connection closed by server")
                    break
                except json.JSONDecodeError as e:
                    print(f"Invalid JSON received: {e}")
                except Exception as e:
                    print(f"Receive error: {e}")
                    break

        finally:
            if self._running:
                # Connection lost - notify caller (satellite handles reconnection)
                self._state = ConnectionState.DISCONNECTED
                if self._on_disconnected:
                    self._on_disconnected()

    async def _handle_message(self, data: Dict[str, Any]):
        """Handle incoming message from server"""
        msg_type = data.get("type", "")

        if msg_type == "state":
            # Server requesting state change
            new_state = data.get("state", "")
            if self._on_state_change:
                self._on_state_change(new_state)

        elif msg_type == "transcription":
            # Transcription result
            session_id = data.get("session_id", "")
            text = data.get("text", "")
            if self._on_transcription:
                self._on_transcription(session_id, text)

        elif msg_type == "action":
            # Action execution result
            session_id = data.get("session_id", "")
            intent = data.get("intent", {})
            success = data.get("success", False)
            if self._on_action:
                self._on_action(session_id, intent, success)

        elif msg_type == "tts_audio":
            # TTS audio response
            session_id = data.get("session_id", "")
            audio_b64 = data.get("audio", "")
            is_final = data.get("is_final", True)

            if audio_b64:
                # Check payload size before decoding to prevent OOM on Pi
                if len(audio_b64) > MAX_AUDIO_PAYLOAD_BYTES:
                    print(f"⚠️ TTS audio payload too large ({len(audio_b64)} bytes), skipping")
                else:
                    audio_bytes = base64.b64decode(audio_b64)
                    if self._on_tts_audio:
                        self._on_tts_audio(session_id, audio_bytes, is_final)

        elif msg_type == "heartbeat_ack":
            pass  # Heartbeat acknowledged

        elif msg_type == "error":
            error_code = data.get("code", "UNKNOWN")
            error_msg = data.get("message", "Unknown error")
            print(f"Server error [{error_code}]: {error_msg}")

            # Handle specific error codes
            if error_code == "RATE_LIMITED":
                print("⚠️ Rate limited by server - slowing down")
                await asyncio.sleep(1)
            elif error_code == "BUFFER_FULL":
                print("⚠️ Audio buffer full - ending current session")
                self._current_session_id = None
            elif error_code in ("UNAUTHORIZED", "AUTH_REQUIRED"):
                print("❌ Authentication failed - may need new token")

            if self._on_error:
                self._on_error(f"{error_code}: {error_msg}")

        elif msg_type == "config_update":
            # Server pushed new wake word configuration
            config_data = data.get("config", {})
            new_config = ServerConfig(
                wake_words=config_data.get("wake_words", self._server_config.wake_words if self._server_config else ["alexa"]),
                threshold=config_data.get("threshold", self._server_config.threshold if self._server_config else 0.5),
                cooldown_ms=config_data.get("cooldown_ms", self._server_config.cooldown_ms if self._server_config else 2000),
                protocol_version=self._server_config.protocol_version if self._server_config else "1.0",
                room_id=self._server_config.room_id if self._server_config else None,
            )
            self._server_config = new_config
            print(f"Config update received: wake_words={new_config.wake_words}, threshold={new_config.threshold}")
            if self._on_config_update:
                self._on_config_update(new_config)

        elif msg_type == "server_shutdown":
            # Server is shutting down gracefully
            shutdown_msg = data.get("message", "Server is shutting down")
            print(f"⚠️ {shutdown_msg}")
            self._state = ConnectionState.DISCONNECTED
            if self._on_disconnected:
                self._on_disconnected()

        elif msg_type == "update_request":
            # OTA update request from server
            target_version = data.get("target_version", "unknown")
            package_url = data.get("package_url", "")
            checksum = data.get("checksum", "")
            size_bytes = data.get("size_bytes", 0)
            print(f"📥 Update request received: v{target_version}")
            if self._on_update_request:
                self._on_update_request(target_version, package_url, checksum, size_bytes)

    async def _heartbeat_loop(self):
        """Background task sending periodic heartbeats with metrics"""
        while self._running and self._ws:
            try:
                await asyncio.sleep(self.heartbeat_interval)

                if self._running and self._ws:
                    uptime = int(time.time() - self._start_time)

                    # Build heartbeat message
                    heartbeat = {
                        "type": "heartbeat",
                        "status": "idle",  # Could be more dynamic
                        "uptime_seconds": uptime,
                        "version": __version__
                    }

                    # Add metrics if callback is set
                    if self._get_metrics:
                        try:
                            metrics = self._get_metrics()
                            if metrics:
                                heartbeat["metrics"] = metrics
                        except Exception as e:
                            print(f"Error getting metrics: {e}")

                    await self._send(heartbeat)

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Heartbeat error: {e}")

    async def send_wakeword_detected(
        self,
        keyword: str,
        confidence: float
    ) -> Optional[str]:
        """
        Notify server of wake word detection.

        Args:
            keyword: Wake word that was detected
            confidence: Detection confidence score

        Returns:
            Session ID if acknowledged, None otherwise
        """
        if not self.is_connected:
            return None

        self._current_session_id = f"{self.satellite_id}-{int(time.time()*1000)}"
        self._audio_sequence = 0

        await self._send({
            "type": "wakeword_detected",
            "satellite_id": self.satellite_id,
            "keyword": keyword,
            "confidence": confidence,
            "session_id": self._current_session_id,
            "timestamp": int(time.time() * 1000)
        })

        return self._current_session_id

    async def send_audio_chunk(
        self,
        session_id: str,
        audio_bytes: bytes
    ):
        """
        Send audio chunk to server.

        Args:
            session_id: Current session ID
            audio_bytes: Raw PCM audio data
        """
        if not self.is_connected:
            return

        self._audio_sequence += 1

        await self._send({
            "type": "audio",
            "session_id": session_id,
            "chunk": base64.b64encode(audio_bytes).decode("utf-8"),
            "sequence": self._audio_sequence
        })

    async def send_audio_end(
        self,
        session_id: str,
        reason: str = "silence"
    ):
        """
        Notify server that audio streaming has ended.

        Args:
            session_id: Current session ID
            reason: Why audio ended (silence, button, timeout)
        """
        if not self.is_connected:
            return

        await self._send({
            "type": "audio_end",
            "session_id": session_id,
            "reason": reason
        })

        self._current_session_id = None

    async def send_config_ack(
        self,
        success: bool,
        active_keywords: List[str],
        failed_keywords: Optional[List[str]] = None,
        error: Optional[str] = None
    ):
        """
        Send config acknowledgment to server after applying config_update.

        Args:
            success: Whether the config was applied successfully
            active_keywords: List of keywords currently active on this satellite
            failed_keywords: List of keywords that failed to load
            error: Error message if something went wrong
        """
        if not self.is_connected:
            return

        await self._send({
            "type": "config_ack",
            "success": success,
            "active_keywords": active_keywords,
            "failed_keywords": failed_keywords or [],
            "error": error
        })

        status_emoji = "✅" if success else "⚠️"
        print(f"{status_emoji} Config ack sent: active={active_keywords}, failed={failed_keywords or []}")

    async def send_update_progress(
        self,
        stage: str,
        progress: int,
        message: str = ""
    ):
        """
        Send update progress to server.

        Args:
            stage: Current update stage (downloading, verifying, etc.)
            progress: Progress percentage (0-100)
            message: Optional status message
        """
        if not self.is_connected:
            return

        await self._send({
            "type": "update_progress",
            "stage": stage,
            "progress": progress,
            "message": message
        })

    async def send_update_complete(
        self,
        success: bool,
        old_version: str,
        new_version: str,
        error: Optional[str] = None
    ):
        """
        Send update completion status to server.

        Args:
            success: Whether the update succeeded
            old_version: Previous version
            new_version: New version (if successful)
            error: Error message (if failed)
        """
        if not self.is_connected:
            return

        await self._send({
            "type": "update_complete",
            "success": success,
            "old_version": old_version,
            "new_version": new_version,
            "error": error
        })

    async def send_update_failed(
        self,
        stage: str,
        error: str,
        rolled_back: bool = False
    ):
        """
        Send update failure notification to server.

        Args:
            stage: Stage where failure occurred
            error: Error message
            rolled_back: Whether rollback was performed
        """
        if not self.is_connected:
            return

        await self._send({
            "type": "update_failed",
            "stage": stage,
            "error": error,
            "rolled_back": rolled_back
        })
