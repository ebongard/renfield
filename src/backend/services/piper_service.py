"""
Piper Service - Text to Speech (in-process Python bindings)

Uses the `piper-tts` Python package directly instead of shelling out to the
`piper` CLI per request. Eliminates the ~150-300 ms subprocess cold-start
that the previous implementation paid on every TTS call.

Voice models still live at `/usr/share/piper/voices/<voice>.onnx` (downloaded
by the Dockerfile) — we just load them in-process via PiperVoice.load() and
cache the loaded voice across requests.

Configuration via PIPER_VOICES environment variable:
  PIPER_VOICES=de:de_DE-thorsten-high,en:en_US-amy-medium
"""
import asyncio
import io
import wave
from collections import OrderedDict
from pathlib import Path
from threading import Lock

from loguru import logger

from utils.config import settings

try:
    from piper.voice import PiperVoice
    PIPER_AVAILABLE = True
except ImportError:
    PIPER_AVAILABLE = False
    PiperVoice = None  # type: ignore[assignment,misc]


class PiperService:
    """Service für Text-to-Speech mit Piper (in-process)."""

    def __init__(self):
        self.default_voice = settings.piper_default_voice
        self.voice_map = settings.piper_voice_map
        self.default_language = settings.default_language
        self.available = PIPER_AVAILABLE
        # Cache loaded PiperVoice instances by voice name. Each voice is ~50-100 MB
        # in memory (ONNX session + tokenizer); for the typical de/en pair we hold
        # ~200 MB total which is fine.
        self._voice_cache: dict[str, "PiperVoice"] = {}

        # LRU cache for synthesized WAV bytes. Keyed on (voice_name, text). Only
        # the deterministic synthesis path hits this — anything that takes per-
        # request audio params (e.g., speaker styling) must bypass.
        self._wav_cache: "OrderedDict[tuple[str, str], bytes]" = OrderedDict()
        self._wav_cache_max = max(0, int(settings.tts_cache_size))
        self._wav_cache_lock = Lock()
        self._wav_cache_hits = 0
        self._wav_cache_misses = 0

        # Bound concurrent synthesis. Voice ONNX inference is sync and gets run
        # off the event loop via asyncio.to_thread; the semaphore caps how many
        # threads can be inflight at once.
        self._max_concurrent = max(1, int(settings.tts_max_concurrent))
        self._inflight_sem: asyncio.Semaphore | None = None

        if not PIPER_AVAILABLE:
            logger.warning(
                "⚠️  piper-tts Python package not installed. TTS-Funktionen sind deaktiviert. "
                "Install with: pip install piper-tts>=1.2.0"
            )
        else:
            logger.info(
                f"🗣️ Piper voice map: {self.voice_map} · TTS cache: {self._wav_cache_max} entries"
            )

    def _cache_get(self, voice_name: str, text: str) -> bytes | None:
        if self._wav_cache_max == 0:
            return None
        key = (voice_name, text)
        with self._wav_cache_lock:
            wav = self._wav_cache.get(key)
            if wav is None:
                self._wav_cache_misses += 1
                return None
            self._wav_cache.move_to_end(key)
            self._wav_cache_hits += 1
            return wav

    def _cache_put(self, voice_name: str, text: str, wav: bytes) -> None:
        if self._wav_cache_max == 0 or not wav:
            return
        key = (voice_name, text)
        with self._wav_cache_lock:
            self._wav_cache[key] = wav
            self._wav_cache.move_to_end(key)
            while len(self._wav_cache) > self._wav_cache_max:
                self._wav_cache.popitem(last=False)

    def cache_stats(self) -> dict:
        with self._wav_cache_lock:
            return {
                "size": len(self._wav_cache),
                "max": self._wav_cache_max,
                "hits": self._wav_cache_hits,
                "misses": self._wav_cache_misses,
            }

    def _get_semaphore(self) -> asyncio.Semaphore:
        """Lazily bind the semaphore to the running loop on first use."""
        if self._inflight_sem is None:
            self._inflight_sem = asyncio.Semaphore(self._max_concurrent)
        return self._inflight_sem

    def _synthesize_sync(self, voice: "PiperVoice", text: str) -> bytes:
        """Run ONNX synthesis to a WAV byte buffer. Sync — call via to_thread.

        Deliberately accepts only `(voice, text)`. Multi-speaker `speaker_id`
        and other PiperVoice.synthesize kwargs are NOT plumbed through because
        the LRU cache keys on `(voice_name, text)` only — adding a per-call
        synthesis param without extending the key would mean cached audio is
        served for the wrong speaker. Extend the cache key first if/when we
        adopt multi-speaker voices.
        """
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            voice.synthesize(text, wav_file)
        return buffer.getvalue()

    async def _synthesize_async(self, voice: "PiperVoice", text: str) -> bytes:
        async with self._get_semaphore():
            return await asyncio.to_thread(self._synthesize_sync, voice, text)

    def _get_voice_for_language(self, language: str = None) -> str:
        """
        Get the voice name for a given language.

        Args:
            language: Language code (e.g., 'de', 'en'). Falls back to default_language.

        Returns:
            Voice name (e.g., 'de_DE-thorsten-high')
        """
        lang = (language or self.default_language).lower()
        return self.voice_map.get(lang, self.default_voice)

    def _get_model_path(self, voice: str) -> str:
        """Get the model path for a given voice."""
        return f"/usr/share/piper/voices/{voice}.onnx"

    def _load_voice(self, voice_name: str) -> "PiperVoice | None":
        """
        Load a PiperVoice from disk, with caching.

        Returns None if the model file is missing or fails to load — caller
        falls back to a no-op (matching previous behavior when piper CLI was
        absent).

        Thread-safety: `_voice_cache` is a plain dict mutated only here. This
        is safe because `_load_voice` is sync and never `await`s — under the
        single-threaded asyncio loop, no other coroutine can interleave between
        the cache read and the write below. If `_load_voice` is ever made async
        or called from a worker thread, add a lock or pre-load voices at startup.
        """
        if not PIPER_AVAILABLE:
            return None
        cached = self._voice_cache.get(voice_name)
        if cached is not None:
            return cached
        model_path = self._get_model_path(voice_name)
        if not Path(model_path).exists():
            logger.error(f"❌ Piper voice model not found: {model_path}")
            return None
        try:
            voice = PiperVoice.load(model_path)
            self._voice_cache[voice_name] = voice
            logger.info(f"📥 Piper voice geladen: {voice_name}")
            return voice
        except Exception as e:
            logger.error(f"❌ Fehler beim Laden der Piper voice {voice_name}: {e}")
            return None

    def ensure_model_downloaded(self):
        """Stelle sicher, dass das Sprachmodell heruntergeladen ist."""
        # Voice models are pre-baked into the Docker image at /usr/share/piper/voices/.
        # In-process load happens lazily on first synthesis call via _load_voice().
        return

    async def synthesize_to_file(self, text: str, output_path: str, language: str = None) -> bool:
        """
        Text zu Audio-Datei synthetisieren.

        Args:
            text: Text to synthesize
            output_path: Path for output audio file
            language: Optional language code (e.g., 'de', 'en'). Falls back to default_language.
        """
        if not self.available:
            logger.warning("Piper nicht verfügbar, TTS übersprungen")
            return False

        voice_name = self._get_voice_for_language(language)
        cached = self._cache_get(voice_name, text)
        if cached is not None:
            try:
                with open(output_path, "wb") as f:
                    f.write(cached)
                return True
            except Exception as e:
                logger.error(f"❌ TTS Cache-Datei-Schreibfehler ({voice_name}): {e}")
                return False

        voice = self._load_voice(voice_name)
        if voice is None:
            return False

        try:
            wav_bytes = await self._synthesize_async(voice, text)
            self._cache_put(voice_name, text, wav_bytes)
            with open(output_path, "wb") as f:
                f.write(wav_bytes)
            logger.info(f"✅ TTS erfolgreich ({voice_name}): {output_path}")
            return True
        except Exception as e:
            logger.error(f"❌ TTS Fehler ({voice_name}): {e}")
            return False

    async def synthesize_to_bytes(self, text: str, language: str = None) -> bytes:
        """
        Text zu Audio-Bytes synthetisieren.

        Args:
            text: Text to synthesize
            language: Optional language code (e.g., 'de', 'en'). Falls back to default_language.
        """
        if not self.available:
            logger.warning("Piper nicht verfügbar, TTS übersprungen")
            return b""

        voice_name = self._get_voice_for_language(language)
        cached = self._cache_get(voice_name, text)
        if cached is not None:
            return cached

        voice = self._load_voice(voice_name)
        if voice is None:
            return b""

        try:
            wav_bytes = await self._synthesize_async(voice, text)
            self._cache_put(voice_name, text, wav_bytes)
            return wav_bytes
        except Exception as e:
            logger.error(f"❌ TTS Fehler ({voice_name}): {e}")
            return b""


_piper_instance: PiperService | None = None


def get_piper_service() -> PiperService:
    """Get the PiperService singleton."""
    global _piper_instance
    if _piper_instance is None:
        _piper_instance = PiperService()
    return _piper_instance
