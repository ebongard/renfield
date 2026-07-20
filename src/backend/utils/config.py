"""
Konfiguration und Settings
"""
import json
import os
from functools import lru_cache
from typing import Literal

from loguru import logger
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings


# W13 — Names of fields whose Settings-class default is a placeholder
# meant to fail loudly when running against any real environment. The
# `Settings.warn_on_changeme_defaults()` validator reads each name's
# CURRENT default off `Settings.model_fields[name].default` at runtime
# and compares to the resolved value — no hand-maintained mirror string
# that can silently drift if someone changes the default literal.
#
# Update this list when introducing a new placeholder-defaulted secret.
_CHANGEME_FIELDS: tuple[str, ...] = (
    "postgres_password",
    "secret_key",
    "default_admin_password",
)


class Settings(BaseSettings):
    """Anwendungs-Einstellungen"""

    # Edition & Feature Flags
    #
    # Each `feature_*` field maps to a UI nav item / route guard. `None`
    # means "fall through to the edition preset". Setting True or False
    # via env (e.g. FEATURE_KNOWLEDGE_GRAPH=true) overrides the preset.
    #
    # If you add a feature key to the `features` property below, ALSO add
    # the matching `feature_<name>: bool | None = None` field here —
    # otherwise Pydantic Settings has nothing to bind the env var to and
    # the override silently no-ops while the preset wins. (Cherry-pick
    # 4f3344a originally added tasks/knowledge/knowledge_graph to the
    # property without the fields, breaking per-deploy overrides until
    # this commit.)
    renfield_edition: str = "community"  # "community" (full/home) or "pro" (business, no smart home)
    feature_smart_home: bool | None = None       # None = use edition default
    feature_cameras: bool | None = None          # None = use edition default
    feature_satellites: bool | None = None       # None = use edition default
    feature_voice: bool | None = None            # None = use edition default
    feature_tasks: bool | None = None            # None = use edition default
    feature_knowledge: bool | None = None        # None = use edition default
    feature_knowledge_graph: bool | None = None  # None = use edition default

    # Day/night awareness — time-of-day windows used by services/daypart_service.py
    # to compute the current daypart (day/evening/night) for the agent prompt and
    # the `daypart_changed` hook. Windows are HH:MM in the local timezone.
    daypart_timezone: str = ""  # empty => reuse ha_glue presence_analytics_timezone, else UTC
    daypart_night_start: str = "22:00"
    daypart_night_end: str = "07:00"
    daypart_evening_start: str = "18:00"
    # Satellite LED brightness (0-31, APA102/XVF3800 scale) per daypart. The
    # backend pushes the night level to all connected satellites on the
    # `daypart_changed` → night transition (and rides it in register_ack so a
    # mid-night reconnect comes up dimmed). Animations are never disabled — only
    # their brightness is scaled. See ha_glue/services/led_dimming_service.py.
    led_day_brightness: int = 20
    led_night_brightness: int = 5

    # Datenbank - Einzelfelder für dynamischen DATABASE_URL-Aufbau
    database_url: str | None = None
    postgres_user: str = "renfield"
    postgres_password: SecretStr = "changeme"
    postgres_host: str = "postgres"
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_db: str = "renfield"
    # 10 + 20 = 30 connections/process. backend + document-worker each get their
    # own pool → 2 × 30 = 60 max, comfortably under Postgres max_connections=100.
    # (Briefly bumped to 15+30 during the 2026-07-01 folder-ingest backlog flood,
    # but that only treated the symptom — the real cause was the Paperless leg
    # holding a pooled connection across a multi-second external wait on the push
    # path. That is now decoupled to the async paperless_reconciler (Design Z), so
    # the original headroom is sufficient again.) Env-overridable if the DB's
    # max_connections is raised. Do NOT push per-process total past ~45 while
    # max_connections=100 and two processes share it, or you trade pool timeouts
    # for "too many connections".
    db_pool_size: int = Field(default=10, ge=1, le=100)
    db_max_overflow: int = Field(default=20, ge=0, le=200)
    db_pool_recycle: int = Field(default=3600, ge=60, le=86400)

    # Redis
    redis_url: str = "redis://redis:6379"

    # Ollama - Multi-Modell Konfiguration
    ollama_url: str = "http://ollama:11434"
    ollama_model: str = "llama3.2:3b"  # Legacy fallback; recommended: qwen3:14b
    ollama_chat_model: str = "llama3.2:3b"      # Default for dev; recommended: qwen3:14b
    ollama_rag_model: str = "llama3.2:latest"   # Default for dev; recommended: qwen3:14b
    ollama_embed_model: str = "nomic-embed-text" # Default for dev; recommended: qwen3-embedding:4b (2560 dim)
    ollama_intent_model: str = "llama3.2:3b"    # Default for dev; recommended: qwen3:8b
    ollama_num_ctx: int = 32768                   # Context window für alle Ollama-Calls
    ollama_connect_timeout: float = 10.0          # TCP connect timeout in seconds (fast-fail when host is down)
    ollama_read_timeout: float = 300.0            # Read timeout for long LLM responses
    ollama_fallback_url: str = ""                 # Fallback Ollama URL if primary is unreachable (e.g. http://host.docker.internal:11434)
    ollama_vision_model: str = ""                  # Vision-capable model (e.g. "minicpm-v"). Empty = vision disabled.
    ollama_vision_url: str | None = None         # Separate Ollama URL for vision model (default: ollama_url)
    # Paperless metadata extraction — per docs/design/paperless-llm-metadata.md
    # § 2. Vision-first on scanned docs, Docling text-layer shortcut for
    # plain-text PDFs/docx/md. Empty → falls back to ollama_vision_model,
    # then ollama_chat_model. The extractor uses whichever is set.
    paperless_extraction_model: str = ""
    ollama_embed_url: str | None = None          # Separate Ollama URL for embeddings (default: ollama_url)

    # Voice-server (B.4) — when set, backend's /api/voice/voice-chat
    # orchestrator delegates STT + TTS to the voice-server pod instead
    # of running them in-process. None = legacy in-process path
    # (whisper_service + piper_service). After B.4 lands the in-process
    # path becomes a fallback for dev environments without a
    # voice-server deployment.
    voice_server_url: str | None = None

    # When False, the backend sends NO bearer token to the voice-server, so a
    # voice-server running auth_required=false treats the call as anonymous.
    # Set False on an instance that shares ANOTHER instance's voice-server: its
    # own SECRET_KEY differs, so a token it minted would fail signature
    # verification (401) even though the voice-server is "unauthenticated" (that
    # flag only skips auth for a MISSING token, not a present-but-invalid one).
    # Default True = the normal same-instance path (local-mode JWT validation).
    # Interim until the shared voice-server gets real multi-tenant auth.
    voice_server_auth_enabled: bool = True

    # Client id for the shared voice-server's registry auth (AUTH_MODE=registry).
    # When set, the backend sends it as the X-Voice-Client header on every
    # voice-server call so the server routes token verification to THIS product's
    # backend (or honors its anonymous row). Empty = omit the header, which keeps
    # the legacy local/callback single-tenant voice-servers working unchanged.
    # Each deployment sets its own: reva=reva, xidra=xidra, household=renfield.
    voice_client_id: str = ""

    # Home Assistant / Frigate settings moved to ha_glue/utils/config.py
    # (see `HaGlueSettings`). Access via:
    #     from ha_glue.utils.config import ha_glue_settings
    # Env var names (HOME_ASSISTANT_URL, HOME_ASSISTANT_TOKEN, FRIGATE_URL,
    # FRIGATE_TIMEOUT) are unchanged.

    # n8n — field exists so .env can set N8N_API_URL for the n8n-mcp stdio subprocess
    n8n_api_url: str | None = None

    # MCP integration toggles (used by mcp_servers.yaml)
    weather_enabled: bool = False
    # Home location for the kiosk weather tile (city or postal code). Empty =
    # no weather tile on /kiosk. Kept in the env, never in git (no real place
    # names committed). Consumed by the kiosk weather push (api/websocket/kiosk_data.py).
    kiosk_weather_location: str = ""
    news_enabled: bool = False
    search_enabled: bool = False
    calendar_enabled: bool = False

    # Sprache
    default_language: str = "de"
    supported_languages: str = "de,en,it"  # Comma-separated list of supported languages
    whisper_model: str = "base"
    # Recommended overrides via env: WHISPER_MODEL=medium for CPU production,
    # WHISPER_MODEL=large-v3 on GPU hosts (~3 GB VRAM in float16).
    whisper_device: str = "cpu"  # "cpu" or "cuda" — set WHISPER_DEVICE=cuda on GPU hosts
    whisper_compute_type: str = "int8"  # CPU default. Use "float16" with device=cuda; "int8_float16" is the GPU low-memory mode.
    whisper_beam_size: int = 5
    whisper_initial_prompt: str = ""  # Leer = kein Kontext-Bias (Renfield ist ein offenes System)
    piper_voices: str = "de:de_DE-thorsten-high,en:en_US-amy-medium"  # Language:Voice mapping
    piper_default_voice: str = "de_DE-thorsten-high"  # Fallback voice when requested language has no entry in piper_voices
    # TTS LRU cache for synthesized WAV bytes. Keyed on (voice, text). 0 disables.
    # Repeated confirmations ("Verstanden", "Bestätigt", "Wird erledigt") dominate
    # household TTS; caching them avoids redundant ONNX inference. Each WAV is
    # ~50-200 KB; default of 256 caps memory at ~50 MB.
    tts_cache_size: int = 256
    # Bound concurrent inference so a burst of N satellites speaking at once
    # doesn't OOM the box. faster-whisper / piper are thread-safe at the model
    # level, so the Semaphore gates request submission, not the model itself.
    whisper_max_concurrent: int = 2
    tts_max_concurrent: int = 4

    # Audio Preprocessing (for better STT quality)
    whisper_preprocess_enabled: bool = True       # Enable audio preprocessing before Whisper
    whisper_preprocess_noise_reduce: bool = True  # Enable noise reduction (removes background noise)
    whisper_preprocess_normalize: bool = True     # Enable audio normalization (consistent volume)
    whisper_preprocess_target_db: float = -20.0   # Target dB level for normalization

    # Speaker Recognition
    speaker_recognition_enabled: bool = True      # Enable speaker recognition
    # P0 of docs/design/voice-identity-wakeword-verification.md (fail-loud
    # fallback): the in-process SpeechBrain ECAPA and the voice-server ONNX
    # ECAPA do NOT share a representation space, yet both historically wrote
    # into the same speaker tables. Default OFF = the backend refuses to
    # extract/compare/store SpeechBrain embeddings (STT itself still works);
    # each refusal logs a WARNING + increments
    # renfield_speaker_inprocess_embedding_blocked_total. Only set True in a
    # dev environment that has NO voice-server and accepts a separate,
    # incompatible embedding space.
    speaker_inprocess_embeddings_enabled: bool = False
    speaker_recognition_threshold: float = 0.25  # Minimum similarity for positive identification (0-1)
    speaker_recognition_device: str = "cpu"      # Device for inference: "cpu" or "cuda"
    speaker_auto_enroll: bool = True             # Auto-create unknown speakers and save embeddings
    speaker_continuous_learning: bool = True     # Add embeddings to known speakers on each interaction
    # Phase 0 quality gating (docs/design/speaker-enrollment-redesign.md). Dark by
    # default → matching/enroll byte-identical when off. On: L2-normalize each
    # embedding before averaging the reference centroid; skip auto-enroll +
    # continuous-learning for too-short turns; only reinforce a profile on a
    # strong match (stops the noisy-turn pollution loop). Duration is best-effort
    # (only the voice-server HTTP paths pass it today; the WS frame doesn't yet).
    speaker_quality_gating_enabled: bool = False
    speaker_recognition_min_duration_s: float = 1.0   # too-short turns don't enroll/reinforce
    speaker_continuous_learning_min_confidence: float = 0.45  # only reinforce on a strong match
    # Controlled enrollment (Phase 1): a deliberate, guided, quality-gated flow
    # that builds ONE trusted reference profile per person via the voice-server
    # ONNX model (same as inference). A sample must be >= min_duration; the set
    # must have >= min_samples that mutually cohere (mean pairwise cosine >=
    # min_cohesion) or the enrollment is rejected — the anti-pollution key.
    speaker_enroll_min_duration_s: float = 2.0
    speaker_enroll_min_samples: int = 3
    speaker_enroll_min_cohesion: float = 0.5
    # Phase 3 (controlled recognition, docs/design/speaker-enrollment-redesign.md).
    # Dark by default → passive recognition unchanged. When ON: identify a turn
    # against ENROLLED reference profiles only, require the best match to beat the
    # runner-up by `speaker_match_min_margin`, do NOT auto-enrol on a miss (a
    # quality-passing unknown goes to the review bucket instead), and never
    # reinforce a reference profile from a passive turn (references are immutable).
    speaker_controlled_enrollment_enabled: bool = False
    speaker_match_min_margin: float = 0.1       # best must beat 2nd-best by this
    speaker_review_bucket_cap: int = 200        # max retained review candidates
    # Per-user vocabulary corpus capture (Phase B-3 follow-up). Confirmed-
    # speaker transcripts are appended to speaker_vocabulary_corpus and a
    # daily batch job rebuilds the per-user vocab table for STT bias.
    speaker_vocab_capture_enabled: bool = True
    speaker_vocab_rebuild_interval_seconds: int = 86400  # Daily

    # Room Management / Satellite OTA moved to ha_glue/utils/config.py.

    # Output Routing
    advertise_host: str | None = None  # Hostname/IP that external services (like HA) can reach
    advertise_port: int = 8000            # Port for advertise_host
    advertise_scheme: Literal["http", "https"] = "http"  # URL scheme for advertise_host-built media URLs. https requires renderers to resolve+trust advertise_host's cert. Pair https with ADVERTISE_PORT=443.
    backend_internal_url: str = "http://backend:8000"  # Internal URL for Docker networking (fallback when advertise_host not set)

    # Wake Word Detection
    wake_word_enabled: bool = False  # Disabled by default (opt-in)
    wake_word_default: str = "hey_renfield"  # Default wake word
    wake_word_threshold: float = 0.5
    wake_word_cooldown_ms: int = 2000

    # Satellite OTA Updates — moved to ha_glue/utils/config.py.

    # Agent (ReAct Loop)
    agent_enabled: bool = False           # Opt-in, disabled by default
    agent_max_steps: int = Field(default=12, ge=1, le=50)
    agent_step_timeout: float = Field(default=30.0, ge=1.0, le=300.0)
    agent_total_timeout: float = Field(default=120.0, ge=5.0, le=600.0)
    agent_model: str | None = None     # Optional: separate model for agent (default: ollama_model)
    agent_ollama_url: str | None = None # Optional: separate Ollama instance for agent (default: ollama_url)

    # Follow-up suggestion chips (chat-ui roadmap item 2). Opt-in/dark. After an
    # assistant answer, a small best-effort LLM call proposes 2-4 tappable
    # follow-up questions, attached to the `done` frame (ephemeral, not persisted).
    # Failure/timeout silently yields no chips — never blocks the turn.
    followup_chips_enabled: bool = False
    followup_chips_model: str = ""              # "" → ollama_intent_model (small/fast tier)
    followup_chips_count: int = Field(default=3, ge=1, le=5)
    followup_chips_timeout_seconds: float = Field(default=5.0, ge=1.0, le=30.0)

    # OpenAI-compatible LLM endpoint (e.g. llama-server). When set, the agent
    # tier (and optionally chat/RAG/intent via per-tier overrides below) routes
    # through this endpoint instead of Ollama. The URL must include the
    # OpenAI-compatible path prefix, typically `…/v1`.
    llm_openai_base_url: str | None = None
    llm_openai_api_key: SecretStr | None = None    # Any non-empty string is accepted by llama-server
    llm_openai_model: str = "qwen3.6"               # Logical model name exposed by the server (`--alias`)
    # Reasoning-effort control for reasoning models behind the OpenAI-compat
    # endpoint ("minimal"/"low"/"medium"/"high"/"none"; provider-dependent).
    # Emitted as `reasoning_effort` in the request body ONLY when set —
    # OpenRouter/OpenAI-style APIs honor it, llama-server ignores unknown
    # fields, and the default (None) keeps local deployments byte-identical.
    # Without it, reasoning models (GLM-5.x, Kimi, DeepSeek) run at their
    # default (high) effort: measured ~30s time-to-first-token per agent
    # step, ~100s per multi-step turn. Literal-validated: a typo here would
    # otherwise 400 EVERY call on enum-validating providers across all tiers
    # riding the endpoint.
    llm_openai_reasoning_effort: Literal["minimal", "low", "medium", "high", "none"] | None = None
    # Per-tier opt-in: when True, that tier uses llm_openai_base_url instead of Ollama.
    # `agent` defaults to True if llm_openai_base_url is set; chat/rag/intent default
    # to following the agent setting unless explicitly overridden.
    llm_openai_for_agent: bool | None = None
    llm_openai_for_chat: bool | None = None
    llm_openai_for_rag: bool | None = None
    llm_openai_for_intent: bool | None = None
    llm_openai_for_kg: bool | None = None
    llm_openai_for_memory: bool | None = None

    # Separate OpenAI-compatible endpoint for embeddings (a llama-server pod
    # configured with `--embedding`, hosting an embedding-specific GGUF like
    # Qwen3-Embedding-4B). When set, embeddings route here instead of Ollama.
    llm_openai_embed_base_url: str | None = None
    llm_openai_embed_model: str = "qwen3-embedding"
    agent_conv_context_messages: int = 12  # Number of conversation history messages in agent loop
    conversation_summary_threshold: int = 10  # Trigger LLM summary when message count exceeds this
    agent_roles_path: str = "config/agent_roles.yaml"  # Path to agent role definitions
    agent_router_timeout: float = 30.0    # Timeout for router classification LLM call (seconds)
    agent_router_model: str | None = None  # Dedicated router model (default: ollama_intent_model)
    agent_router_url: str | None = None    # Dedicated Ollama URL for router (default: agent_ollama_url)
    agent_orchestrator_enabled: bool = False  # Enable cross-MCP query orchestration (opt-in)
    # Card-emit-inline (card-flip UX fix). When True, the WebSocket chat
    # handler awaits the `build_assistant_card` hook AFTER the agent loop
    # produces its final answer but BEFORE the `done` marker, and emits
    # the card in the same logical event as the streamed prose. When
    # False (default), the call site is dormant and cards are emitted by
    # the fire-and-forget `post_message` hook after `done` (legacy
    # behaviour — prose appears, card overlays it ~1s later).
    #
    # Default False on purpose: the renfield call site and the Reva-side
    # `on_post_message` card-branch gate land as separate PRs with a
    # submodule bump between. A deploy window with new-renfield +
    # old-Reva would emit TWO cards (chat_handler inline AND the
    # un-gated post_message hook) if this defaulted True. Ship both
    # halves, deploy, verify both SHAs in /api/health, THEN flip to True
    # via a ConfigMap patch (no rebuild) — and flip back the same way if
    # `reva_cards_render_errors_total` spikes. See the Reva repo
    # docs/plans/card-emit-inline.md "Rollout" section.
    card_emit_inline: bool = False
    # W5 — previously hardcoded timeouts now configurable
    agent_preselect_timeout: float = Field(default=10.0, ge=1.0, le=60.0)
    """Timeout for tool pre-selection LLM call in agent_service.py:_preselect_tools.
    Short JSON-only response, deterministic — keep low to fail fast."""
    orchestrator_synthesis_timeout: float = Field(default=30.0, ge=5.0, le=300.0)
    """Timeout for orchestrator's synthesis call (combine sub-agent results into one answer)."""

    # MCP Client (Model Context Protocol)
    mcp_enabled: bool = False             # Opt-in, disabled by default
    mcp_config_path: str = "config/mcp_servers.yaml"
    mcp_refresh_interval: int = 60        # Background refresh interval (seconds)
    mcp_connect_timeout: float = 10.0     # Connection timeout per server (seconds)
    mcp_call_timeout: float = 30.0        # Tool call timeout (seconds)
    mcp_max_response_size: int = Field(default=131072, ge=1024, le=524288)  # 128KB max response — accommodates list_correspondents on real corpora (~70KB at ~900 entries) without truncating mid-payload
    # MCP exponential-backoff for reconnect / transient failures
    mcp_backoff_initial_delay: float = Field(default=1.0, ge=0.1, le=60.0)
    mcp_backoff_max_delay: float = Field(default=300.0, ge=1.0, le=3600.0)
    mcp_backoff_multiplier: float = Field(default=2.0, ge=1.0, le=10.0)
    mcp_backoff_jitter: float = Field(default=0.1, ge=0.0, le=1.0)

    # W5 — previously hardcoded timeouts now configurable
    geocode_http_timeout: float = Field(default=8.0, ge=1.0, le=30.0)
    """HTTP timeout for the Nominatim geocode httpx client in mcp_client.py."""
    federation_synthesis_timeout: float = Field(default=30.0, ge=5.0, le=59.0)
    """Federation responder synthesis timeout. Hard upper bound 59s because the
    responder TTL is 60s and synthesis must fit inside that along with
    retrieval and the poll-reply round trip. The Field constraint enforces
    this, not just the comment."""

    # Agent Advanced
    agent_history_limit: int = Field(default=20, ge=1, le=100)       # Max history steps in agent loop
    agent_response_truncation: int = Field(default=2000, ge=100, le=50000)  # Max chars for tool response truncation
    agent_budget_threshold: float = Field(default=0.85, ge=0.5, le=0.99)   # Token budget utilization threshold (triggers reduction above this)
    agent_parallel_tools: bool = True                                       # Allow multi-action in single step
    agent_orchestrator_parallel: bool = True                                # Run orchestrator sub-agents in parallel

    # Embeddings
    embedding_dimension: int = Field(default=768, ge=128, le=4096)   # Embedding vector dimension

    # RAG (Retrieval-Augmented Generation)
    rag_enabled: bool = True
    rag_chunk_size: int = Field(default=512, ge=64, le=4096)
    rag_chunk_overlap: int = Field(default=50, ge=0, le=512)
    rag_top_k: int = Field(default=20, ge=1, le=100)
    rag_similarity_threshold: float = Field(default=0.4, ge=0.0, le=1.0)

    # Hybrid Search (Dense + BM25 via PostgreSQL Full-Text Search)
    rag_hybrid_enabled: bool = True           # Enable hybrid search (BM25 + dense)
    rag_hybrid_bm25_weight: float = Field(default=0.5, ge=0.0, le=1.0)
    rag_hybrid_dense_weight: float = Field(default=0.5, ge=0.0, le=1.0)
    rag_hybrid_rrf_k: int = 60                # RRF constant k (standard: 60)
    rag_hybrid_fts_config: str = "german"     # PostgreSQL FTS config: simple/german/english

    # Embedding
    rag_embedding_timeout: float = 30.0       # Timeout in seconds for embedding calls

    # W5 — RAG eval LLM timeouts (previously hardcoded as 60 / 30 in rag_eval_service.py)
    rag_eval_answer_timeout: float = Field(default=60.0, ge=10.0, le=300.0)
    """Timeout for the eval pipeline's answer-generation LLM call."""
    rag_eval_score_timeout: float = Field(default=30.0, ge=5.0, le=180.0)
    """Timeout for the eval pipeline's per-criterion LLM-as-judge scoring call."""

    # Context Window Retrieval
    rag_context_window: int = 1               # Adjacent chunks per direction (0=disabled)
    rag_context_window_max: int = 3           # Maximum allowed window size

    # Contextual Retrieval (LLM-generated context prefix per chunk)
    rag_contextual_retrieval: bool = True      # Generate context prefix during ingestion
    rag_contextual_model: str | None = None    # LLM model for context generation (default: ollama_chat_model)

    # Parent-Child Chunking
    rag_parent_child_enabled: bool = True      # Small chunks for retrieval, large for context
    rag_child_chunk_size: int = Field(default=256, ge=64, le=2048)
    rag_parent_chunk_size: int = Field(default=1024, ge=256, le=4096)

    # Reranking
    rag_rerank_enabled: bool = True            # Rerank results with dedicated model
    rag_rerank_model: str = "mxbai-rerank-base-v1"
    rag_rerank_top_k: int = Field(default=5, ge=1, le=50)  # Final results after reranking

    # OCR Processing
    rag_force_ocr: bool = False               # Always force full-page OCR (ignores embedded text)
    rag_ocr_auto_detect: bool = True          # Auto-detect garbled embedded text and re-run with OCR
    rag_ocr_space_threshold: float = 0.03    # Space ratio below this triggers auto OCR (default 3%)
    # Page-raster scale for the force_full_page_ocr converter. Memory during a
    # full-page-OCR pass scales ~quadratically with this (a multi-page PDF rasters
    # every page at this factor). 2.0 doubled accuracy-vs-memory; dropped to 1.5 to
    # keep the OCR re-conversion within the worker/backend 6Gi limit after a hybrid
    # doc OOM'd ingestion (the Docling-default → force-OCR re-conversion held two
    # converters + rasters at once). Raise toward 2.0 only if OCR accuracy regresses
    # AND the memory limit is also raised.
    rag_ocr_images_scale: float = Field(default=1.5, ge=0.5, le=4.0)
    # Per-chunk-rate trigger that complements rag_ocr_space_threshold.
    # When the chunker drops more than this fraction of chunks as
    # low-quality (utils.content_quality.is_low_quality_text), the
    # document is re-converted with force_full_page_ocr. If the re-run
    # ALSO trips, status='failed' with error_message='ocr_quality_low'.
    # Documents called with force_ocr=True that still trip get the
    # distinct error_message='ocr_quality_low_after_forced_ocr' so the
    # maintenance UI can distinguish "tried our best" from "first attempt".
    rag_chunk_quality_drop_threshold: float = Field(default=0.30, ge=0.0, le=1.0)
    # Text-layer UNION for field extraction (Schicht A). A raw poppler `pdftotext`
    # pass recovers positioned text-layer tokens (e.g. right-aligned deadline dates
    # in subsetted no-ToUnicode fonts) that the Docling/OCR stack drops; Docling OCR
    # in turn recovers image-only values the text layer lacks. process_document unions
    # them into result["field_text"] when the text layer passes these quality gates.
    # A garbled/empty text layer is dropped (OCR-only). Thresholds calibrated on the
    # Schicht A golden set (see tasks T-A0-1/T-A0-2).
    rag_text_layer_min_chars_per_page: int = 50      # below => scan/no text layer => OCR-only
    rag_text_layer_min_space_ratio: float = 0.05     # below => no-space mojibake => drop text layer
    rag_text_layer_max_replacement_ratio: float = 0.02  # above => broken encoding => drop text layer
    rag_text_layer_min_vowel_ratio: float = 0.55     # below => garbled glyphs => drop text layer
    rag_text_layer_max_chars: int = 1_000_000        # cap raw text-layer length (OOM guard on pathological PDFs)
    # Schicht A field extractor (post_document_ingest consumer). Reads field_text,
    # extracts identifiers (deterministic) + obligations (LLM), stores as atoms.
    # Opt-in: the LLM obligation pass costs one classification call per ingest, and
    # the obligation/alert layer that consumes these facts isn't wired yet.
    schicht_a_extraction_enabled: bool = False
    # Unified "Wissen" workspace (frontend). When on, the 6 corpus nav entries
    # (knowledge/brain/review/fristen/memory/knowledge-graph) collapse into one
    # /wissen workspace and the old routes redirect in. Off => legacy flat nav.
    # Runtime flag (exposed via /api/config/features) so it flips without a rebuild.
    wissen_workspace_enabled: bool = False
    # Gates the chat command palette UI (`/`-trigger + touch button + overlay).
    # Frontend-only gate; the backend `role_hint` handling is always present
    # (no-op when absent), so flipping this needs no backend redeploy.
    command_palette_enabled: bool = False
    # Gates the chat agent-role badge (item 6): shows which agent role answered +
    # lets the user pin a role for the next turn (reuses role_hint). Frontend-only
    # gate; the backend always emits the resolved role on the done frame + persists
    # it, so flipping this needs no backend redeploy.
    role_surfacing_enabled: bool = False
    # Gates the chat message-search UI (item 3): the search field in the
    # conversations sidebar + the results list + jump-to-message. Frontend-only
    # gate; the backend search route + the messages.search_vector column are
    # always present (harmless when unused), so flipping this needs no backend
    # redeploy. See docs/design/chat-ui-modernization.md.
    message_search_enabled: bool = False
    # Chat artifacts Lane A (typed table/list/keyvalue/chart rendered as real
    # React components — zero model HTML, React's escape boundary is the security
    # story). Gates BOTH the backend `artifact` WS frame emit and the frontend
    # renderer (exposed via /api/config/features). Ships dark; flip without a
    # rebuild. See docs/design/chat-artifacts-sandbox.md §8.
    artifacts_typed_enabled: bool = False
    # Chat room-handoff affordance (item 8): a quiet inline meta line in the chat
    # thread when Media Follow moves the user's OWN playback to the room they just
    # entered ("🔊 Wiedergabe folgt nach {room}"). Gates BOTH the backend
    # `media_handoff` device-WS frame emit and the frontend indicator (exposed via
    # /api/config/features). Reuses the existing presence/Media-Follow data — no
    # new presence mechanism. Surfaces only the acting user's own location, routed
    # to the same room audience as the existing follow info push (no privacy
    # widening). Ships dark; flip without a rebuild.
    room_handoff_enabled: bool = False
    # Chat message branching (edit-and-fork, Phase 1). Gates the FORK affordances:
    # the per-message edit/regenerate actions in the frontend (exposed via
    # /api/config/features) AND whether the backend honors an inbound
    # `fork_from_message_id` on a chat turn. The conversation TREE
    # (messages.parent_message_id / conversations.active_leaf_message_id) and the
    # active-path query are ALWAYS maintained regardless of this flag — the
    # backfill makes flag-off byte-identical to pre-branching. Ships dark; flip
    # without a rebuild. See docs/design/chat-ui-modernization.md.
    chat_branching_enabled: bool = False
    # Business-instance Projects — Phase 1: a minimal Project model +
    # one KnowledgeBase per project + CRUD. Gates BOTH the backend /api/projects
    # routes (404 when off) and the frontend /projects nav (exposed via
    # /api/config/features). Off => the household instance is byte-identical.
    # See the business-instance plan §7.1.
    projects_enabled: bool = False
    # Notes (Phase 4B) — hand-authored atomic notes as a 5th atom_type
    # (circles + polymorphic RRF + /brain). Gates the /api/notes routes (404 when
    # off), the notes RRF source in polymorphic_atom_store, and the frontend
    # surface (via /api/config/features). Off => byte-identical. See
    # docs/design/notes-atom.md.
    notes_enabled: bool = False
    # Meeting transcription §2 — upload a multi-speaker recording -> diarized,
    # speaker-attributed transcript in the KB. Gates the /api/meetings routes
    # (404 when off), the meeting worker, and the frontend surface (via
    # /api/config/features). Off => both instances are byte-identical.
    # See docs/design/meeting-transcription.md.
    meeting_transcription_enabled: bool = False
    # §2 Phase 3: minutes pipeline (summary/decisions/action-items with human
    # confirm) on a completed transcript. Dark by default; needs a chat model.
    meeting_minutes_enabled: bool = False
    # faster-whisper model for meeting batch ASR ("" => reuse the STT default).
    # A larger model (e.g. large-v3-turbo) trades GPU-seconds for accuracy; set
    # from the spike results. Loaded/unloaded per job on the voice-server.
    meeting_whisper_model: str = ""
    # Hard duration ceiling (hours) enforced at upload; also derives the worker's
    # stream visibility window. >4h is a documented escalation (chunked path).
    meeting_max_duration_h: int = Field(default=4, ge=1, le=12)
    # Auto-match diarized clusters to enrolled speakers. DEFERRED/dark: the spike
    # separation gate was insufficient-data on synthetic audio, so the matcher is
    # NOT built yet — pseudonyms ("Sprecher N") + human labeling is the product.
    meeting_auto_match_enabled: bool = False
    # Keep the original audio after a completed transcript (opt-in); default is to
    # delete it after the grace window below.
    meeting_keep_audio: bool = False
    meeting_audio_grace_days: int = Field(default=7, ge=0, le=365)
    # Full-retention window (days): a meeting's retention_until is stamped at
    # upload to created_at + this, and the daily retention job then purges the
    # transcript + segments (incl. ECAPA embeddings) + audio + row past it.
    # 0 = retain forever (retention_until left NULL). Consent-gated DE workplace
    # recordings should NOT be 0.
    meeting_retention_days: int = Field(default=365, ge=0, le=3650)
    # Chat artifacts Lane B (free-form HTML/SVG in a sandboxed iframe). DEFERRED —
    # NOT wired to anything in this delivery. Placeholder so the per-lane flag
    # split (§8 Q5) exists; defaults off and requires its own security review
    # before it is ever built/enabled. Do NOT enable.
    artifacts_html_sandbox_enabled: bool = False
    # Generic output-provider registry for room media/control routing. When on,
    # room output discovery + dispatch route through the pluggable OutputProvider
    # registry (built-in renfield/HA + MCP-declared dlna/samsung/sonos via the
    # `output_provider:` stanza in mcp_servers.yaml) instead of the hardcoded
    # 3-source branches. Off => byte-identical legacy routing. See
    # docs/design/output-providers.md.
    output_providers_enabled: bool = False
    # Per-provider timeout (seconds) for the aggregated available-outputs discover
    # fan-out. A provider that exceeds it is shown DEGRADED (not dropped).
    output_provider_discover_timeout: float = 5.0
    schicht_a_extraction_model: str = ""             # empty => ollama_chat_model || ollama_model
    # Output-token cap for the obligation/universal LLM pass. The old fixed 1200
    # cap (→ OpenAI max_tokens) silently truncated rich docs' JSON → unparseable
    # → all LLM facts lost (doc 43: 1 vs 14). Output size tracks fact DENSITY, not
    # doc size, so no fixed cap is safe. 0 = no cap: let the model generate to
    # completion, bounded by the server context (verified: the densest real doc,
    # an invoice, completes at ~3.2k chars well within context). Set >0 only to
    # deliberately bound a misbehaving model. The parser also salvages a truncated
    # tail as defense-in-depth.
    schicht_a_extraction_num_predict: int = 0

    # Conversation Memory (Long-term)
    memory_enabled: bool = False                                             # Opt-in
    memory_retrieval_limit: int = Field(default=3, ge=1, le=10)              # Max memories per query
    # Cosine threshold for the chat-injection path. Dropped from 0.7 to
    # 0.5 (2026-05-26) — the 0.7 gate was tuned for short paraphrase
    # matches but suppressed natural German question queries against
    # third-person fact memories (e.g. "Was mag Jutta gerne essen?"
    # against "Jutta mag Maracujas und Ananas" embeds at ~0.55). The
    # /brain page returned 0 hits even though the memory was a direct
    # answer. 0.5 is calibrated for qwen3-embedding:4b's distribution;
    # tune downward if cross-language false negatives still appear.
    memory_retrieval_threshold: float = Field(default=0.5, ge=0.0, le=1.0)  # Cosine-similarity threshold
    memory_max_per_user: int = Field(default=500, ge=10, le=5000)           # Max active memories
    memory_context_decay_days: int = Field(default=30, ge=1, le=365)        # Days until context category expires
    memory_dedup_threshold: float = Field(default=0.9, ge=0.5, le=1.0)     # Deduplication threshold
    memory_extraction_enabled: bool = False                                  # Auto-extract memories from conversations
    memory_extraction_model: str = ""                                         # Model for extraction (default: ollama_model)
    memory_cleanup_interval: int = Field(default=3600, ge=60, le=86400)     # Cleanup interval in seconds
    memory_essential_threshold: float = Field(default=0.9, ge=0.0, le=1.0)   # Importance threshold for always-inject
    memory_contradiction_resolution: bool = False                            # LLM-based contradiction resolution
    memory_contradiction_threshold: float = Field(default=0.6, ge=0.3, le=0.89)  # Similarity range lower bound
    memory_contradiction_top_k: int = Field(default=5, ge=1, le=10)         # Max similar memories to compare
    # Mem0 v2 batched extraction (Lane B/2 of memory architecture plan)
    memory_extraction_retrieve_k: int = Field(default=5, ge=1, le=50)       # Top-K candidates for v2 extract LLM prompt
    memory_extraction_v2_shadow: bool = False                                # Phase A: run v2 in shadow mode alongside v1
    memory_extraction_v2_authoritative: bool = False                         # Phase B: v2 is primary; v1 becomes legacy fallback
    # Lane D — separate retrieval threshold for the v2 extract pipeline.
    # Chat retrieval uses ``memory_retrieval_threshold`` (currently 0.5
    # after the 2026-05-26 brain-quality tune, dropped from 0.7 because
    # natural German question queries embed below 0.7 against fact
    # memories). Extract is a different surface: high recall is what
    # matters, and the LLM plus the drift check together replace the
    # score gate. Defaulting to 0.0 means the LLM sees top-K candidates
    # regardless of similarity.
    #
    # Empirical basis: the 0.7 default produced cross_session_update
    # detection of 0.143; setting this to 0.0 raised it to 0.929 with no
    # regression on any of the four locked baselines. See
    # `docs/lane-d-extract-retrieval-threshold.md` for the full A/B.
    #
    # If you want to experiment with intermediate values, set this via
    # env var (MEMORY_EXTRACT_RETRIEVAL_THRESHOLD). 0.0 is the production
    # default; do not raise above 0.5 without re-running the corpus.
    memory_extract_retrieval_threshold: float = Field(default=0.0, ge=0.0, le=1.0)
    # Lane C two-stage retrieval with recency-aware rerank. Opt-in via
    # `ranker="recency_aware"` on MemoryRetrieval.retrieve(). v2 callers
    # use it by default; web chat / retrieve_for_prompt still on the
    # legacy single-stage ranker until eval data justifies a flip.
    memory_retrieval_recall_k: int = Field(default=50, ge=10, le=500)       # Stage-1 HNSW recall window
    memory_retrieval_recency_weight: float = Field(default=0.2, ge=0.0, le=1.0)  # 0 = ignore recency, 1 = heavy weight
    memory_retrieval_recency_half_life_days: int = Field(default=30, ge=1, le=365)  # Decay half-life for last_accessed_at
    memory_episodic_enabled: bool = False                                         # Opt-in for episodic memory
    memory_episodic_max_per_user: int = Field(default=100, ge=10, le=1000)       # Max episodes per user
    memory_episodic_decay_days: int = Field(default=90, ge=7, le=365)            # Days until episodes deactivate
    memory_episodic_summarize_threshold: int = Field(default=50, ge=10, le=200)  # Episode count before summarization
    memory_relevance_filter_enabled: bool = True                                  # Skip transactional queries
    memory_retrieval_budget_chars: int = Field(default=2000, ge=500, le=10000)   # Max chars for memory prompt block

    # Procedural Skills (self-learning Phase 1)
    # The agent learns multi-step tool-call recipes from complex turns and
    # reuses them on similar future requests. See docs/SELF_LEARNING.md.
    skills_enabled: bool = False                                            # Opt-in for the whole feature
    skill_extract_enabled: bool = True                                       # Auto-extract from agent turns
    skill_extract_min_tool_calls: int = Field(default=3, ge=1, le=20)        # Threshold for "complex" turn
    skill_extract_model: str = ""                                            # Empty = use ollama_chat_model
    skill_inject_enabled: bool = True                                        # Inject matching skills into agent prompt
    skill_inject_top_k: int = Field(default=3, ge=1, le=10)                  # Max skills injected per turn
    skill_inject_similarity_threshold: float = Field(default=0.75, ge=0.0, le=1.0)  # Min cosine sim
    skill_seed_load_on_boot: bool = True                                     # Load src/backend/seed_skills/*.md at boot
    skill_seed_directory: str = "seed_skills"                                # Relative to backend root
    skill_auto_demote_threshold: int = Field(default=5, ge=1, le=100)        # Failures before auto-deactivate
    skill_auto_demote_success_rate: float = Field(default=0.10, ge=0.0, le=1.0)  # Success rate below this triggers demote

    # Trajectory capture (self-learning Phase 2)
    # Captures full agent-turn traces as JSONL-exportable training data
    # for downstream LoRA fine-tuning. See docs/SELF_LEARNING.md.
    trajectory_capture_enabled: bool = False                                  # Master switch — implicitly requires skills_enabled
    trajectory_capture_outcomes: str = "success,tool_fail"                    # Comma-separated: outcomes to capture
    trajectory_retention_days: int = Field(default=30, ge=1, le=3650)        # Auto-delete after N days
    trajectory_cleanup_interval: int = Field(default=86400, ge=300, le=604800)  # Cleanup job interval (seconds)
    trajectory_max_per_user: int = Field(default=10000, ge=100, le=100000)   # Soft cap, oldest dropped first
    # COUNT-then-DELETE on every save() costs N round-trips for N inserts;
    # only the last few near the cap actually matter. Run the cap check on
    # every Nth save (probabilistic) — drift up to N rows over the cap is
    # harmless because the cleanup scheduler also prunes by retention.
    trajectory_cap_check_every: int = Field(default=50, ge=1, le=1000)
    trajectory_redact_pii: bool = False                                       # Phase 4: scrub PII into redacted_payload

    # Tool outcome tracking (self-learning Phase 3)
    # Counts every tool_result step in the agent loop; surfaces warnings
    # in the agent prompt when a tool's per-user success rate drops below
    # the floor. Implicitly requires skills_enabled (rides on the same
    # post-turn fire-and-forget task).
    tool_health_tracking_enabled: bool = False                                # Master switch
    tool_health_warn_enabled: bool = True                                     # Inject warnings into agent prompt
    tool_health_warn_min_uses: int = Field(default=5, ge=1, le=100)           # Min total calls before warning
    tool_health_warn_success_rate: float = Field(default=0.5, ge=0.0, le=1.0) # Warn if below
    tool_health_warn_top_k: int = Field(default=3, ge=1, le=10)               # Max warnings per prompt

    # Skill curator (self-learning Phase 4)
    # Periodically dedupes and archives skills the agent has accumulated.
    # Runs as a background scheduler when enabled; can also be triggered
    # manually via /api/skills/curator/run (admin-only).
    skill_curator_enabled: bool = False                                       # Master switch
    skill_curator_interval: int = Field(default=86400, ge=300, le=604800)     # Seconds between runs (default 1d)
    skill_curator_duplicate_threshold: float = Field(default=0.92, ge=0.5, le=1.0)  # Cosine sim to consider as duplicates
    skill_curator_stale_days: int = Field(default=90, ge=7, le=365)           # Archive after N days unused
    skill_curator_stale_success_rate: float = Field(default=0.3, ge=0.0, le=1.0)    # Archive if rate below this AND stale
    skill_curator_min_uses_to_consider_stale: int = Field(default=3, ge=1, le=100)  # Avoid archiving rarely-tested skills
    skill_curator_max_merges_per_run: int = Field(default=20, ge=1, le=200)   # Safety cap

    # KG entity reconciler (Structured Memory Phase 1, T5). Periodic per-user
    # self-join over kg_entities embeddings: same-tier high-confidence dupes are
    # auto-merged; cross-tier / gray-zone dupes become kg_merge_proposals for
    # owner review (D3). Opt-in.
    kg_reconciler_enabled: bool = False                                          # Master switch
    kg_reconciler_interval: int = Field(default=86400, ge=300, le=604800)        # Seconds between runs (default 1d)
    kg_reconciler_candidate_threshold: float = Field(default=0.85, ge=0.5, le=1.0)   # Cosine to consider a pair at all
    kg_reconciler_auto_merge_threshold: float = Field(default=0.95, ge=0.5, le=1.0)  # Same-tier auto-merge bar (>= candidate)
    kg_reconciler_max_per_run: int = Field(default=50, ge=1, le=500)             # Safety cap per user per run
    kg_reconciler_embed_backfill_per_run: int = Field(default=50, ge=0, le=500)  # Re-embed up to N null-embedding entities per pass (#6); 0 disables
    # KG conflation tripwire (read-only early warning, services/kg_conflation_monitor.py).
    # Logs + gauges DISTINCT-name same-type pairs embedding >= threshold (a forming
    # generic-centroid magnet); never mutates. Expected count: 0.
    kg_conflation_monitor_enabled: bool = False                                  # Master switch (opt-in scheduled scan)
    kg_conflation_monitor_interval: int = Field(default=86400, ge=300, le=604800)    # Seconds between scans (default 1d)
    kg_conflation_monitor_threshold: float = Field(default=0.85, ge=0.5, le=1.0)     # Cosine at/above which a distinct-name same-type pair is flagged
    kg_conflation_monitor_max_pairs: int = Field(default=100, ge=1, le=1000)         # Cap on pairs reported per user per scan
    memory_kg_bridge_enabled: bool = False                                       # Phase 3: link memory subjects to canonical KG entities (save-time + entity-augmented retrieval). Opt-in.
    memory_subsume_to_kg: bool = False                                           # Phase 3-subsume: decomposable facts (category=fact + subject) live in the KG only; skip the flat duplicate. Opt-in, aggressive.
    memory_subsume_require_kg_relation: bool = True                              # Phase 3-subsume recall-loss REDUCER (subject-level proxy, NOT a per-fact guarantee): only drop the flat fact when the subject's person-entity already has >=1 relation — protects never-before-related subjects. A state/feeling fact about an already-related person is still subsumed-and-lost; per-fact fix is a TODOS follow-up. Off = legacy unguarded subsume. Does NOT make subsume multi-user-safe.
    memory_retrieval_subject_union_limit: int = Field(default=5, ge=1, le=50)    # Phase 3c: max deterministic subject-linked memories merged into retrieval per turn

    # Skill draft-gate shadow log (v2.10 admin console rollout). When True,
    # SkillService.find_similar runs a parallel "would-have-injected" query
    # that relaxes the status='approved' filter, so we can measure how much
    # recall the human-in-the-loop gate costs. Disable after the rollout
    # window — the table can grow significantly under load.
    skill_shadow_log_enabled: bool = True
    skill_shadow_log_top_k: int = Field(default=10, ge=1, le=50)              # Cap shadow rows per query
    skill_shadow_log_retention_days: int = Field(default=30, ge=1, le=365)    # Auto-delete shadow rows older than N days
    skill_shadow_log_cleanup_interval: int = Field(default=86400, ge=300, le=604800)  # Cleanup tick (seconds)

    # Knowledge Graph (Entity-Relation triples from conversations)
    knowledge_graph_enabled: bool = False                                        # Opt-in
    kg_extraction_model: str = ""                                                # Empty = use default model
    kg_similarity_threshold: float = Field(default=0.85, ge=0.5, le=1.0)        # Entity dedup threshold (0.85 merges OCR variants)
    kg_retrieval_threshold: float = Field(default=0.70, ge=0.0, le=1.0)         # Context retrieval threshold
    kg_max_entities_per_user: int = Field(default=5000, ge=10, le=50000)         # Max active entities per user
    kg_max_context_triples: int = Field(default=15, ge=1, le=50)                 # Max triples injected into prompt
    # Graph-expansion retrieval (Phase 4, post-RRF) — opt-in, off = byte-identical
    graph_expansion_enabled: bool = False                                       # Walk 1-2 hops from fused kg_node pivots (post-RRF in PolymorphicAtomStore)
    graph_expansion_max_pivots: int = Field(default=8, ge=1, le=50)             # Max fused kg_node pivots to expand from
    graph_expansion_max_hops: int = Field(default=2, ge=1, le=3)               # Traversal depth
    graph_expansion_max_expanded: int = Field(default=15, ge=1, le=100)        # Cap on added neighbour atoms (hub-flood guard)

    # Document Upload
    upload_dir: str = "/app/data/uploads"
    max_file_size_mb: int = Field(default=50, ge=1, le=500)
    allowed_extensions: str = "pdf,docx,doc,txt,md,html,pptx,xlsx,png,jpg,jpeg"  # Comma-separated
    chat_upload_max_context_chars: int = Field(default=50000, ge=1000, le=200000)
    chat_upload_auto_index: bool = True
    chat_upload_default_kb_name: str = "Chat Uploads"
    chat_upload_retention_days: int = Field(default=30, ge=1, le=365)
    chat_upload_cleanup_enabled: bool = False
    chat_upload_email_account: str = "primary"

    # Folder-ingest (watch-folder auto-ingest via renfield-mcp-filesystem).
    # The dedicated Filesystem MCP pushes settled files to
    # POST /api/folder-ingest/document; the backend never mounts the shares.
    # The Bearer token lives in SystemSetting (revocable), not here. Owner/tier
    # (D4) and the Paperless leg toggle are consumed in later tasks (T5/T6);
    # the push route (T3) uses enabled + kb_name + target_user.
    folder_ingest_enabled: bool = False
    folder_ingest_kb_name: str = "Eingang"  # target KB; auto-created on first push
    folder_ingest_target_user: str = ""  # owner username/id; empty → admin/first user
    folder_ingest_default_tier: int = Field(default=0, ge=0, le=4)  # circle tier at create
    folder_ingest_to_paperless: bool = True
    folder_ingest_notify_on_filed: bool = True

    # Async Paperless reconciler (Design Z): folder/email-ingest stamp
    # paperless_state='pending' and this periodic reconciler files them out of
    # band (services/paperless_reconciler.py), so the push never awaits the
    # external Paperless round-trip on a pooled DB connection. Runs whenever
    # folder- OR email-ingest→Paperless is on. Batch bounds per-tick work so a
    # large first-run backlog drains across ticks.
    paperless_reconciler_interval: int = 120  # seconds between reconciler ticks
    paperless_reconciler_batch: int = 25  # pending docs re-enqueued per tick
    # Grace before a still-pending completed doc is re-enqueued for a worker
    # refile — keeps the scan from racing the initial fire-and-forget filing hook
    # (which runs after the doc is marked completed). Only docs completed longer
    # ago than this are treated as genuine stragglers.
    paperless_reconciler_refile_grace_seconds: int = 300
    # Per-doc refile lease (Redis SET NX EX). ``processed_at`` is fixed at
    # completion, so a still-pending straggler re-selects every tick; without a
    # lease the SAME doc is re-enqueued each interval until it settles (a slow
    # doc then re-runs a full Docling pass per tick). One lease lets a single
    # refile attempt run; it expires so a FAILED attempt retries, and a success
    # drops the row out of the pending select anyway — no explicit release. Keep
    # it well above one refile's worst-case queue-wait + Docling time, below the
    # tolerable retry cadence for a genuinely stuck doc.
    paperless_reconciler_refile_lease_seconds: int = 900

    # Document-worker stale-task recovery. reclaim_stale() re-adopts entries a
    # dead consumer left un-ACKed in the Redis PEL. It used to run ONLY at worker
    # startup, so an entry orphaned WHILE the worker keeps running (an OOMKill
    # mid-OCR where the pod recovers, or a transient-error return) was invisible
    # to the running consumer forever (doc 241, 2026-07-02). Run it periodically
    # in the steady-state loop too. min-idle stays visibility_ms (10min) so it
    # never steals an entry a live worker is mid-processing.
    worker_reclaim_interval_seconds: int = 120
    # OOM-poison guard: a doc that OOM-kills the worker every attempt would, with
    # periodic reclaim, be re-adopted and re-OOM in a loop (crashloop the queue).
    # After a task has been delivered more than this many times it is quarantined
    # (the doc is marked failed and the entry ACKed) instead of re-processed.
    worker_max_deliveries: int = 3
    # Transient-retry cap for the MEETING worker: a voice-server 5xx / unreachable
    # is left in the PEL for reclaim (a restart/model-load DOES recover). But an
    # unbounded transient loop — e.g. a CUDA-OOM that surfaces as a 500 the OOM
    # markers miss, or a pod-killing OOM seen as "unreachable" — would re-burn the
    # shared GPU every reclaim window forever (crash_count never trips the poison
    # guard because transient leaves are excluded from it). After this many
    # transient leaves the meeting is quarantined (marked failed) so it can't
    # thrash indefinitely. Generous vs worker_max_deliveries: a legit voice-server
    # outage (rolling restart / redeploy) should get several reclaim windows
    # (~120 s each) to recover before we give up on the recording.
    meeting_worker_max_transient_retries: int = 10

    # Email-mailbox auto-ingest (Phase 1; ships dark). The dedicated
    # renfield-mcp-email-ingest watcher PUSHES attachments to
    # POST /api/email-ingest/document; the backend owns the SPHERE routing here
    # (server-authoritative — the watcher only sends a mailbox_id). Each entry:
    #   {"id": "<stable mailbox id>", "owner": "<username|id|''>",
    #    "tier": <0-4>, "kb": "<target KB name>"}
    # Stored as a JSON STRING (EMAIL_INGEST_MAILBOXES_JSON, or the
    # renfield-mcp-config ConfigMap) and parsed by the email_ingest_mailboxes
    # property — graceful: a malformed value falls back to [] rather than
    # crashing the backend at import (a raw list[dict] env would). An unknown
    # mailbox_id on a push → failed (route).
    email_ingest_enabled: bool = False
    email_ingest_to_paperless: bool = True
    email_ingest_mailboxes_json: str = ""
    # Paperless cold-start confirm ramp: the first N archives show a metadata
    # confirm; after N the system trusts itself and archives silently. 0 =
    # never confirm (always silent). Tunable without a code change.
    paperless_cold_start_confirm_n: int = Field(default=3, ge=0, le=100)

    # Federation (v2 — F5a depth + cycle detection)
    # Max number of federation hops a query can traverse before
    # responders reject with "too deep". 1 = direct asker→responder
    # only (no transitive). Default 3 matches the household assumption
    # of at most A→B→C→D chains; larger values widen the reach but
    # also the latency + trust surface.
    federation_max_depth: int = Field(default=3, ge=1, le=10)

    # Federation (F5b — rate limits).
    # Asker-side: max initiate calls per minute per paired peer. Throttles
    # how fast THIS Renfield can hammer a single remote peer. At 60/min
    # (default) a reasonable upper bound is 1 query/sec sustained.
    federation_asker_rate_per_minute: int = Field(default=60, ge=1, le=600)
    # Responder-side: max initiate calls per minute from any one asker
    # pubkey. Defense against a compromised-or-rogue paired peer flooding
    # us. 30/min (default) is 0.5 QPS sustained — generous for household
    # use, tight enough that abuse is obvious.
    federation_responder_rate_per_minute: int = Field(default=30, ge=1, le=600)

    # Federation (F5c — Redis-backed pending requests).
    # Default off: single-backend deploys (the Renfield default) keep
    # the in-memory store with no behavioral change. Flip on for
    # multi-worker deploys so a poll landing on a different worker
    # than the initiate can still read state, AND so nonce dedup works
    # across workers (replay defense).
    federation_pending_use_redis: bool = False

    # Federation (F-ID-1 — person-scoped identity links).
    # Default off (DARK): the responder ignores any `querier_ref` in a query
    # envelope and always serves the peer-scoped public/guest fallback, and the
    # asker never attaches a `querier_ref` — byte-identical to pre-F-ID-1. When
    # on, a query whose `querier_ref` matches a `federation_user_links` row is
    # served AS the mapped local user (full circle reach). Design:
    # docs/design/federation-identity-mapping.md.
    federation_identity_links_enabled: bool = False

    # This instance's OWN reachable URL, advertised to a peer during pairing so
    # the peer's PeerUser.transport_config gets a usable endpoint (without it, a
    # UI pairing yields transport_config.endpoints=[] → _select_endpoint→None →
    # "Peer has no usable transport endpoint" — why federation has been
    # population-of-1). The pairing offer/accept already SIGN + PERSIST endpoints
    # (#408/#421); this setting just supplies the value a caller didn't override.
    # Same-cluster peers use the internal svc DNS, e.g.
    #   http://backend.renfield.svc.cluster.local:8000
    # A per-pairing UI field can override it. Empty = advertise nothing (legacy).
    federation_advertised_url: str = ""

    # Federation — where this instance's Ed25519 identity private key lives.
    # MUST point at PERSISTENT storage in production: the default /app/secrets is
    # ephemeral container FS, so the key regenerates on every restart and the
    # pubkey changes → every existing pairing breaks on the next deploy. Point it
    # at a mounted secret (operator-provisioned) so the identity — and thus every
    # peer pairing — survives restarts. See docs/design/federation-identity-mapping.md.
    federation_identity_key_path: str = "/app/secrets/federation_identity_key"

    # The READ-ONLY mounted persisted key location (the `federation-identity`
    # Secret, mounted as a whole dir — NOT subPath). When this file exists it is
    # PREFERRED over the writable path above (a provisioned instance loads the
    # durable key); when absent (secret not provisioned yet) the loader falls
    # through to the writable path and generates ephemerally. Empty = no mount.
    federation_identity_persisted_key_path: str = ""

    # When set, the backend FAILS TO BOOT if the federation identity key was
    # generated fresh at startup instead of loaded from a persisted mount — i.e.
    # the operator forgot to provision the `federation-identity` secret. Default
    # off so non-federating instances are unaffected; set true on any instance
    # that actually pairs, so a misprovision is loud (boot failure) instead of
    # silent (broken pairings one deploy later). See enforce_persistent_identity().
    federation_require_persistent_identity: bool = False

    # Monitoring
    metrics_enabled: bool = False  # Enable Prometheus /metrics endpoint

    # Logging
    log_level: str = "INFO"

    # Security
    secret_key: SecretStr = "changeme-in-production-use-strong-random-key"
    trusted_proxies: str = ""  # Comma-separated CIDRs, e.g. "172.18.0.0/16,127.0.0.1"

    # Jellyfin / Paperless / Paperless Audit settings moved to
    # ha_glue/utils/config.py.

    # Email MCP
    email_mcp_enabled: bool = False
    mail_primary_password: SecretStr | None = None

    # SearXNG
    searxng_api_url: str | None = None
    searxng_instances: str | None = None

    # n8n MCP
    n8n_base_url: str | None = None
    n8n_api_key: SecretStr | None = None
    n8n_mcp_enabled: bool = False

    # Radio (TuneIn) / HA MCP settings moved to ha_glue/utils/config.py.

    # === Plugin / Extension System ===
    plugin_module: str = ""  # e.g. "renfield_twin.hooks:register"
    # Comma-separated list of additional "module:callable" startup plugins,
    # e.g. "renfield_twin.hooks:register,other_pkg.mod:init". Loaded in order
    # after plugin_module; duplicates across both are deduped.
    plugin_modules: str = ""

    # Optional binding of a startup plugin to the MCP server it backs, so a
    # failed plugin load marks that server "degraded" on the kiosk (connected
    # but not fully functional). Comma-separated "plugin_module_prefix:server",
    # e.g. an adapter plugin backing its sidecar MCP server. The plugin naming
    # stays out of this generic default (empty) — the deployment that ships the
    # plugin sets the binding. Matched by spec.startswith(prefix).
    plugin_mcp_bindings: str = ""

    # === Deployment environment ===
    # Deployment posture marker: "development" (default) | "dev" | "test" |
    # "staging" | "production" | "prod". Read by the security validators below —
    # a real-deployment value (production/prod/staging) ARMS the insecure-JWT-key
    # boot guard even when auth is off (#692) and gates the changeme-default
    # warning. Previously read only via os.getenv at validation time; now a
    # tracked Settings field so it is introspectable + documented and can be set
    # in the ConfigMap alongside the other posture keys (#697). Env: RENFIELD_ENV.
    renfield_env: str = "development"

    # === Authentication ===
    # Set to True to enable authentication (default: False for development)
    auth_enabled: bool = False

    # JWT Token settings
    access_token_expire_minutes: int = 60 * 24  # 24 hours
    refresh_token_expire_days: int = 30

    # SSO token hand-off hardening — replaces the URL-fragment token hand-off
    # (implicit flow) with a one-time, single-use, PKCE-bound code exchanged over
    # POST (a token never rides in a URL). Gates POST /api/auth/sso/exchange
    # (404 when off) and the issue helper. Dark by default; the legacy fragment
    # path stays until every emitter emits ?code=. See
    # docs/design/sso-token-handoff-hardening.md.
    sso_handoff_enabled: bool = False
    sso_handoff_ttl_seconds: int = 60  # single-use code lifetime

    # Password policy
    password_min_length: int = 8

    # Registration settings
    allow_registration: bool = True  # Allow self-registration
    require_email_verification: bool = False  # Not implemented yet

    # === Pluggable auth provider registry (ebongard/renfield#591) ===
    # Per-provider credential walk timeout. A provider exceeding this is
    # skipped (fail-open) — see auth/registry.py.
    auth_provider_timeout_seconds: float = 10.0

    # --- LDAP credential provider (authn only; group→role authz is a
    #     separate future layer). Default off → DB-only behavior unchanged. ---
    ldap_auth_enabled: bool = False
    ldap_url: str = ""  # ldaps://host:636 or ldap://host:389
    ldap_bind_dn: str = ""  # service account DN for the user search
    ldap_bind_password: SecretStr = ""
    ldap_auth_user_base_dn: str = ""  # subtree to search for the user
    ldap_auth_user_filter: str = "(uid={username})"  # {username} substituted
    ldap_connect_timeout: int = 5
    ldap_receive_timeout: int = 10

    # --- Social redirect providers. All ship enabled=False; enabling is a
    #     config-only change (no redeploy). Off the credential critical path. ---
    oauth_google_enabled: bool = False
    oauth_google_client_id: str = ""
    oauth_google_client_secret: SecretStr = ""
    oauth_google_redirect_uri: str = ""

    oauth_github_enabled: bool = False
    oauth_github_client_id: str = ""
    oauth_github_client_secret: SecretStr = ""
    oauth_github_redirect_uri: str = ""

    oauth_apple_enabled: bool = False
    oauth_apple_client_id: str = ""  # Services ID
    oauth_apple_team_id: str = ""
    oauth_apple_key_id: str = ""
    oauth_apple_private_key: SecretStr = ""
    oauth_apple_redirect_uri: str = ""

    # Voice authentication
    voice_auth_enabled: bool = False
    voice_auth_min_confidence: float = 0.7

    # Default admin credentials (only used on first startup)
    default_admin_username: str = "admin"
    default_admin_password: SecretStr = "changeme"  # MUST be changed in production!

    # CORS
    cors_origins: str = "*"  # Comma-separated list or "*" for development

    # WebSocket Security
    ws_auth_enabled: bool = False  # Enable WebSocket authentication (set True in production)
    ws_token_expire_minutes: int = 60  # WebSocket token expiration

    # Security (review H1): comma-separated allowlist of satellite_ids permitted
    # to receive per-person BLE IRKs (which permanently de-anonymize a resident's
    # rotating BLE address — a location-tracking key). When non-empty, IRKs are
    # pushed ONLY to listed satellites; when empty (default) the push is ungated
    # for backward compatibility but logs a loud one-shot warning per satellite.
    # NOTE: this is a stop-gap — the full fix is a per-satellite enrollment
    # credential so a rogue LAN device can't register as a satellite at all.
    satellite_irk_allowlist: str = ""

    # Security (review H1, full fix): per-satellite enrollment credential. When
    # enabled, a satellite must present its enrollment PSK in the register frame
    # (verified constant-time against the bcrypt hash in the `satellites` table).
    # Effective-mode state machine (see docs/private/security/satellite-trust-design.md):
    #   - enabled=False (default): legacy — no PSK checks, byte-identical behavior.
    #   - enabled=True, not enforcing (PERMISSIVE/soak): a presented PSK is
    #     verified (wrong/unknown/revoked → reject); no PSK → allowed but logged
    #     unenrolled; IRKs pushed ONLY to verified-enrolled satellites.
    #   - ENFORCING (auto-flip latched): no valid PSK → reject.
    satellite_enrollment_enabled: bool = False
    # Auto-flip PERMISSIVE→ENFORCING once EVERY enrolled satellite row has
    # authenticated at least once (not just currently-connected ones), then
    # latch it persistently. Default off until the fleet is fully enrolled.
    satellite_enrollment_autoflip_enabled: bool = False

    # C1 binary Opus transport for satellite audio (docs/design/
    # voice-identity-wakeword-verification.md §4, decision D6). Dark by
    # default: when off, a satellite that requests audio_codec=opus at
    # register is answered "pcm" and keeps the legacy base64-PCM JSON path —
    # byte-identical fleet behavior. When on (and opuslib/libopus is present
    # in the image), an opus-capable satellite streams binary frames that the
    # backend edge-decodes to PCM before the existing buffer, so STT/speaker
    # paths and the voice-server API are untouched.
    satellite_opus_enabled: bool = False

    # WebSocket Rate Limiting
    # Note: Audio streaming sends ~12.5 chunks/second, so limits must accommodate this
    ws_rate_limit_enabled: bool = True
    ws_rate_limit_per_second: int = 50  # Allows audio streaming + overhead
    ws_rate_limit_per_minute: int = 1000  # Allows longer recordings and multiple interactions

    # REST API Rate Limiting
    api_rate_limit_enabled: bool = True
    api_rate_limit_default: str = "100/minute"  # Default rate limit for most endpoints
    api_rate_limit_auth: str = "10/minute"      # Stricter limit for auth endpoints (login, register)
    api_rate_limit_voice: str = "30/minute"     # Voice endpoints (STT, TTS)
    api_rate_limit_chat: str = "60/minute"      # Chat endpoints
    api_rate_limit_admin: str = "200/minute"    # Admin endpoints (higher limit)
    # Folder/email-ingest PUSH endpoints. These are hit by the trusted,
    # Bearer-token-authed MCP watchers (one IP), whose own push-concurrency
    # semaphore is the intended throughput bound — not this per-IP limit, which
    # exists for untrusted user-API abuse. Since the Paperless leg was decoupled
    # (Design Z) the push returns in ms, so a watch-folder backlog now bursts far
    # above the 100/min default and 429s (stalling the drain). This generous
    # ceiling lets the MCP semaphore govern legit throughput while still capping a
    # leaked-token flood (the DB pool is the harder backstop). Env-tunable.
    api_rate_limit_ingest: str = "1200/minute"
    # Storage backend for the REST rate limiter (slowapi/limits URI). Default
    # "memory://" = per-pod counters (backwards-compatible; a multi-replica
    # deploy under-counts because each pod limits independently). Set to the
    # Redis URL (e.g. ${REDIS_URL}) for shared per-CLUSTER limiting so the auth
    # limit holds across replicas — required once the multi-user clone runs >1
    # backend pod (#693). limits accepts "redis://host:port/db".
    api_rate_limit_storage_uri: str = "memory://"

    # Account lockout — throttle credential-stuffing beyond the per-IP rate
    # limit by locking a USERNAME after repeated failures (#693). Keyed on the
    # normalized username (not IP), so it survives an attacker rotating IPs; the
    # per-IP api_rate_limit_auth still caps request volume. Bounded duration +
    # env-disable keep the username-targeted-DoS surface small (an attacker who
    # knows a username can lock that user out for at most the duration). Backed
    # by Redis (fail-OPEN on outage — a Redis blip must not lock out the whole
    # household). Controlled solely by this flag; it is harmless-but-dormant in
    # the auth-off posture (nobody logs in), so it is not additionally gated on
    # auth_enabled.
    login_lockout_enabled: bool = True
    login_lockout_max_attempts: int = 5        # failures within the window → lock
    login_lockout_window_seconds: int = 900    # 15 min rolling failure window
    login_lockout_duration_seconds: int = 900  # 15 min lock once tripped

    # WebSocket Connection Limits
    ws_max_connections_per_ip: int = 10
    ws_max_message_size: int = 1_000_000  # 1MB max message size
    ws_max_audio_buffer_size: int = 10_000_000  # 10MB max audio buffer per session

    # WebSocket Protocol
    ws_protocol_version: str = "1.0"

    # Device/Session Timeouts
    device_session_timeout: float = 30.0  # Max voice session duration in seconds
    device_heartbeat_timeout: float = 60.0  # Disconnect after no heartbeat for this duration

    # HA / Frigate integration timeouts moved to ha_glue/utils/config.py.
    n8n_timeout: float = Field(default=30.0, ge=1.0, le=300.0)

    # Agent LLM Defaults (fallback when prompt_manager has no config)
    agent_default_temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    agent_default_num_predict: int = Field(default=2048, ge=64, le=32768)

    # Circuit Breaker
    cb_failure_threshold: int = Field(default=3, ge=1, le=50)
    cb_llm_recovery_timeout: float = Field(default=30.0, ge=1.0, le=600.0)
    cb_agent_recovery_timeout: float = Field(default=60.0, ge=1.0, le=600.0)

    # Cache TTLs (seconds) — ha_cache_ttl and satellite_package_cache_ttl
    # moved to ha_glue/utils/config.py.
    intent_feedback_cache_ttl: int = Field(default=300, ge=10, le=86400)
    # Cosine-similarity bars for past-correction matching. Two intentionally
    # different bars: the general bar (0.75) for surfacing similar past
    # corrections, and the stricter complexity-routing bar (0.80) for the
    # binary "is this query simple or complex?" decision where we want fewer
    # false positives. Both configurable so an operator can tune recall vs
    # precision per environment without a code change.
    intent_feedback_similarity_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    intent_feedback_complexity_threshold: float = Field(default=0.80, ge=0.0, le=1.0)

    # === Proactive Notifications ===
    proactive_enabled: bool = False                    # Master-Switch (opt-in)
    proactive_suppression_window: int = 60             # Dedup-Fenster in Sekunden
    proactive_tts_default: bool = True                 # TTS standardmäßig an
    proactive_notification_ttl: int = 86400            # Ablauf in Sekunden (24h)

    # Phase 2: Notification Intelligence
    proactive_semantic_dedup_enabled: bool = False
    proactive_semantic_dedup_threshold: float = 0.85
    proactive_urgency_auto_enabled: bool = False
    proactive_enrichment_enabled: bool = False
    proactive_enrichment_model: str | None = None
    proactive_feedback_learning_enabled: bool = False
    proactive_feedback_similarity_threshold: float = 0.80

    # Presence Detection / Media Follow Me settings moved to
    # ha_glue/utils/config.py.

    # Notification Polling (generic MCP server polling)
    notification_poller_enabled: bool = False           # Master-Switch for MCP notification polling
    notification_poller_startup_delay: int = 30         # Delay before first poll (seconds)

    # Phase 3: Reminders
    proactive_reminders_enabled: bool = False
    proactive_reminder_check_interval: int = 15        # Sekunden

    # Obligation-deadline notifier (Schicht A). Daily idempotent scan over
    # document_facts (obligations are the scheduling source of truth) →
    # owner-targeted lead-time reminders + a (fact, milestone) notified-ledger.
    # Opt-in; delivery degrades gracefully if proactive_enabled is off.
    obligation_notifier_enabled: bool = False
    obligation_notifier_interval: int = 86400          # daily (seconds)
    obligation_notifier_overdue_grace_days: int = 30   # still fire "overdue" within this window

    # Weekly obligation digest — the safety floor under the per-milestone
    # notifier. One owner-targeted summary per ISO week of every OPEN obligation
    # (no lower date bound), so a late-extracted / very-overdue deadline the
    # notifier's grace window missed still surfaces. Opt-in; also needs
    # proactive_enabled (delivery runs through the proactive subsystem).
    obligation_digest_enabled: bool = False
    obligation_digest_interval: int = 604800           # weekly (seconds)
    obligation_digest_horizon_days: int = 30           # include upcoming within N days (overdue always included)

    # Obligation → calendar auto-push (Calendar MCP). Per-user, opt-in: only
    # users who set a calendar preference get their open obligations mirrored as
    # calendar events (create/update/delete reconciler). Needs the calendar MCP
    # (CALENDAR_ENABLED) reachable; degrades gracefully if not. Events are timed
    # at obligation_calendar_event_hour (all-day not supported by the MCP).
    obligation_calendar_sync_enabled: bool = False
    obligation_calendar_sync_interval: int = 86400     # daily (seconds)
    obligation_calendar_event_hour: int = 9            # local hour for the (timed) event
    obligation_calendar_horizon_days: int = 90         # sync obligations due within N days
    obligation_calendar_retain_past_days: int = 30     # keep past-due events this long before cleanup
    obligation_calendar_max_ops_per_run: int = 100     # cap create/update MCP calls per user per pass

    @property
    def features(self) -> dict[str, bool]:
        """Resolve feature flags: explicit override > edition preset."""
        presets = {
            "community": {"smart_home": True, "cameras": True, "satellites": True, "voice": True, "tasks": True, "knowledge": True, "knowledge_graph": True},
            "pro": {"smart_home": False, "cameras": False, "satellites": False, "voice": False, "tasks": False, "knowledge": False, "knowledge_graph": False},
        }
        defaults = presets.get(self.renfield_edition, presets["pro"])
        return {
            "smart_home": self.feature_smart_home if self.feature_smart_home is not None else defaults["smart_home"],
            "cameras": self.feature_cameras if self.feature_cameras is not None else defaults["cameras"],
            "satellites": self.feature_satellites if self.feature_satellites is not None else defaults["satellites"],
            "voice": self.feature_voice if self.feature_voice is not None else defaults["voice"],
            "tasks": getattr(self, 'feature_tasks', None) if getattr(self, 'feature_tasks', None) is not None else defaults.get("tasks", True),
            "knowledge": getattr(self, 'feature_knowledge', None) if getattr(self, 'feature_knowledge', None) is not None else defaults.get("knowledge", True),
            "knowledge_graph": getattr(self, 'feature_knowledge_graph', None) if getattr(self, 'feature_knowledge_graph', None) is not None else defaults.get("knowledge_graph", True),
        }

    @property
    def allowed_extensions_list(self) -> list[str]:
        """Gibt allowed_extensions als Liste zurück"""
        return [ext.strip().lower() for ext in self.allowed_extensions.split(",")]

    @property
    def email_ingest_mailboxes(self) -> list[dict]:
        """Parsed email-ingest routing table from ``email_ingest_mailboxes_json``.
        Graceful: a malformed JSON value logs + falls back to ``[]`` so a config
        typo can't crash the backend at import (a typed ``list[dict]`` env would).
        """
        raw = (self.email_ingest_mailboxes_json or "").strip()
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.warning("email_ingest_mailboxes_json is not valid JSON; using []")
            return []
        if not isinstance(data, list):
            logger.warning("email_ingest_mailboxes_json is not a JSON list; using []")
            return []
        return [e for e in data if isinstance(e, dict)]

    @property
    def supported_languages_list(self) -> list[str]:
        """Returns supported_languages as a list"""
        return [lang.strip().lower() for lang in self.supported_languages.split(",")]

    @property
    def piper_voice_map(self) -> dict[str, str]:
        """
        Returns piper_voices as a dictionary mapping language code to voice name.
        Example: {"de": "de_DE-thorsten-high", "en": "en_US-amy-medium"}
        """
        voice_map = {}
        for pair in self.piper_voices.split(","):
            if ":" in pair:
                lang, voice = pair.strip().split(":", 1)
                voice_map[lang.strip().lower()] = voice.strip()
        # Ensure default language has a voice (fallback to piper_default_voice)
        if self.default_language not in voice_map:
            voice_map[self.default_language] = self.piper_default_voice
        return voice_map

    @model_validator(mode="after")
    def warn_deprecated_extract_mode_env(self) -> "Settings":
        """Surface stale `MEMORY_EXTRACT_RETRIEVAL_MODE` env vars.

        The mode enum (`threshold_filter`/`no_filter`/`score_aware`) was an
        experiment-only knob introduced and removed within the 2026-05-15
        Lane D work. Silent-ignore is the Pydantic Settings default for
        unknown env vars; operators who followed internal notes and set
        the env var would think the knob is still wired. Bark loudly so
        they switch to `MEMORY_EXTRACT_RETRIEVAL_THRESHOLD`.
        """
        if os.getenv("MEMORY_EXTRACT_RETRIEVAL_MODE"):
            logger.warning(
                "MEMORY_EXTRACT_RETRIEVAL_MODE is set but no longer recognised "
                "(removed in PR #583). Use MEMORY_EXTRACT_RETRIEVAL_THRESHOLD "
                "(float 0.0-1.0; production default 0.0) instead. See "
                "docs/lane-d-extract-retrieval-threshold.md."
            )
        return self

    @model_validator(mode="after")
    def assemble_database_url(self) -> "Settings":
        """Baut DATABASE_URL aus Einzelteilen zusammen, falls nicht explizit gesetzt."""
        if self.database_url is None:
            self.database_url = (
                f"postgresql://{self.postgres_user}:{self.postgres_password.get_secret_value()}"
                f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
            )
        return self

    @model_validator(mode="after")
    def warn_on_changeme_defaults(self) -> "Settings":
        """W13 — Emit a loud WARNING for any secret/password field still set
        to its placeholder default.

        For each field name in `_CHANGEME_FIELDS`, compare the current
        resolved value to the field's class-level default (read live from
        `Settings.model_fields[name].default`). If they match, the env
        override didn't take effect — i.e. the placeholder is in use.

        Gated to non-development environments so dev/test runs aren't
        spammed with a warning that exists to catch production-deploy
        regressions. Trigger condition: `RENFIELD_ENV` is set to anything
        other than the default `"development"` (e.g. `"production"`,
        `"staging"`, or `"prod"`). Tests can opt in to the warning path
        by setting RENFIELD_ENV explicitly.
        """
        env = self.renfield_env.lower()
        if env in {"development", "dev", "test"}:
            return self

        offenders: list[str] = []
        for field_name in _CHANGEME_FIELDS:
            field_info = type(self).model_fields.get(field_name)
            if field_info is None:
                continue  # field renamed/removed — silent skip is intentional
            placeholder_default = field_info.default
            if isinstance(placeholder_default, SecretStr):
                placeholder_default = placeholder_default.get_secret_value()
            value = getattr(self, field_name, None)
            if value is None:
                continue
            current = value.get_secret_value() if isinstance(value, SecretStr) else value
            if current == placeholder_default:
                offenders.append(field_name)

        if offenders:
            logger.warning(
                f"⚠ INSECURE DEFAULT(S) IN USE — RENFIELD_ENV={env!r} but the "
                f"following fields are still on their class-level placeholder "
                f"default: {', '.join(offenders)}. Set them via env vars "
                "(POSTGRES_PASSWORD, SECRET_KEY, DEFAULT_ADMIN_PASSWORD) or "
                "Docker Secrets."
            )
        return self

    @model_validator(mode="after")
    def fail_closed_on_insecure_jwt_key(self) -> "Settings":
        """Security (review M1 + #692): refuse to boot when the JWT signing key
        is insecure and it matters.

        A WARNING is insufficient: the placeholder is public (ships in the repo),
        so a known/weak key lets anyone forge a valid admin JWT (HS256 over the
        key) — a full auth bypass the moment auth is used. Enforced when EITHER
        ``AUTH_ENABLED=true`` OR ``RENFIELD_ENV`` declares a real deployment
        (production/prod/staging) — the latter closes the "auth is off today but
        the key is still weak" gap (#692). Insecure = the placeholder default OR
        shorter than 32 chars (too little entropy for HS256).

        Dev/test and the current single-user household deploy (AUTH_ENABLED=false
        with RENFIELD_ENV unset → "development") are unaffected: the guard only
        arms once an operator declares production, at which point a strong
        SECRET_KEY must already be provisioned.
        """
        env = self.renfield_env.lower()
        is_real_env = env in {"production", "prod", "staging"}
        if not (self.auth_enabled or is_real_env):
            return self

        field_info = type(self).model_fields.get("secret_key")
        placeholder = field_info.default if field_info else None
        if isinstance(placeholder, SecretStr):
            placeholder = placeholder.get_secret_value()
        current = (
            self.secret_key.get_secret_value()
            if isinstance(self.secret_key, SecretStr)
            else self.secret_key
        ) or ""

        reason = None
        if placeholder is not None and current == placeholder:
            reason = "still the placeholder default (public — it ships in the repo)"
        elif len(current) < 32:
            reason = f"too short ({len(current)} chars; need >= 32 for HS256 entropy)"

        if reason:
            raise ValueError(
                f"SECRET_KEY is insecure: {reason}. (AUTH_ENABLED={self.auth_enabled}, "
                f"RENFIELD_ENV={env!r}) — refusing to start. A weak/known key lets an "
                "attacker forge JWTs. Set SECRET_KEY to a strong random value "
                "(>= 32 random chars, env var or Docker secret)."
            )
        return self

    @model_validator(mode="after")
    def assert_auth_config_consistency(self) -> "Settings":
        """Security (#697): fail loud on an incoherent auth posture, warn on soft
        misconfigurations. Prevents a deploy that *looks* authenticated but has a
        security control silently disabled.

        HARD FAIL — ``AUTH_ENABLED=true`` with ``WS_AUTH_ENABLED=false``. HTTP
        routes would authenticate, but the WebSocket surface (the primary chat
        channel) would not: ``authenticate_websocket`` short-circuits to
        auth-skipped, so no ``user_id`` resolves and the WS chat-session
        ownership check (#657) becomes a no-op — any LAN client could then
        register against another user's conversation. Multi-user auth REQUIRES
        both flags on together; refuse to boot on the mismatch rather than run
        with a phantom control.

        WARN (not fatal — may be intentional in some deploys):
        - ``AUTH_ENABLED=true`` with wildcard ``CORS_ORIGINS='*'`` — with Bearer
          tokens this is less severe than with cookies, but a real deployment
          should pin origins.
        - A real ``RENFIELD_ENV`` (production/prod/staging) with
          ``ALLOW_REGISTRATION=true`` — open self-registration in a multi-user
          deployment lets anyone mint a Gast account.

        The current single-user posture (auth off) trips nothing: every check is
        gated on ``auth_enabled`` (or a production env), so an all-false config
        is byte-identical.
        """
        if self.auth_enabled and not self.ws_auth_enabled:
            raise ValueError(
                "Inconsistent auth config: AUTH_ENABLED=true but "
                "WS_AUTH_ENABLED=false — the WebSocket surface would be "
                "unauthenticated and the WS chat-session ownership check (#657) "
                "silently disabled. Set WS_AUTH_ENABLED=true when enabling auth "
                "(refusing to start with a phantom security control)."
            )

        if self.auth_enabled and self.cors_origins.strip() == "*":
            logger.warning(
                "⚠ AUTH_ENABLED=true with CORS_ORIGINS='*' (wildcard). A real "
                "deployment should pin CORS_ORIGINS to the frontend origin(s)."
            )

        env = self.renfield_env.lower()
        if env in {"production", "prod", "staging"} and self.allow_registration:
            logger.warning(
                f"⚠ RENFIELD_ENV={self.renfield_env!r} with ALLOW_REGISTRATION=true "
                "— open self-registration lets anyone create an account. Set "
                "ALLOW_REGISTRATION=false unless self-signup is intended."
            )
        return self

    class Config:
        env_file = ".env"
        secrets_dir = "/run/secrets"
        case_sensitive = False


# Globale Settings Instanz
settings = Settings()


@lru_cache
def get_settings() -> Settings:
    """Gibt die Settings-Instanz zurück (cached)"""
    return settings
