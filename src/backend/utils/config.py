"""
Konfiguration und Settings
"""
from functools import lru_cache

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Anwendungs-Einstellungen"""

    # Datenbank - Einzelfelder für dynamischen DATABASE_URL-Aufbau
    database_url: str | None = None
    postgres_user: str = "renfield"
    postgres_password: SecretStr = "changeme"
    postgres_host: str = "postgres"
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_db: str = "renfield"
    db_pool_size: int = Field(default=5, ge=1, le=100)
    db_max_overflow: int = Field(default=10, ge=0, le=200)
    db_pool_recycle: int = Field(default=3600, ge=60, le=86400)

    # Redis
    redis_url: str = "redis://redis:6379"

    # Ollama - Multi-Modell Konfiguration
    ollama_url: str = "http://ollama:11434"
    ollama_model: str = "llama3.2:3b"  # Legacy fallback; recommended: qwen3:14b (see docs/LLM_MODEL_GUIDE.md)
    ollama_chat_model: str = "llama3.2:3b"      # Default for dev; recommended: qwen3:14b
    ollama_rag_model: str = "llama3.2:latest"   # Default for dev; recommended: qwen3:14b
    ollama_embed_model: str = "nomic-embed-text" # Default for dev; recommended: qwen3-embedding:4b (768 dim)
    ollama_intent_model: str = "llama3.2:3b"    # Default for dev; recommended: qwen3:8b
    ollama_num_ctx: int = 32768                   # Context window für alle Ollama-Calls

    # Home Assistant
    home_assistant_url: str | None = None
    home_assistant_token: SecretStr | None = None

    # n8n — field exists so .env can set N8N_API_URL for the n8n-mcp stdio subprocess
    n8n_api_url: str | None = None

    # Frigate
    frigate_url: str | None = None

    # Plugin System
    plugins_enabled: bool = True
    plugins_dir: str = "integrations/plugins"

    # Weather Plugin
    weather_enabled: bool = False
    openweather_api_url: str | None = "https://api.openweathermap.org/data/2.5"
    openweather_api_key: SecretStr | None = None

    # News Plugin
    news_enabled: bool = False
    newsapi_url: str | None = "https://newsapi.org/v2"
    newsapi_key: SecretStr | None = None

    # Search Plugin
    search_enabled: bool = False
    duckduckgo_api_url: str | None = "https://api.duckduckgo.com"

    # Music Plugin (reserved for future use)
    # spotify_* fields removed — unused. Re-add when music integration is implemented.

    # Sprache
    default_language: str = "de"
    supported_languages: str = "de,en"  # Comma-separated list of supported languages
    whisper_model: str = "base"
    whisper_initial_prompt: str = ""  # Leer = kein Kontext-Bias (Renfield ist ein offenes System)
    piper_voice: str = "de_DE-thorsten-high"  # Default voice (legacy)
    piper_voices: str = "de:de_DE-thorsten-high,en:en_US-amy-medium"  # Language:Voice mapping

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

    # Room Management
    rooms_auto_create_from_satellite: bool = True  # Auto-create rooms when satellites register

    # Output Routing
    advertise_host: str | None = None  # Hostname/IP that external services (like HA) can reach
    advertise_port: int = 8000            # Port for advertise_host
    backend_internal_url: str = "http://backend:8000"  # Internal URL for Docker networking (fallback when advertise_host not set)

    # Wake Word Detection
    wake_word_enabled: bool = False  # Disabled by default (opt-in)
    wake_word_default: str = "alexa"  # Default wake word for satellites (alexa has 32-bit ONNX model)
    wake_word_threshold: float = 0.5
    wake_word_cooldown_ms: int = 2000

    # Satellite OTA Updates
    satellite_latest_version: str = "1.0.0"  # Latest available satellite version

    # Agent (ReAct Loop)
    agent_enabled: bool = False           # Opt-in, disabled by default
    agent_max_steps: int = Field(default=12, ge=1, le=50)
    agent_step_timeout: float = Field(default=30.0, ge=1.0, le=300.0)
    agent_total_timeout: float = Field(default=120.0, ge=5.0, le=600.0)
    agent_model: str | None = None     # Optional: separate model for agent (default: ollama_model)
    agent_ollama_url: str | None = None # Optional: separate Ollama instance for agent (default: ollama_url)
    agent_conv_context_messages: int = 6  # Number of conversation history messages in agent loop
    agent_roles_path: str = "config/agent_roles.yaml"  # Path to agent role definitions
    agent_router_timeout: float = 30.0    # Timeout for router classification LLM call (seconds)

    # MCP Client (Model Context Protocol)
    mcp_enabled: bool = False             # Opt-in, disabled by default
    mcp_config_path: str = "config/mcp_servers.yaml"
    mcp_refresh_interval: int = 60        # Background refresh interval (seconds)
    mcp_connect_timeout: float = 10.0     # Connection timeout per server (seconds)
    mcp_call_timeout: float = 30.0        # Tool call timeout (seconds)
    mcp_max_response_size: int = Field(default=10240, ge=1024, le=524288)  # 10KB max response

    # Agent Advanced
    agent_history_limit: int = Field(default=20, ge=1, le=100)       # Max history steps in agent loop
    agent_response_truncation: int = Field(default=2000, ge=100, le=50000)  # Max chars for tool response truncation

    # Embeddings
    embedding_dimension: int = Field(default=768, ge=128, le=4096)   # Embedding vector dimension

    # RAG (Retrieval-Augmented Generation)
    rag_enabled: bool = True
    rag_chunk_size: int = Field(default=512, ge=64, le=4096)
    rag_chunk_overlap: int = Field(default=50, ge=0, le=512)
    rag_top_k: int = Field(default=5, ge=1, le=50)
    rag_similarity_threshold: float = Field(default=0.4, ge=0.0, le=1.0)

    # Hybrid Search (Dense + BM25 via PostgreSQL Full-Text Search)
    rag_hybrid_enabled: bool = True           # Enable hybrid search (BM25 + dense)
    rag_hybrid_bm25_weight: float = Field(default=0.3, ge=0.0, le=1.0)
    rag_hybrid_dense_weight: float = Field(default=0.7, ge=0.0, le=1.0)
    rag_hybrid_rrf_k: int = 60                # RRF constant k (standard: 60)
    rag_hybrid_fts_config: str = "simple"     # PostgreSQL FTS config: simple/german/english

    # Context Window Retrieval
    rag_context_window: int = 1               # Adjacent chunks per direction (0=disabled)
    rag_context_window_max: int = 3           # Maximum allowed window size

    # Conversation Memory (Long-term)
    memory_enabled: bool = False                                             # Opt-in
    memory_retrieval_limit: int = Field(default=3, ge=1, le=10)              # Max memories per query
    memory_retrieval_threshold: float = Field(default=0.7, ge=0.0, le=1.0)  # Cosine-similarity threshold
    memory_max_per_user: int = Field(default=500, ge=10, le=5000)           # Max active memories
    memory_context_decay_days: int = Field(default=30, ge=1, le=365)        # Days until context category expires
    memory_dedup_threshold: float = Field(default=0.9, ge=0.5, le=1.0)     # Deduplication threshold
    memory_extraction_enabled: bool = False                                  # Auto-extract memories from conversations
    memory_cleanup_interval: int = Field(default=3600, ge=60, le=86400)     # Cleanup interval in seconds

    # Document Upload
    upload_dir: str = "/app/data/uploads"
    max_file_size_mb: int = Field(default=50, ge=1, le=500)
    allowed_extensions: str = "pdf,docx,doc,txt,md,html,pptx,xlsx"  # Comma-separated

    # Monitoring
    metrics_enabled: bool = False  # Enable Prometheus /metrics endpoint

    # Logging
    log_level: str = "INFO"

    # Security
    secret_key: SecretStr = "changeme-in-production-use-strong-random-key"
    trusted_proxies: str = ""  # Comma-separated CIDRs, e.g. "172.18.0.0/16,127.0.0.1"

    # Jellyfin
    jellyfin_enabled: bool = False
    jellyfin_url: str | None = None
    jellyfin_base_url: str | None = None
    jellyfin_api_key: SecretStr | None = None
    jellyfin_token: SecretStr | None = None
    jellyfin_user_id: str | None = None

    # Paperless-NGX
    paperless_enabled: bool = False
    paperless_api_url: str | None = None
    paperless_api_token: SecretStr | None = None

    # Email MCP
    email_mcp_enabled: bool = False
    mail_regfish_password: SecretStr | None = None

    # SearXNG
    searxng_api_url: str | None = None
    searxng_instances: str | None = None

    # n8n MCP
    n8n_base_url: str | None = None
    n8n_api_key: SecretStr | None = None
    n8n_mcp_enabled: bool = False

    # Home Assistant MCP
    ha_mcp_enabled: bool = False

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

    # Integration Timeouts
    ha_timeout: float = Field(default=10.0, ge=1.0, le=120.0)
    frigate_timeout: float = Field(default=10.0, ge=1.0, le=120.0)
    n8n_timeout: float = Field(default=30.0, ge=1.0, le=300.0)

    # Agent LLM Defaults (fallback when prompt_manager has no config)
    agent_default_temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    agent_default_num_predict: int = Field(default=2048, ge=64, le=32768)

    # Circuit Breaker
    cb_failure_threshold: int = Field(default=3, ge=1, le=50)
    cb_llm_recovery_timeout: float = Field(default=30.0, ge=1.0, le=600.0)
    cb_agent_recovery_timeout: float = Field(default=60.0, ge=1.0, le=600.0)

    # Cache TTLs (seconds)
    ha_cache_ttl: int = Field(default=300, ge=10, le=86400)
    satellite_package_cache_ttl: int = Field(default=300, ge=10, le=86400)
    intent_feedback_cache_ttl: int = Field(default=300, ge=10, le=86400)

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

    # Phase 3: Reminders
    proactive_reminders_enabled: bool = False
    proactive_reminder_check_interval: int = 15        # Sekunden

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
        # Ensure default language has a voice (fallback to piper_voice)
        if self.default_language not in voice_map:
            voice_map[self.default_language] = self.piper_voice
        return voice_map

    @model_validator(mode="after")
    def assemble_database_url(self) -> "Settings":
        """Baut DATABASE_URL aus Einzelteilen zusammen, falls nicht explizit gesetzt."""
        if self.database_url is None:
            self.database_url = (
                f"postgresql://{self.postgres_user}:{self.postgres_password.get_secret_value()}"
                f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
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
