"""voice-server configuration via env vars.

All paths/URLs are env-overridable. Defaults match the k8s manifest in
`k8s/voice-server.yaml`. Local dev uses `.env` (gitignored).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AuthClient(BaseModel):
    """One row of the registry auth client map (AUTH_MODE=registry).

    Exactly one of the two shapes is valid:
      {"verify_url": "http://backend.example/api/internal/auth/verify"}
        — tokens from this client are POSTed to ITS OWN backend for
          verification; voice-server holds no signing keys.
      {"anonymous": true}
        — this client may connect without a token, but ONLY via the
          dedicated anonymous listener port (`anon_port`), which the
          deployment restricts with a NetworkPolicy. Exists for the
          household deployment whose identity model is voice-biometric +
          presence, not JWT login.
    """

    verify_url: str | None = None
    anonymous: bool = False
    # Optional shared secret sent as the X-Verify-Secret header on this
    # client's verify POST. The client's backend gates its (unauthenticated-
    # by-design) verify endpoint on it — closes the token oracle to anyone who
    # can merely reach the endpoint. Only meaningful with verify_url.
    verify_secret: SecretStr | None = None

    @model_validator(mode="after")
    def _exactly_one_shape(self) -> "AuthClient":
        if self.anonymous and self.verify_url:
            raise ValueError(
                "auth client row must be EITHER anonymous OR verify_url, not both"
            )
        if not self.anonymous and not self.verify_url:
            raise ValueError(
                "auth client row needs verify_url (or anonymous: true)"
            )
        if self.verify_url and not self.verify_url.startswith(("http://", "https://")):
            raise ValueError(f"verify_url must be http(s), got: {self.verify_url}")
        if self.verify_secret and not self.verify_url:
            raise ValueError("verify_secret is only valid with verify_url")
        return self


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
    auth_mode: Literal["local", "callback", "registry"] = "local"
    secret_key: SecretStr = SecretStr("changeme-in-production")
    jwt_algorithm: str = "HS256"
    auth_callback_url: str | None = None  # used when auth_mode=callback
    # Shared secret sent as X-Verify-Secret on the callback POST (auth_mode=
    # callback). The registry mode carries its own per-client verify_secret in
    # each AuthClient row instead.
    auth_callback_secret: SecretStr | None = None

    # Multi-client registry (auth_mode=registry). Env AUTH_CLIENTS is a JSON
    # object mapping client-id → row, e.g.
    #   AUTH_CLIENTS='{"reva": {"verify_url": "http://192.168.99.101/api/internal/auth/verify"},
    #                  "renfield": {"anonymous": true}}'
    # REST callers identify via the `X-Voice-Client` header, WS via `?client=`.
    # Every request MUST name a registered client; per-user identity is
    # namespaced (client_id, user_id) — user 5 of one product is never user 5
    # of another.
    auth_clients: dict[str, AuthClient] = {}
    # Second listener where `anonymous: true` rows are honored. The k8s
    # deployment points a separate ClusterIP Service here and restricts it
    # with a NetworkPolicy; the primary port NEVER serves anonymous registry
    # clients, so an ingress-reachable request can't claim the anonymous row.
    anon_port: int = 8081
    # On the anon listener, a request with NO X-Voice-Client defaults to this
    # client id. Lets a caller that predates the X-Voice-Client header (e.g.
    # the renfield household backend) use the shared instance via the fenced
    # anon port. Must name an `anonymous: true` row. Empty = off (missing
    # client id is rejected as usual).
    anon_default_client: str = ""

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
    # Chunked transcription: recordings longer than this are diarized + ASR'd in
    # bounded time-windows (peak VRAM ∝ window, not whole recording — so a
    # multi-hour meeting fits a shared GPU and CTranslate2 doesn't retain a huge
    # workspace). Chunk-local speakers are stitched into global ones by ECAPA
    # cosine ≥ meeting_speaker_match_threshold. 0 disables chunking (single pass).
    meeting_chunk_seconds: int = 480          # 8 min → ~3-4 GB peak
    meeting_speaker_match_threshold: float = 0.55
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

    @model_validator(mode="after")
    def _fail_closed_on_empty_registry(self) -> "Settings":
        """Registry mode with no clients would reject every request — that is a
        deploy-time misconfiguration (e.g. an `apply -k` wiping AUTH_CLIENTS to
        empty), not a runtime condition. Refuse to start so it can't ship dark.
        """
        if self.auth_mode == "registry":
            if not self.auth_clients:
                raise ValueError(
                    "auth_mode=registry but AUTH_CLIENTS is empty — refusing to "
                    "start. Register at least one client "
                    '(e.g. {"reva": {"verify_url": "http://..."}}).'
                )
            if self.anon_port == self.port:
                raise ValueError(
                    "anon_port must differ from the primary port — the anonymous "
                    "listener is a separate NetworkPolicy-enforceable boundary."
                )
        return self


settings = Settings()
