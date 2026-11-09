"""
Audio Capture Module for Renfield Satellite

Handles microphone input using PyAudio (ALSA), arecord subprocess,
or soundcard library. PyAudio is preferred on Raspberry Pi for
general ALSA hardware support.

Supports optional beamforming with ReSpeaker 2-Mics Pi HAT for
improved noise rejection and speech enhancement.

ReSpeaker 4-Mic Array (AC108): MUST use arecord subprocess backend
(use_arecord=True). PyAudio and onnxruntime (used by openwakeword)
in the same process triggers a kernel crash when pa.open() is called.
The AC108 hardware only supports 4ch/S32_LE natively — arecord handles
the I2S driver in a separate process, and the capture loop converts
4ch/S32_LE → mono S16_LE in Python.
"""

import asyncio
import queue
import shutil
import subprocess
import threading
from typing import Callable, List, Optional, TYPE_CHECKING
import numpy as np

if TYPE_CHECKING:
    from .beamformer import BeamformerDAS

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

    Supports optional beamforming with ReSpeaker 2-Mics Pi HAT:
    - Set channels=2 and beamforming=True
    - Captures stereo and applies Delay-and-Sum beamforming
    - Output is still mono (enhanced)
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        chunk_size: int = 1024,
        channels: int = 1,
        device: Optional[str] = None,
        use_arecord: bool = False,
        beamforming: bool = False,
        mic_spacing: float = 0.058,  # ReSpeaker 2-Mics: 58mm
        steering_angle: float = 0.0,  # 0 = front-facing
        combine: Optional[str] = None,
        select_channel: Optional[int] = None,
    ):
        """
        Initialize audio capture.

        Args:
            sample_rate: Sample rate in Hz (default 16000)
            chunk_size: Samples per chunk (default 1024)
            channels: Number of channels (hardware native channel count)
            device: Device name/id or None for default (ALSA device name)
            use_arecord: Use arecord subprocess for capture. Required for
                AC108 4-mic HAT — PyAudio + onnxruntime in the same process
                crashes the kernel on Pi Zero 2 W.
            beamforming: Enable beamforming (requires channels=2)
            mic_spacing: Microphone spacing in meters (default 58mm for ReSpeaker)
            steering_angle: Target direction in degrees (0=front)
            combine: Stereo→mono reduction: "beamform" | "select" |
                "passthrough". None = auto-derive (beamforming→beamform,
                channels>1→select, else passthrough) for back-compat.
            select_channel: When combine="select", keep this channel and drop
                the rest. None = legacy default (AC108 4-mic→ch1, else ch0).
                The XVF3800 selects ch0 (its processed beam) rather than let
                ALSA downmix beam+residual to silence.
                See docs/design/satellite-audio-combine-pipeline.md.
        """
        self.sample_rate = sample_rate
        self.chunk_size = chunk_size
        self.device_name = device
        self.use_arecord = use_arecord
        self.beamforming_enabled = beamforming

        # If beamforming enabled, force stereo capture
        if beamforming and channels == 1:
            channels = 2
            print("Beamforming enabled: switching to stereo capture")

        self.channels = channels

        # Stereo→mono combine (docs/design/satellite-audio-combine-pipeline.md).
        # Resolve the effective mode + channel from the legacy signals when not
        # given explicitly, so un-reprovisioned sats stay byte-identical:
        #   combine: beamforming→"beamform", channels>1→"select", else "passthrough"
        #   select_channel: AC108 4-mic→ch1 (ch0 is its silent reference), else ch0
        if combine is not None:
            self.combine = combine
        elif beamforming:
            self.combine = "beamform"
        elif channels > 1:
            self.combine = "select"
        else:
            self.combine = "passthrough"
        if self.combine not in ("beamform", "select", "passthrough"):
            print(f"Unknown audio combine '{self.combine}', falling back to select")
            self.combine = "select" if channels > 1 else "passthrough"
        # passthrough only makes sense for a single channel — on a multi-channel
        # device it would emit the raw interleaved frame and feed the wakeword
        # 2x/4x garbage. Force select so a misconfig can't silently deafen it.
        if self.combine == "passthrough" and channels > 1:
            print(f"combine=passthrough invalid for {channels}ch — using select")
            self.combine = "select"
        self.select_channel = (
            select_channel if select_channel is not None
            else (1 if channels >= 4 else 0)
        )
        if not 0 <= self.select_channel < max(1, channels):
            print(
                f"select_channel {self.select_channel} out of range for "
                f"{channels}ch — clamping to 0"
            )
            self.select_channel = 0

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._consumer_thread: Optional[threading.Thread] = None
        self._callback: Optional[Callable[[bytes], None]] = None
        # Queue decouples reads from callback processing to prevent
        # buffer overruns that crash the AC108 driver on Pi Zero 2 W.
        self._audio_queue: queue.Queue = queue.Queue(maxsize=200)

        # PyAudio specific
        self._pyaudio = None
        self._stream = None

        # arecord subprocess
        self._arecord_proc: Optional[subprocess.Popen] = None

        # Soundcard specific
        self._mic = None
        self._recorder = None

        # Optional per-chunk transform applied in the CONSUMER thread (never the
        # capture read loop). The PyAudio backend uses this to run stereo->mono
        # beamforming off the read thread, so heavy numpy work can never delay
        # stream.read() (an I2S buffer overflow can crash the kernel on Pi Zero 2 W).
        self._consumer_transform: Optional[Callable[[bytes], bytes]] = None

        # Beamformer (lazy initialization)
        self._beamformer: Optional["BeamformerDAS"] = None
        self._mic_spacing = mic_spacing
        self._steering_angle = steering_angle

        if beamforming:
            self._init_beamformer()

        # Determine which backend to use
        if use_arecord:
            if shutil.which("arecord"):
                print(f"Audio backend: arecord subprocess — {self.channels}ch S32_LE (AC108)")
            else:
                print("Audio backend: arecord requested but not found!")
        elif PYAUDIO_AVAILABLE:
            print(f"Audio backend: PyAudio (ALSA) — {self.channels}ch S16_LE")
        elif SOUNDCARD_AVAILABLE:
            print("Audio backend: soundcard (PipeWire/PulseAudio)")
        else:
            print("Audio backend: None available!")

    def _init_beamformer(self):
        """Initialize beamformer for stereo processing."""
        try:
            from .beamformer import BeamformerDAS
            self._beamformer = BeamformerDAS(
                mic_spacing=self._mic_spacing,
                sample_rate=self.sample_rate,
                steering_angle=self._steering_angle,
            )
            print(f"Beamformer: DAS (spacing={self._mic_spacing*1000:.0f}mm, angle={self._steering_angle}°)")
        except ImportError as e:
            print(f"Beamformer not available: {e}")
            self.beamforming_enabled = False

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

        # If device specified, try to find it. Prefer an EXACT name match before
        # falling back to substring: the configured device "default" must resolve
        # to the ALSA "default" PCM (our /etc/asound.conf → XVF3800 dsnoop), NOT
        # "sysdefault" (raw card, opened exclusively, silent on this hardware) —
        # a plain substring match picks "sysdefault" because it *contains*
        # "default", which captured pure silence and broke wake-word detection.
        if self.device_name:
            name = self.device_name.lower()
            count = self._pyaudio.get_device_count()
            for i in range(count):
                info = self._pyaudio.get_device_info_by_index(i)
                if info['maxInputChannels'] > 0 and info['name'].lower() == name:
                    print(f"Found microphone (exact): {info['name']} (index {i})")
                    return i
            for i in range(count):
                info = self._pyaudio.get_device_info_by_index(i)
                if info['maxInputChannels'] > 0 and name in info['name'].lower():
                    print(f"Found microphone (substring): {info['name']} (index {i})")
                    return i
            # A configured device that is missing is NOT replaced by PortAudio's
            # default input. With the XVF3800 unplugged, "default" vanishes and
            # the only input left is the "pulse" null PCM (/etc/asound.conf):
            # its reads return at once, so the capture threads spun at full CPU
            # on fabricated audio (Fitnessraum, 2026-11-09).
            print(f"Configured microphone '{self.device_name}' not found")
            return None

        # No device configured: use the default input device
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

        if self.use_arecord:
            self._start_arecord()
        elif PYAUDIO_AVAILABLE:
            self._start_pyaudio()
        elif SOUNDCARD_AVAILABLE:
            self._start_soundcard()
        else:
            print("No audio backend available")
            self._running = False

    def _start_arecord(self):
        """Start arecord subprocess capture.

        Required for AC108 4-mic HAT: PyAudio and onnxruntime (openwakeword)
        in the same process causes a kernel crash when pa.open() accesses the
        I2S driver. arecord runs in a separate process, isolating the driver.

        The AC108 hardware only supports 4ch/S32_LE natively. This method
        spawns arecord with those parameters and converts to mono S16_LE
        in the capture loop.
        """
        if not shutil.which("arecord"):
            print("arecord not found — install alsa-utils")
            self._running = False
            return

        device = self.device_name or "hw:0,0"
        cmd = [
            "arecord",
            "-D", device,
            "-f", "S32_LE",
            "-c", str(self.channels),
            "-r", str(self.sample_rate),
            "-t", "raw",
        ]

        try:
            self._arecord_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
            print(f"Audio capture started: arecord {device} (S32_LE/{self.channels}ch)")

            # Start capture thread (reads from arecord pipe)
            self._thread = threading.Thread(target=self._arecord_capture_loop, daemon=True)
            self._thread.start()

            # Start consumer thread (processes audio and calls callback)
            self._consumer_thread = threading.Thread(target=self._audio_consumer_loop, daemon=True)
            self._consumer_thread.start()

        except Exception as e:
            print(f"Failed to start arecord: {e}")
            self._running = False

    def _arecord_capture_loop(self):
        """Read from arecord pipe, convert Nch/S32_LE → mono S16_LE.

        Each frame is channels * 4 bytes (S32_LE). Channel selection uses the
        shared _select_mono (combine="select" for the arecord/AC108 backend;
        select_channel defaults to ch1 for a 4-mic AC108 — ch0 is its silent
        reference), then shift right by 16 to convert 32-bit to 16-bit.
        """
        frame_bytes = self.channels * 4  # S32_LE = 4 bytes per sample
        chunk_bytes = self.chunk_size * frame_bytes
        proc = self._arecord_proc

        try:
            while self._running and proc and proc.poll() is None:
                data = proc.stdout.read(chunk_bytes)
                if not data or len(data) < chunk_bytes:
                    break

                # Convert multi-channel S32_LE to mono S16_LE via the shared
                # channel selector, then downshift 32→16 bit.
                s32 = np.frombuffer(data, dtype=np.int32)
                ch = self._select_mono(s32)
                s16 = (ch >> 16).astype(np.int16)

                try:
                    self._audio_queue.put_nowait(s16.tobytes())
                except queue.Full:
                    pass  # Drop chunk if consumer can't keep up
        except Exception as e:
            if self._running:
                print(f"arecord capture error: {e}")

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
            print(f"Audio capture started: {device_info['name']} (S16_LE/{self.channels}ch)")

            # Beamforming/mono-downmix runs in the consumer thread, off the read loop.
            self._consumer_transform = self._stereo_to_mono

            # Start capture thread (reads audio as fast as possible)
            self._thread = threading.Thread(target=self._pyaudio_capture_loop, daemon=True)
            self._thread.start()

            # Start consumer thread (processes audio and calls callback)
            self._consumer_thread = threading.Thread(target=self._audio_consumer_loop, daemon=True)
            self._consumer_thread.start()

        except Exception as e:
            print(f"Failed to open audio stream: {e}")
            if self._pyaudio:
                self._pyaudio.terminate()
                self._pyaudio = None
            self._running = False

    def _pyaudio_capture_loop(self):
        """PyAudio capture thread — reads audio as fast as possible.

        CRITICAL: This loop must never block on anything except stream.read().
        Any delay between reads causes the AC108 I2S kernel buffer to overflow,
        triggering an I2S SYNC error that crashes the kernel on Pi Zero 2 W.
        All heavy processing (wake word, VAD) happens in the consumer thread.
        """
        try:
            while self._running and self._stream:
                try:
                    # Read ONLY — no beamforming/numpy here. Stereo->mono is done
                    # in the consumer thread via self._consumer_transform so any
                    # delay between reads (which can overflow the I2S kernel buffer
                    # and crash the kernel on Pi Zero 2 W) is avoided.
                    audio_bytes = self._stream.read(self.chunk_size, exception_on_overflow=False)

                    # Queue raw (possibly multi-channel) audio for the consumer thread.
                    try:
                        self._audio_queue.put_nowait(audio_bytes)
                    except queue.Full:
                        pass  # Drop chunk if consumer can't keep up

                except Exception as e:
                    if self._running:
                        print(f"Audio capture error: {e}")
                    break
        finally:
            pass

    def _select_mono(self, samples: np.ndarray) -> np.ndarray:
        """Keep the configured channel from an interleaved multi-channel array,
        dropping the rest (dtype preserved).

        Shared by the S16 PyAudio path and the S32 arecord path so the channel
        choice lives in ONE place. The non-selected channels are residual /
        reference signals (XVF3800 AEC residual, AC108 silent reference), NOT
        mics — downmixing them is wrong (see the combine design doc).
        """
        return samples[self.select_channel::self.channels]

    def _stereo_to_mono(self, audio_bytes: bytes) -> bytes:
        """Reduce interleaved multi-channel S16_LE to mono per the combine mode.

        Runs in the CONSUMER thread (via self._consumer_transform), never in the
        capture read loop, so heavy numpy can't delay stream.read() (an I2S
        overflow can crash the kernel on Pi Zero 2 W).
          beamform    → DAS of the raw mics (2-mic HAT)
          select      → keep self.select_channel (XVF3800 processed beam, etc.)
          passthrough → already mono
        """
        if self.channels <= 1 or self.combine == "passthrough":
            return audio_bytes
        if self.combine == "beamform" and self._beamformer:
            return self._beamformer.process_bytes(audio_bytes)
        samples = np.frombuffer(audio_bytes, dtype=np.int16)
        return self._select_mono(samples).tobytes()

    def _audio_consumer_loop(self):
        """Consumer thread — processes queued audio and calls callback.

        Runs separately from the capture thread so that slow callback
        processing (wake word inference, VAD) AND the stereo->mono transform
        don't delay reads.
        """
        while self._running:
            try:
                audio_bytes = self._audio_queue.get(timeout=0.5)
                if self._consumer_transform is not None and audio_bytes:
                    audio_bytes = self._consumer_transform(audio_bytes)
                if self._callback and audio_bytes:
                    self._callback(audio_bytes)
            except queue.Empty:
                continue
            except Exception as e:
                if self._running:
                    print(f"Audio consumer error: {e}")

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
        # Start consumer thread
        self._consumer_thread = threading.Thread(target=self._audio_consumer_loop, daemon=True)
        self._consumer_thread.start()
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

                        # Apply beamforming if enabled (stereo -> mono)
                        if self._beamformer and self.channels == 2:
                            # audio_float shape: (samples, channels)
                            stereo = audio_float.T  # -> (channels, samples)
                            audio_float = self._beamformer.process(stereo)
                        elif len(audio_float.shape) > 1 and audio_float.shape[1] > 1:
                            # Convert to mono if stereo but no beamforming
                            audio_float = audio_float.mean(axis=1)
                        else:
                            audio_float = audio_float.flatten()

                        # Convert to 16-bit PCM bytes
                        audio_int16 = (audio_float * 32767).astype(np.int16)
                        audio_bytes = audio_int16.tobytes()

                        # Queue for consumer thread
                        try:
                            self._audio_queue.put_nowait(audio_bytes)
                        except queue.Full:
                            pass

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

        # Terminate arecord subprocess if running
        if self._arecord_proc:
            try:
                self._arecord_proc.terminate()
                self._arecord_proc.wait(timeout=3)
            except Exception:
                pass
            self._arecord_proc = None

        # Wait for capture thread to exit its read loop first
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

        # Wait for consumer thread
        if self._consumer_thread:
            self._consumer_thread.join(timeout=2.0)
            self._consumer_thread = None

        # Do NOT call self._pyaudio.terminate() — on the ReSpeaker 4-Mic
        # Array (AC108 codec), pa.terminate() triggers an I2S SYNC error
        # that crashes the kernel. The OS releases all PortAudio resources
        # when the process exits, so explicit cleanup is unnecessary.
        self._pyaudio = None

        self._stream = None
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
        except (ValueError, TypeError):
            return 0.0

    @property
    def is_running(self) -> bool:
        """Check if capture is running"""
        return self._running

    @property
    def beamformer(self) -> Optional["BeamformerDAS"]:
        """Get beamformer instance (if enabled)"""
        return self._beamformer

    def set_steering_angle(self, angle_degrees: float) -> None:
        """
        Update beamformer steering angle.

        Args:
            angle_degrees: Target direction (0=front, 90=right, -90=left)
        """
        if self._beamformer:
            self._beamformer.set_steering_angle(angle_degrees)

    def get_beamformer_stats(self) -> Optional[dict]:
        """Get beamformer statistics (if enabled)"""
        if self._beamformer:
            return self._beamformer.get_stats()
        return None


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
