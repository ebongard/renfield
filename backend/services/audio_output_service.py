"""
Audio Output Service for Renfield

Handles delivery of TTS audio to output devices:
- Renfield devices (via WebSocket)
- Home Assistant media players (via HA service calls)

For HA media players, audio files are cached and served via a URL endpoint
that HA can access to play the audio.
"""

import asyncio
import os
import time
import uuid
from pathlib import Path
from typing import Optional, Dict, Any
from loguru import logger

from models.database import RoomOutputDevice
from integrations.homeassistant import HomeAssistantClient
from services.device_manager import get_device_manager
from utils.config import settings


class AudioOutputService:
    """
    Service for delivering audio to output devices.
    """

    # TTS cache directory
    TTS_CACHE_DIR = Path("/tmp/renfield_tts_cache")

    # Cache cleanup interval (in seconds)
    CACHE_CLEANUP_INTERVAL = 300  # 5 minutes

    # Max age for cached files (in seconds)
    CACHE_MAX_AGE = 600  # 10 minutes

    def __init__(self):
        self.ha_client = HomeAssistantClient()
        self._ensure_cache_dir()
        self._last_cleanup = 0

    def _ensure_cache_dir(self):
        """Ensure the TTS cache directory exists."""
        self.TTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    async def play_audio(
        self,
        audio_bytes: bytes,
        output_device: RoomOutputDevice,
        session_id: str
    ) -> bool:
        """
        Play audio on the specified output device.

        Args:
            audio_bytes: WAV audio bytes to play
            output_device: The configured output device
            session_id: Current session ID (for logging)

        Returns:
            True if audio was sent successfully
        """
        if output_device.is_renfield_device:
            return await self._play_on_renfield_device(
                audio_bytes=audio_bytes,
                device_id=output_device.renfield_device_id,
                session_id=session_id
            )
        else:
            return await self._play_on_ha_media_player(
                audio_bytes=audio_bytes,
                entity_id=output_device.ha_entity_id,
                tts_volume=output_device.tts_volume,
                session_id=session_id
            )

    async def _play_on_renfield_device(
        self,
        audio_bytes: bytes,
        device_id: str,
        session_id: str
    ) -> bool:
        """
        Play audio on a Renfield device via WebSocket.

        The device manager handles encoding and sending the audio.
        """
        device_manager = get_device_manager()
        device = device_manager.get_device(device_id)

        if not device:
            logger.warning(f"Cannot play audio: device {device_id} not connected")
            return False

        if not device.capabilities.has_speaker:
            logger.warning(f"Cannot play audio: device {device_id} has no speaker")
            return False

        try:
            # Use the device manager's send_tts_audio method
            # which handles base64 encoding and WebSocket delivery
            await device_manager.send_tts_audio(
                session_id=session_id,
                audio_bytes=audio_bytes,
                is_final=True
            )

            logger.info(f"🔊 Sent TTS audio to Renfield device {device_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to send audio to Renfield device {device_id}: {e}")
            return False

    async def _play_on_ha_media_player(
        self,
        audio_bytes: bytes,
        entity_id: str,
        tts_volume: Optional[float],
        session_id: str
    ) -> bool:
        """
        Play audio on a Home Assistant media player.

        1. Save audio to cache file
        2. Optionally adjust volume
        3. Call media_player.play_media service
        4. Optionally restore volume after playback
        """
        # Run cache cleanup periodically
        await self._cleanup_old_cache_files()

        # Save audio to cache
        audio_id = f"tts_{session_id}_{uuid.uuid4().hex[:8]}"
        audio_path = self.TTS_CACHE_DIR / f"{audio_id}.wav"

        try:
            audio_path.write_bytes(audio_bytes)
            logger.debug(f"Cached TTS audio: {audio_path}")
        except Exception as e:
            logger.error(f"Failed to cache TTS audio: {e}")
            return False

        # Build the URL for HA to fetch the audio
        # Use the backend's advertise host or fallback to localhost
        base_url = self._get_backend_url()
        audio_url = f"{base_url}/api/voice/tts-cache/{audio_id}"

        # Store original volume for restoration (if we're changing it)
        original_volume = None

        try:
            # Optionally adjust volume before playback
            if tts_volume is not None:
                original_volume = await self._get_ha_volume(entity_id)

                if original_volume != tts_volume:
                    await self.ha_client.call_service(
                        domain="media_player",
                        service="volume_set",
                        entity_id=entity_id,
                        service_data={"volume_level": tts_volume}
                    )
                    logger.debug(f"Set volume to {tts_volume} for {entity_id}")

            # Play the audio (use longer timeout as HA needs to fetch the audio file)
            success = await self.ha_client.call_service(
                domain="media_player",
                service="play_media",
                entity_id=entity_id,
                service_data={
                    "media_content_id": audio_url,
                    "media_content_type": "music"
                },
                timeout=30.0  # Longer timeout for media playback
            )

            if success:
                logger.info(f"🔊 Playing TTS audio on HA media player {entity_id}")

                # Schedule volume restoration after estimated playback time
                if original_volume is not None and original_volume != tts_volume:
                    # Estimate playback duration from audio file size
                    # WAV 16kHz mono 16-bit = ~32KB/s
                    duration_seconds = len(audio_bytes) / 32000
                    asyncio.create_task(
                        self._restore_volume_after_delay(
                            entity_id, original_volume, duration_seconds + 1.0
                        )
                    )

            return success

        except Exception as e:
            logger.error(f"Failed to play audio on HA media player {entity_id}: {e}")
            return False

    async def _get_ha_volume(self, entity_id: str) -> Optional[float]:
        """Get current volume level of a HA media player."""
        try:
            state = await self.ha_client.get_state(entity_id)
            if state:
                return state.get("attributes", {}).get("volume_level")
        except Exception as e:
            logger.error(f"Failed to get volume for {entity_id}: {e}")
        return None

    async def _restore_volume_after_delay(
        self,
        entity_id: str,
        volume: float,
        delay_seconds: float
    ):
        """Restore volume after a delay (async task)."""
        try:
            await asyncio.sleep(delay_seconds)
            await self.ha_client.call_service(
                domain="media_player",
                service="volume_set",
                entity_id=entity_id,
                service_data={"volume_level": volume}
            )
            logger.debug(f"Restored volume to {volume} for {entity_id}")
        except Exception as e:
            logger.error(f"Failed to restore volume for {entity_id}: {e}")

    def _get_backend_url(self) -> str:
        """
        Get the URL for the backend that HA can access.

        Priority:
        1. ADVERTISE_HOST from settings (required for HA integration)
        2. Fallback to localhost (warning: won't work if HA is on different host)
        """
        if settings.advertise_host:
            host = settings.advertise_host
            port = settings.advertise_port or 8000
            return f"http://{host}:{port}"

        # Fallback to localhost - this will likely not work for HA
        logger.warning("⚠️ ADVERTISE_HOST not set - HA may not be able to fetch TTS audio!")
        return "http://localhost:8000"

    async def _cleanup_old_cache_files(self):
        """Remove old TTS cache files."""
        now = time.time()

        # Only run cleanup periodically
        if now - self._last_cleanup < self.CACHE_CLEANUP_INTERVAL:
            return

        self._last_cleanup = now

        try:
            for cache_file in self.TTS_CACHE_DIR.glob("tts_*.wav"):
                file_age = now - cache_file.stat().st_mtime
                if file_age > self.CACHE_MAX_AGE:
                    cache_file.unlink()
                    logger.debug(f"Cleaned up old TTS cache file: {cache_file.name}")
        except Exception as e:
            logger.error(f"Error during TTS cache cleanup: {e}")

    def get_cached_audio(self, audio_id: str) -> Optional[bytes]:
        """
        Get cached audio bytes by ID.

        Used by the TTS cache endpoint.
        """
        audio_path = self.TTS_CACHE_DIR / f"{audio_id}.wav"

        if audio_path.exists():
            try:
                return audio_path.read_bytes()
            except Exception as e:
                logger.error(f"Failed to read cached audio {audio_id}: {e}")

        return None


# Global singleton instance
_audio_output_service: Optional[AudioOutputService] = None


def get_audio_output_service() -> AudioOutputService:
    """Get or create the global AudioOutputService instance."""
    global _audio_output_service
    if _audio_output_service is None:
        _audio_output_service = AudioOutputService()
    return _audio_output_service
