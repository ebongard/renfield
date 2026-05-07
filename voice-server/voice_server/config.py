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


settings = Settings()
