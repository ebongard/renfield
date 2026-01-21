"""
Audio Playback Module for Renfield Satellite

Handles speaker output using MPV for robust playback across
different audio backends (ALSA, PulseAudio, PipeWire).

Inspired by OHF-Voice/linux-voice-assistant approach.
"""

import asyncio
import io
import os
import tempfile
import threading
from typing import List, Optional

try:
    import mpv
    MPV_AVAILABLE = True
except ImportError:
    mpv = None
    MPV_AVAILABLE = False
    print("Warning: python-mpv not installed. Audio playback disabled.")
    print("Install with: pip install python-mpv")
    print("Also install mpv: sudo apt install mpv")


class AudioPlayback:
    """
    Plays audio through the speaker using MPV.

    MPV handles audio format conversion and device selection
    automatically, making it more robust than direct PyAudio.
    """

    def __init__(
        self,
        device: Optional[str] = None,
        volume: int = 100,
    ):
        """
        Initialize audio playback.

        Args:
            device: Audio device name (e.g., "alsa/plughw:1,0") or None for default
            volume: Volume level 0-100
        """
        self.device = device
        self.volume = volume
        self._player: Optional["mpv.MPV"] = None
        self._playing = False
        self._lock = threading.Lock()

    @staticmethod
    def list_devices() -> List[str]:
        """
        List available audio output devices.

        Returns:
            List of device names
        """
        if not MPV_AVAILABLE:
            return []

        try:
            player = mpv.MPV()
            devices = player.audio_device_list
            player.terminate()
            return [d['name'] for d in devices] if devices else []
        except Exception as e:
            print(f"Failed to list audio devices: {e}")
            return []

    def _create_player(self) -> Optional["mpv.MPV"]:
        """Create MPV player instance"""
        if not MPV_AVAILABLE:
            return None

        try:
            # Create player with audio-only settings
            player = mpv.MPV(
                video=False,
                terminal=False,
                input_default_bindings=False,
                input_vo_keyboard=False,
            )

            # Set audio device if specified
            if self.device:
                player.audio_device = self.device

            # Set volume
            player.volume = self.volume

            return player

        except Exception as e:
            print(f"Failed to create MPV player: {e}")
            return None

    def play_wav(self, wav_data: bytes) -> bool:
        """
        Play WAV audio data.

        Args:
            wav_data: WAV file data (with header)

        Returns:
            True if played successfully
        """
        if not MPV_AVAILABLE:
            print("MPV not available")
            return False

        with self._lock:
            if self._playing:
                print("Already playing audio")
                return False
            self._playing = True

        try:
            # Write WAV data to temp file (MPV needs a file)
            with tempfile.NamedTemporaryFile(
                suffix=".wav",
                delete=False
            ) as tmp_file:
                tmp_file.write(wav_data)
                tmp_path = tmp_file.name

            print(f"Playing audio: {len(wav_data)} bytes")

            # Create player
            self._player = self._create_player()
            if not self._player:
                return False

            # Play the file
            self._player.play(tmp_path)

            # Wait for playback to finish
            self._player.wait_for_playback()

            return True

        except Exception as e:
            print(f"Playback error: {e}")
            return False

        finally:
            self._cleanup()
            # Clean up temp file
            try:
                if 'tmp_path' in locals():
                    os.unlink(tmp_path)
            except:
                pass

    def play_file(self, file_path: str) -> bool:
        """
        Play audio from file path.

        Args:
            file_path: Path to audio file

        Returns:
            True if played successfully
        """
        if not MPV_AVAILABLE:
            print("MPV not available")
            return False

        with self._lock:
            if self._playing:
                print("Already playing audio")
                return False
            self._playing = True

        try:
            print(f"Playing file: {file_path}")

            # Create player
            self._player = self._create_player()
            if not self._player:
                return False

            # Play the file
            self._player.play(file_path)

            # Wait for playback to finish
            self._player.wait_for_playback()

            return True

        except Exception as e:
            print(f"Playback error: {e}")
            return False

        finally:
            self._cleanup()

    def _cleanup(self):
        """Clean up MPV resources"""
        with self._lock:
            self._playing = False

        if self._player:
            try:
                self._player.terminate()
            except:
                pass
            self._player = None

    def stop(self):
        """Stop current playback"""
        if self._player:
            try:
                self._player.stop()
            except:
                pass
        self._playing = False

    def set_volume(self, volume: int):
        """
        Set playback volume.

        Args:
            volume: Volume level 0-100
        """
        self.volume = max(0, min(100, volume))
        if self._player:
            try:
                self._player.volume = self.volume
            except:
                pass

    @property
    def is_playing(self) -> bool:
        """Check if audio is currently playing"""
        return self._playing


class AudioPlaybackAsync:
    """
    Async wrapper for AudioPlayback.

    Allows non-blocking audio playback.
    """

    def __init__(self, playback: AudioPlayback):
        self.playback = playback
        self._current_task: Optional[asyncio.Task] = None

    async def play_wav(self, wav_data: bytes) -> bool:
        """
        Play WAV audio asynchronously.

        Args:
            wav_data: WAV file data

        Returns:
            True if played successfully
        """
        loop = asyncio.get_event_loop()

        # Run blocking playback in thread pool
        return await loop.run_in_executor(None, self.playback.play_wav, wav_data)

    async def play_file(self, file_path: str) -> bool:
        """
        Play audio file asynchronously.

        Args:
            file_path: Path to audio file

        Returns:
            True if played successfully
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.playback.play_file, file_path)

    def stop(self):
        """Stop current playback"""
        self.playback.stop()

    def set_volume(self, volume: int):
        """Set volume"""
        self.playback.set_volume(volume)

    @property
    def is_playing(self) -> bool:
        """Check if audio is playing"""
        return self.playback.is_playing
