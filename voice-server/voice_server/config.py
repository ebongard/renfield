"""voice-server configuration via env vars.

All paths/URLs are env-overridable. Defaults match the k8s manifest in
`k8s/voice-server.yaml`. Local dev uses `.env` (gitignored).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "INFO"

    # Auth (D5)
    # auth_required=False mirrors backend's AUTH_ENABLED=false — for
    # single-user / no-auth deployments where /ws/voice is reachable
    # only on the cluster network. When False, authenticate() short-
    # circuits and returns an anonymous payload. JWT validation is
    # still applied when a token IS provided so the same image
    # runs in both modes.
    auth_required: bool = True
    auth_mode: Literal["local", "callback"] = "local"
    secret_key: SecretStr = SecretStr("changeme-in-production")
    jwt_algorithm: str = "HS256"
    auth_callback_url: str | None = None  # used when auth_mode=callback

    # STT (D1) — accepts a local path OR an HF model id (downloads to HF cache)
    whisper_model: str = "Systran/faster-whisper-medium"
    whisper_compute_type: Literal["int8_float16", "float16", "int8"] = "int8_float16"
    whisper_device: Literal["cuda", "cpu"] = "cuda"
    whisper_language_default: str = "de"
    whisper_vad_min_silence_ms: int = 500

    # Speaker (D4)
    speaker_model_path: Path = Path("/mnt/llm/voice/ecapa_tdnn.onnx")
    speaker_providers: list[str] = ["CUDAExecutionProvider", "CPUExecutionProvider"]

    # TTS (B.1)
    piper_voices_dir: Path = Path("/mnt/llm/voice/piper")
    piper_default_voice_de: str = "de_DE-thorsten-medium"
    piper_default_voice_en: str = "en_US-amy-medium"
    piper_use_cuda: bool = True

    # B.5 — XTTS-v2 spike (off in production; on in the spike image only).
    # When False, main.py does NOT construct XTTSService, the coqui-tts
    # import is never triggered, and engine=xtts-* requests at the REST
    # endpoint return 503. This keeps v0.1.5 (which doesn't ship coqui-tts)
    # importable and runnable unchanged.
    xtts_enabled: bool = False
    # XTTS model lives in HF cache; resolved at load time. Override path
    # only if the cache layout differs from the standard HF snapshot.
    xtts_repo_id: str = "coqui/XTTS-v2"
    # When engine=xtts-clone, this is the speaker_wav reference passed to
    # XTTS. Set to None for a non-cloning default-speaker run. Step 3
    # of the plan synthesises the reference clip via Piper-thorsten and
    # stores it at the path below.
    xtts_clone_voice_ref: Path | None = Path("/mnt/llm/voice/xtts_refs/thorsten_ref.wav")
    xtts_use_cuda: bool = True
    # Per the plan, sample-rate parity normalises everything to 22.05 kHz
    # before measurement and listening (Piper's native rate). XTTS native
    # is 24 kHz; we resample its output.
    xtts_target_sample_rate: int = 22050
    # XTTS-v2 drifts and OOMs on long inputs. Plan-imposed manual chunking
    # threshold matches the existing _split_sentences default (240 chars).
    xtts_max_chunk_chars: int = 240


settings = Settings()
