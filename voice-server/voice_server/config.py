"""voice-server configuration via env vars.

All paths/URLs are env-overridable. Defaults match the k8s manifest in
`k8s/voice-server.yaml`. Local dev uses `.env` (gitignored).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import SecretStr, model_validator
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

    # Meeting diarization (§2) — pyannote turns + faster-whisper words →
    # speaker-attributed segments. The pyannote pipeline is loaded at warmup
    # ONLY when meeting_enabled (it's ~2 GB VRAM and useless on a non-meeting
    # deployment). meeting_whisper_model="" reuses the resident STT model; set a
    # larger one (e.g. large-v3-turbo) to trade GPU-seconds for accuracy — loaded
    # per job so it doesn't sit resident contending with live satellite STT.
    meeting_enabled: bool = False
    meeting_diarization_model: str = "pyannote/speaker-diarization-3.1"
    meeting_whisper_model: str = ""
    # HF token for the gated pyannote model at warmup (offline-first: the model
    # is baked into the image, so this only matters if the cache is cold).
    hf_token: SecretStr | None = None

    # TTS (B.1)
    piper_voices_dir: Path = Path("/mnt/llm/voice/piper")
    piper_default_voice_de: str = "de_DE-thorsten-medium"
    piper_default_voice_en: str = "en_US-amy-medium"
    piper_use_cuda: bool = True

    # Max concurrent /ws/voice sessions (review M4) — bounds GPU STT/TTS abuse.
    max_concurrent_sessions: int = 16

    # Opus decode-amplification guard for the satellite /stt-opus path. Bounds a
    # single decoded utterance so a body of tiny packets each declaring 120 ms
    # can't decode to gigabytes. NOT a recording cap — set generously so a long
    # spoken diary entry is never truncated (project rule: no voice max-recording
    # cap). 1800 s ≈ 57.6 MB of 16-bit mono PCM per request.
    opus_max_decoded_seconds: int = 1800

    @model_validator(mode="after")
    def _fail_closed_on_default_key(self) -> "Settings":
        """Security (review M4): refuse to start when local JWT auth is enforced
        but the signing key is still the public placeholder default. Otherwise an
        attacker could forge a valid voice token (and harvest the returned
        speaker_embedding voiceprint). auth_required=False (cluster-internal) and
        auth_mode=callback are unaffected.
        """
        if self.auth_required and self.auth_mode == "local":
            placeholder = type(self).model_fields["secret_key"].default
            if isinstance(placeholder, SecretStr):
                placeholder = placeholder.get_secret_value()
            if self.secret_key.get_secret_value() == placeholder:
                raise ValueError(
                    "VOICE secret_key is the placeholder default while "
                    "auth_required=true and auth_mode=local — refusing to start. "
                    "Set a strong random SECRET_KEY (or auth_required=false for "
                    "cluster-internal deployments)."
                )
        return self


settings = Settings()
