"""
Konfiguration und Settings
"""
import os
from functools import lru_cache

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

    # Datenbank - Einzelfelder für dynamischen DATABASE_URL-Aufbau
    database_url: str | None = None
    postgres_user: str = "renfield"
    postgres_password: SecretStr = "changeme"
    postgres_host: str = "postgres"
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_db: str = "renfield"
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

    # Home Assistant / Frigate settings moved to ha_glue/utils/config.py
    # (see `HaGlueSettings`). Access via:
    #     from ha_glue.utils.config import ha_glue_settings
    # Env var names (HOME_ASSISTANT_URL, HOME_ASSISTANT_TOKEN, FRIGATE_URL,
    # FRIGATE_TIMEOUT) are unchanged.

    # n8n — field exists so .env can set N8N_API_URL for the n8n-mcp stdio subprocess
    n8n_api_url: str | None = None

    # MCP integration toggles (used by mcp_servers.yaml)
    weather_enabled: bool = False
    news_enabled: bool = False
    search_enabled: bool = False
    calendar_enabled: bool = False

    # Sprache
    default_language: str = "de"
    supported_languages: str = "de,en"  # Comma-separated list of supported languages
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
    speaker_recognition_threshold: float = 0.25  # Minimum similarity for positive identification (0-1)
    speaker_recognition_device: str = "cpu"      # Device for inference: "cpu" or "cuda"
    speaker_auto_enroll: bool = True             # Auto-create unknown speakers and save embeddings
    speaker_continuous_learning: bool = True     # Add embeddings to known speakers on each interaction
    # Per-user vocabulary corpus capture (Phase B-3 follow-up). Confirmed-
    # speaker transcripts are appended to speaker_vocabulary_corpus and a
    # daily batch job rebuilds the per-user vocab table for STT bias.
    speaker_vocab_capture_enabled: bool = True
    speaker_vocab_rebuild_interval_seconds: int = 86400  # Daily

    # Room Management / Satellite OTA moved to ha_glue/utils/config.py.

    # Output Routing
    advertise_host: str | None = None  # Hostname/IP that external services (like HA) can reach
    advertise_port: int = 8000            # Port for advertise_host
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

    # OpenAI-compatible LLM endpoint (e.g. llama-server). When set, the agent
    # tier (and optionally chat/RAG/intent via per-tier overrides below) routes
    # through this endpoint instead of Ollama. The URL must include the
    # OpenAI-compatible path prefix, typically `…/v1`.
    llm_openai_base_url: str | None = None
    llm_openai_api_key: SecretStr | None = None    # Any non-empty string is accepted by llama-server
    llm_openai_model: str = "qwen3.6"               # Logical model name exposed by the server (`--alias`)
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
    # Chat retrieval uses `memory_retrieval_threshold=0.7` for high
    # precision (don't surface tangential memories to the user). Extract
    # is a different surface: high recall is what matters, and the LLM
    # plus the drift check together replace the score gate. Defaulting
    # to 0.0 means the LLM sees top-K candidates regardless of similarity.
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

    # === Authentication ===
    # Set to True to enable authentication (default: False for development)
    auth_enabled: bool = False

    # JWT Token settings
    access_token_expire_minutes: int = 60 * 24  # 24 hours
    refresh_token_expire_days: int = 30

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
        env = os.getenv("RENFIELD_ENV", "development").lower()
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
