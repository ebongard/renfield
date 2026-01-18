"""
Main Satellite Class for Renfield

Orchestrates all satellite components:
- Wake word detection
- Audio capture and playback
- LED feedback
- Button handling
- WebSocket communication
"""

import asyncio
import time
from enum import Enum
from typing import Optional

from .config import Config
from .audio.capture import AudioCapture
from .audio.playback import AudioPlayback
from .wakeword.detector import WakeWordDetector, Detection
from .hardware.led import LEDController, LEDPattern
from .hardware.button import ButtonHandler
from .network.websocket_client import WebSocketClient, ServerConfig
from .network.discovery import ServiceDiscovery


class SatelliteState(str, Enum):
    """Satellite operational states"""
    BOOT = "boot"
    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"
    SPEAKING = "speaking"
    ERROR = "error"


class Satellite:
    """
    Main satellite controller.

    Manages the lifecycle and coordination of all components.
    Implements the state machine for voice interaction.
    """

    def __init__(self, config: Config):
        """
        Initialize satellite with configuration.

        Args:
            config: Configuration object
        """
        self.config = config
        self._state = SatelliteState.BOOT
        self._running = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None  # Main event loop

        # Current session
        self._session_id: Optional[str] = None
        self._audio_buffer: list = []
        self._silence_start: Optional[float] = None
        self._processing_start: Optional[float] = None  # Track when processing started
        self._processing_timeout: float = 30.0  # Max time to wait for server response
        self._reconnecting: bool = False  # Prevent duplicate reconnection attempts
        self._wakeword_pending: bool = False  # Prevent duplicate wakeword processing

        # Initialize components
        self._init_components()

    def _init_components(self):
        """Initialize all hardware and software components"""
        print("Initializing satellite components...")

        # Audio capture
        self.audio_capture = AudioCapture(
            sample_rate=self.config.audio.sample_rate,
            chunk_size=self.config.audio.chunk_size,
            channels=self.config.audio.channels,
            device=self.config.audio.device,
        )

        # Audio playback
        self.audio_playback = AudioPlayback(
            device=self.config.audio.playback_device,
        )

        # Wake word detector
        self.wakeword = WakeWordDetector(
            models_path=self.config.wakeword.models_path,
            keywords=[self.config.wakeword.model],
            threshold=self.config.wakeword.threshold,
            stop_words=self.config.wakeword.stop_words,
            refractory_seconds=self.config.wakeword.refractory_seconds,
        )

        # LED controller
        self.leds = LEDController(
            num_leds=self.config.led.num_leds,
            brightness=self.config.led.brightness,
        )

        # Button handler
        self.button = ButtonHandler(
            gpio_pin=self.config.button.gpio_pin,
            debounce_ms=self.config.button.debounce_ms,
        )

        # WebSocket client (URL may be set later via auto-discovery)
        self.ws_client = WebSocketClient(
            satellite_id=self.config.satellite.id,
            room=self.config.satellite.room,
            server_url=self.config.server.url,  # May be None if using auto-discovery
            reconnect_interval=self.config.server.reconnect_interval,
            heartbeat_interval=self.config.server.heartbeat_interval,
        )

        # Service discovery for auto-finding server
        self.discovery = ServiceDiscovery()

        # Wire up callbacks
        self._setup_callbacks()

    def _setup_callbacks(self):
        """Setup event callbacks between components"""
        # WebSocket callbacks
        self.ws_client.on_state_change(self._on_server_state_change)
        self.ws_client.on_transcription(self._on_transcription)
        self.ws_client.on_action(self._on_action_result)
        self.ws_client.on_tts_audio(self._on_tts_audio)
        self.ws_client.on_connected(self._on_connected)
        self.ws_client.on_disconnected(self._on_disconnected)
        self.ws_client.on_error(self._on_error)

        # Button callbacks
        self.button.on_press(self._on_button_press)
        self.button.on_long_press(self._on_button_long_press)

    async def start(self):
        """Start the satellite"""
        print(f"Starting Renfield Satellite: {self.config.satellite.id}")
        print(f"Room: {self.config.satellite.room}")

        self._running = True
        self._set_state(SatelliteState.BOOT)

        # Initialize hardware
        if not self.leds.open():
            print("Warning: LED control not available")

        if not self.button.setup():
            print("Warning: Button control not available")

        # Show boot animation
        self.leds.set_pattern(LEDPattern.BOOT)
        await asyncio.sleep(2)

        # Load wake word model
        print("Loading wake word model...")
        if not self.wakeword.load():
            print("Warning: Wake word detection not available")
            self._set_state(SatelliteState.ERROR)
            self.leds.set_pattern(LEDPattern.ERROR)
            await asyncio.sleep(3)

        # Discover server if needed
        server_url = self.config.server.url
        if not server_url and self.config.server.auto_discover:
            print("Auto-discovering Renfield server...")
            server_url = await self._discover_server()

        if server_url:
            self.ws_client.set_server_url(server_url)
            print(f"Server: {server_url}")
        else:
            print("No server URL configured and auto-discovery disabled/failed")
            self._set_state(SatelliteState.ERROR)
            self.leds.set_pattern(LEDPattern.ERROR)
            await asyncio.sleep(3)

        # Connect to server
        print("Connecting to server...")
        if not await self.ws_client.connect():
            print("Failed to connect to server - will retry")
            self._set_state(SatelliteState.ERROR)
            self.leds.set_pattern(LEDPattern.ERROR)
            await asyncio.sleep(2)
            asyncio.create_task(self._reconnect_with_discovery())

        # Go to idle state
        self._set_state(SatelliteState.IDLE)

        # Start main loop
        await self._main_loop()

    async def stop(self):
        """Stop the satellite"""
        print("Stopping satellite...")
        self._running = False

        # Stop audio
        self.audio_capture.stop()
        self.audio_playback.stop()

        # Disconnect from server
        await self.ws_client.disconnect()

        # Stop discovery if running
        await self.discovery.stop_continuous_discovery()

        # Turn off LEDs
        self.leds.set_pattern(LEDPattern.OFF)
        self.leds.close()

        # Cleanup GPIO
        self.button.cleanup()

        print("Satellite stopped")

    async def _discover_server(self) -> Optional[str]:
        """
        Discover Renfield server using zeroconf.

        Returns:
            WebSocket URL if server found, None otherwise
        """
        if not self.discovery.available:
            print("Zeroconf not available for auto-discovery")
            return None

        server = await self.discovery.find_server(
            timeout=self.config.server.discovery_timeout
        )

        if server:
            print(f"Discovered server: {server.name} at {server.ws_url}")
            return server.ws_url
        else:
            print("No Renfield server found on network")
            return None

    async def _reconnect_with_discovery_wrapper(self):
        """Wrapper for reconnection that resets the reconnecting flag"""
        try:
            await self._reconnect_with_discovery()
        finally:
            self._reconnecting = False

    async def _reconnect_with_discovery(self):
        """
        Reconnection loop that re-discovers server if needed.

        This is used when the initial connection fails or when
        disconnected from the server.
        """
        attempts = 0
        max_backoff = 60

        while self._running:
            attempts += 1
            backoff = min(self.config.server.reconnect_interval * (2 ** (attempts - 1)), max_backoff)

            print(f"Reconnecting in {backoff}s (attempt {attempts})...")
            await asyncio.sleep(backoff)

            if not self._running:
                break

            # Re-discover server if auto-discovery is enabled and no URL set
            if self.config.server.auto_discover and not self.config.server.url:
                print("Re-discovering server...")
                server_url = await self._discover_server()
                if server_url:
                    self.ws_client.set_server_url(server_url)

            # Try to connect
            if await self.ws_client.connect():
                return True

        return False

    async def _main_loop(self):
        """Main event loop"""
        print("Entering main loop...")

        # Store event loop for thread-safe callbacks
        self._loop = asyncio.get_running_loop()

        # Start audio capture with callback
        self.audio_capture.start(self._on_audio_chunk)

        try:
            while self._running:
                # Check for processing timeout
                if self._state == SatelliteState.PROCESSING and self._processing_start:
                    elapsed = time.time() - self._processing_start
                    if elapsed > self._processing_timeout:
                        print(f"Processing timeout ({elapsed:.1f}s) - returning to idle")
                        await self._reset_session("processing_timeout")

                # Main loop tick
                await asyncio.sleep(0.1)

        except asyncio.CancelledError:
            pass
        finally:
            self.audio_capture.stop()

    def _set_state(self, state: SatelliteState):
        """Update satellite state and LED pattern"""
        if state == self._state:
            return

        print(f"State: {self._state.value} -> {state.value}")
        self._state = state

        # Update LED pattern
        pattern_map = {
            SatelliteState.BOOT: LEDPattern.BOOT,
            SatelliteState.IDLE: LEDPattern.IDLE,
            SatelliteState.LISTENING: LEDPattern.LISTENING,
            SatelliteState.PROCESSING: LEDPattern.PROCESSING,
            SatelliteState.SPEAKING: LEDPattern.SPEAKING,
            SatelliteState.ERROR: LEDPattern.ERROR,
        }
        self.leds.set_pattern(pattern_map.get(state, LEDPattern.OFF))

    def _schedule_async(self, coro):
        """Schedule a coroutine from a non-async context (thread-safe)"""
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _on_audio_chunk(self, audio_bytes: bytes):
        """Handle incoming audio chunk from microphone (called from audio thread)"""
        # Process for wake word in IDLE state
        if self._state == SatelliteState.IDLE and not self._wakeword_pending:
            detection = self.wakeword.process_audio(audio_bytes)
            if detection and not detection.is_stop_word:
                # Set flag immediately to prevent duplicate detection
                self._wakeword_pending = True
                self._schedule_async(self._on_wakeword_detected(detection.keyword, detection.confidence))
            return

        # Check for stop words during LISTENING or PROCESSING
        if self._state in (SatelliteState.LISTENING, SatelliteState.PROCESSING):
            detection = self.wakeword.process_audio(audio_bytes)
            if detection and detection.is_stop_word:
                print(f"Stop word detected: {detection.keyword}")
                self._schedule_async(self._cancel_interaction())
                return

        # Buffer and stream audio in LISTENING state
        if self._state == SatelliteState.LISTENING:
            self._audio_buffer.append(audio_bytes)

            # Stream to server
            if self._session_id:
                self._schedule_async(
                    self.ws_client.send_audio_chunk(self._session_id, audio_bytes)
                )

            # Check for silence (VAD)
            rms = self.audio_capture.get_rms(audio_bytes)
            if rms < self.config.vad.silence_threshold:
                if self._silence_start is None:
                    self._silence_start = time.time()
                elif time.time() - self._silence_start > self.config.vad.silence_duration_ms / 1000:
                    # Silence detected - end recording
                    self._schedule_async(self._end_listening("silence"))
            else:
                self._silence_start = None

            # Check max recording length
            if len(self._audio_buffer) * self.config.audio.chunk_size / self.config.audio.sample_rate > self.config.vad.max_recording_seconds:
                self._schedule_async(self._end_listening("timeout"))

    async def _on_wakeword_detected(self, keyword: str, confidence: float):
        """Handle wake word detection"""
        print(f"Wake word detected: {keyword} ({confidence:.2f})")

        if self._state != SatelliteState.IDLE:
            print("Ignoring - not in idle state")
            self._wakeword_pending = False
            return

        if not self.ws_client.is_connected:
            print("Ignoring - not connected to server")
            self._wakeword_pending = False
            self.leds.set_pattern(LEDPattern.ERROR)
            await asyncio.sleep(1)
            self.leds.set_pattern(LEDPattern.IDLE)
            return

        # Start listening - flag will be cleared in _reset_session when done
        self._set_state(SatelliteState.LISTENING)
        self._audio_buffer.clear()
        self._silence_start = None

        # Notify server
        self._session_id = await self.ws_client.send_wakeword_detected(keyword, confidence)
        print(f"Session started: {self._session_id}")

        # Reset wake word detector
        self.wakeword.reset()

    async def _end_listening(self, reason: str):
        """End the listening phase"""
        if self._state != SatelliteState.LISTENING:
            return

        print(f"Ending listening: {reason}")
        self._set_state(SatelliteState.PROCESSING)
        self._processing_start = time.time()  # Start timeout countdown

        # Notify server
        if self._session_id:
            await self.ws_client.send_audio_end(self._session_id, reason)

    async def _reset_session(self, reason: str = "reset"):
        """Reset session state and return to idle"""
        # Skip if already idle (prevents duplicate resets)
        if self._state == SatelliteState.IDLE and self._session_id is None:
            return

        print(f"Resetting session: {reason}")

        # Clear session data
        self._session_id = None
        self._audio_buffer.clear()
        self._silence_start = None
        self._processing_start = None
        self._wakeword_pending = False  # Allow new wake word detection

        # Reset wake word detector
        self.wakeword.reset()

        # Return to idle
        self._set_state(SatelliteState.IDLE)

    async def _cancel_interaction(self):
        """Cancel the current interaction (triggered by stop word)"""
        print("Canceling interaction...")

        # Notify server that the session was canceled
        if self._session_id:
            try:
                await self.ws_client.send_audio_end(self._session_id, "canceled")
            except Exception as e:
                print(f"Failed to notify server of cancellation: {e}")

        # Reset session
        await self._reset_session("canceled")

    def _on_button_press(self):
        """Handle button press"""
        print("Button pressed")

        if self._state == SatelliteState.LISTENING:
            # End listening early
            self._schedule_async(self._end_listening("button"))
        elif self._state == SatelliteState.IDLE:
            # Manual trigger (like saying wake word)
            self._schedule_async(self._manual_trigger())

    def _on_button_long_press(self):
        """Handle button long press"""
        print("Button long press - stopping")
        self._running = False

    async def _manual_trigger(self):
        """Manually start listening (via button)"""
        await self._on_wakeword_detected("button", 1.0)

    # WebSocket callbacks
    def _on_server_state_change(self, state: str):
        """Handle state change command from server"""
        print(f"Server state command: {state}")

        state_map = {
            "idle": SatelliteState.IDLE,
            "listening": SatelliteState.LISTENING,
            "processing": SatelliteState.PROCESSING,
            "speaking": SatelliteState.SPEAKING,
        }

        if state in state_map:
            new_state = state_map[state]
            self._set_state(new_state)

            # If server tells us to go idle, do a full session reset
            if new_state == SatelliteState.IDLE:
                self._schedule_async(self._reset_session("server_idle"))

    def _on_transcription(self, session_id: str, text: str):
        """Handle transcription result"""
        print(f"Transcription: {text}")
        # Could display on screen if available

    def _on_action_result(self, session_id: str, intent: dict, success: bool):
        """Handle action execution result"""
        print(f"Action: {intent.get('intent')} = {success}")

        if not success:
            # Flash error
            self.leds.set_pattern(LEDPattern.ERROR)

    def _on_tts_audio(self, session_id: str, audio_bytes: bytes, is_final: bool):
        """Handle TTS audio from server"""
        print(f"Playing TTS audio ({len(audio_bytes)} bytes)")
        self._set_state(SatelliteState.SPEAKING)
        self._processing_start = None  # Clear processing timeout

        # Play audio
        self.audio_playback.play_wav(audio_bytes)

        if is_final:
            # Return to idle after playback - use async reset
            self._schedule_async(self._reset_session("tts_complete"))

    def _on_connected(self, config: ServerConfig):
        """Handle successful connection"""
        print(f"Connected to server. Wake words: {config.wake_words}")

        # Update wake word config if server provides different settings
        if config.wake_words:
            self.wakeword.set_keywords(config.wake_words)
        if config.threshold:
            self.wakeword.set_threshold(config.threshold)

        self._set_state(SatelliteState.IDLE)

    def _on_disconnected(self):
        """Handle disconnection"""
        print("Disconnected from server")
        self._set_state(SatelliteState.ERROR)

        # Clear session state
        self._session_id = None
        self._processing_start = None
        self._silence_start = None

        # Start reconnection (only if not already reconnecting)
        if not self._reconnecting:
            self._reconnecting = True
            self._schedule_async(self._reconnect_with_discovery_wrapper())

    def _on_error(self, message: str):
        """Handle error from server"""
        print(f"Server error: {message}")

        # Show error LED briefly
        self.leds.set_pattern(LEDPattern.ERROR)

        # If we have an active session, reset it and return to idle
        if self._session_id or self._state in (SatelliteState.LISTENING, SatelliteState.PROCESSING, SatelliteState.SPEAKING):
            print("Resetting due to server error")
            self._schedule_async(self._reset_session("server_error"))
