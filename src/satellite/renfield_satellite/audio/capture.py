"""
Audio Capture Module for Renfield Satellite

Handles microphone input using PyAudio (ALSA) or soundcard library.
PyAudio is preferred on Raspberry Pi as it respects ALSA configuration.

Inspired by OHF-Voice/linux-voice-assistant approach.
"""

import asyncio
import threading
from typing import Callable, List, Optional
import numpy as np

# Try PyAudio first (preferred for Raspberry Pi / ALSA)
PYAUDIO_AVAILABLE = False
SOUNDCARD_AVAILABLE = False

try:
    import pyaudio
    PYAUDIO_AVAILABLE = True
except ImportError:
    pyaudio = None

# Only try soundcard if PyAudio is not available
# soundcard can crash on Raspberry Pi when trying to connect to PulseAudio
if not PYAUDIO_AVAILABLE:
    try:
        import soundcard as sc
        SOUNDCARD_AVAILABLE = True
    except Exception as e:
        # Catch any exception, not just ImportError
        # soundcard may crash with IndexError on Raspberry Pi
        sc = None
        print(f"soundcard not available: {e}")

if not PYAUDIO_AVAILABLE and not SOUNDCARD_AVAILABLE:
    print("Warning: Neither pyaudio nor soundcard installed. Audio capture disabled.")
    print("Install with: pip install pyaudio  (recommended for Raspberry Pi)")
    print("         or: pip install soundcard")


class AudioCapture:
    """
    Captures audio from microphone in real-time.

    Uses PyAudio (ALSA) on Raspberry Pi for proper hardware support,
    or soundcard library as fallback.
    Audio is provided as 16-bit PCM, 16kHz, mono.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        chunk_size: int = 1024,
        channels: int = 1,
        device: Optional[str] = None,
    ):
        """
        Initialize audio capture.

        Args:
            sample_rate: Sample rate in Hz (default 16000)
            chunk_size: Samples per chunk (default 1024)
            channels: Number of channels (default 1 = mono)
            device: Device name/id or None for default (ALSA device for PyAudio)
        """
        self.sample_rate = sample_rate
        self.chunk_size = chunk_size
        self.channels = channels
        self.device_name = device

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._callback: Optional[Callable[[bytes], None]] = None

        # PyAudio specific
        self._pyaudio = None
        self._stream = None

        # Soundcard specific
        self._mic = None
        self._recorder = None

        # Determine which backend to use
        self._use_pyaudio = PYAUDIO_AVAILABLE
        if self._use_pyaudio:
            print("Audio backend: PyAudio (ALSA)")
        elif SOUNDCARD_AVAILABLE:
            print("Audio backend: soundcard (PipeWire/PulseAudio)")
        else:
            print("Audio backend: None available!")

    @staticmethod
    def list_devices() -> List[dict]:
        """
        List available microphone devices.

        Returns:
            List of device info dicts with 'name' and 'index'
        """
        devices = []

        if PYAUDIO_AVAILABLE:
            pa = pyaudio.PyAudio()
            try:
                for i in range(pa.get_device_count()):
                    info = pa.get_device_info_by_index(i)
                    if info['maxInputChannels'] > 0:
                        devices.append({
                            "name": info['name'],
                            "index": i,
                            "channels": info['maxInputChannels'],
                            "sample_rate": int(info['defaultSampleRate']),
                        })
            finally:
                pa.terminate()
        elif SOUNDCARD_AVAILABLE:
            for mic in sc.all_microphones(include_loopback=False):
                devices.append({
                    "name": mic.name,
                    "id": mic.id,
                    "channels": mic.channels,
                })

        return devices

    def _find_pyaudio_device(self) -> Optional[int]:
        """Find PyAudio device index"""
        if not self._pyaudio:
            return None

        # If device specified, try to find it
        if self.device_name:
            for i in range(self._pyaudio.get_device_count()):
                info = self._pyaudio.get_device_info_by_index(i)
                if info['maxInputChannels'] > 0:
                    if self.device_name.lower() in info['name'].lower():
                        print(f"Found microphone: {info['name']} (index {i})")
                        return i

        # Use default input device
        try:
            default_info = self._pyaudio.get_default_input_device_info()
            print(f"Using default microphone: {default_info['name']} (index {default_info['index']})")
            return int(default_info['index'])
        except Exception as e:
            print(f"No default input device: {e}")
            return None

    def _find_soundcard_device(self):
        """Find soundcard microphone"""
        if not SOUNDCARD_AVAILABLE:
            return None

        if self.device_name:
            try:
                return sc.get_microphone(self.device_name, include_loopback=False)
            except Exception:
                for mic in sc.all_microphones(include_loopback=False):
                    if self.device_name.lower() in mic.name.lower():
                        print(f"Found microphone: {mic.name}")
                        return mic

        try:
            mic = sc.default_microphone()
            print(f"Using default microphone: {mic.name}")
            return mic
        except Exception as e:
            print(f"No default microphone found: {e}")
            return None

    def start(self, callback: Callable[[bytes], None]):
        """
        Start audio capture.

        Args:
            callback: Function called with audio chunks (bytes)
        """
        if self._running:
            print("Audio capture already running")
            return

        self._callback = callback
        self._running = True

        if self._use_pyaudio:
            self._start_pyaudio()
        elif SOUNDCARD_AVAILABLE:
            self._start_soundcard()
        else:
            print("No audio backend available")
            self._running = False

    def _start_pyaudio(self):
        """Start PyAudio capture"""
        self._pyaudio = pyaudio.PyAudio()
        device_index = self._find_pyaudio_device()

        if device_index is None:
            print("No microphone device found")
            print("Available devices:")
            for dev in self.list_devices():
                print(f"  [{dev.get('index', '?')}] {dev['name']}")
            self._pyaudio.terminate()
            self._pyaudio = None
            self._running = False
            return

        try:
            self._stream = self._pyaudio.open(
                format=pyaudio.paInt16,
                channels=self.channels,
                rate=self.sample_rate,
                input=True,
                input_device_index=device_index,
                frames_per_buffer=self.chunk_size,
            )

            # Get device name for logging
            device_info = self._pyaudio.get_device_info_by_index(device_index)
            print(f"Audio capture started: {device_info['name']}")

            # Start capture thread
            self._thread = threading.Thread(target=self._pyaudio_capture_loop, daemon=True)
            self._thread.start()

        except Exception as e:
            print(f"Failed to open audio stream: {e}")
            if self._pyaudio:
                self._pyaudio.terminate()
                self._pyaudio = None
            self._running = False

    def _pyaudio_capture_loop(self):
        """PyAudio capture thread"""
        try:
            while self._running and self._stream:
                try:
                    # Read audio data (already 16-bit PCM)
                    audio_bytes = self._stream.read(self.chunk_size, exception_on_overflow=False)

                    # Deliver to callback
                    if self._callback and audio_bytes:
                        self._callback(audio_bytes)

                except Exception as e:
                    if self._running:
                        print(f"Audio capture error: {e}")
                    break
        finally:
            pass

    def _start_soundcard(self):
        """Start soundcard capture"""
        self._mic = self._find_soundcard_device()

        if not self._mic:
            print("No microphone device found")
            print("Available devices:")
            for dev in self.list_devices():
                print(f"  - {dev['name']} (id: {dev.get('id', '?')})")
            self._running = False
            return

        # Start capture thread
        self._thread = threading.Thread(target=self._soundcard_capture_loop, daemon=True)
        self._thread.start()
        print(f"Audio capture started: {self._mic.name}")

    def _soundcard_capture_loop(self):
        """Soundcard capture thread"""
        try:
            with self._mic.recorder(
                samplerate=self.sample_rate,
                channels=self.channels,
                blocksize=self.chunk_size
            ) as recorder:
                self._recorder = recorder

                while self._running:
                    try:
                        # Record audio block (returns float32 array)
                        audio_float = recorder.record(numframes=self.chunk_size)

                        # Convert to mono if needed
                        if len(audio_float.shape) > 1 and audio_float.shape[1] > 1:
                            audio_float = audio_float.mean(axis=1)
                        else:
                            audio_float = audio_float.flatten()

                        # Convert to 16-bit PCM bytes
                        audio_int16 = (audio_float * 32767).astype(np.int16)
                        audio_bytes = audio_int16.tobytes()

                        # Deliver to callback
                        if self._callback and audio_bytes:
                            self._callback(audio_bytes)

                    except Exception as e:
                        if self._running:
                            print(f"Audio capture error: {e}")
                        break

        except Exception as e:
            print(f"Failed to open audio stream: {e}")
        finally:
            self._recorder = None

    def stop(self):
        """Stop audio capture"""
        self._running = False

        # Stop PyAudio
        if self._stream:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except:
                pass
            self._stream = None

        if self._pyaudio:
            try:
                self._pyaudio.terminate()
            except:
                pass
            self._pyaudio = None

        # Wait for thread
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

        self._mic = None
        print("Audio capture stopped")

    def get_rms(self, audio_bytes: bytes) -> float:
        """
        Calculate RMS (Root Mean Square) of audio for VAD.

        Args:
            audio_bytes: Raw PCM audio bytes

        Returns:
            RMS value (0-32768 for 16-bit audio)
        """
        try:
            audio = np.frombuffer(audio_bytes, dtype=np.int16)
            return float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))
        except:
            return 0.0

    @property
    def is_running(self) -> bool:
        """Check if capture is running"""
        return self._running


class AudioCaptureAsync:
    """
    Async wrapper for AudioCapture.

    Provides async iteration over audio chunks.
    """

    def __init__(self, capture: AudioCapture):
        self.capture = capture
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._running = False

    async def start(self):
        """Start async audio capture"""
        self._running = True

        def on_audio(data: bytes):
            if self._running:
                try:
                    self._queue.put_nowait(data)
                except asyncio.QueueFull:
                    pass  # Drop oldest if queue full

        self.capture.start(on_audio)

    async def stop(self):
        """Stop async audio capture"""
        self._running = False
        self.capture.stop()

    async def get_chunk(self, timeout: float = 1.0) -> Optional[bytes]:
        """Get next audio chunk"""
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        if not self._running:
            raise StopAsyncIteration

        chunk = await self.get_chunk()
        if chunk is None:
            raise StopAsyncIteration
        return chunk
