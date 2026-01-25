"""
Konfiguration und Settings
"""
from pydantic_settings import BaseSettings
from typing import Optional, List, Dict
from functools import lru_cache

class Settings(BaseSettings):
    """Anwendungs-Einstellungen"""
    
    # Datenbank
    database_url: str = "postgresql://renfield:changeme@postgres:5432/renfield"
    
    # Redis
    redis_url: str = "redis://redis:6379"
    
    # Ollama - Multi-Modell Konfiguration
    ollama_url: str = "http://ollama:11434"
    ollama_model: str = "llama3.2:3b"  # Legacy: wird als chat_model verwendet
    ollama_chat_model: str = "llama3.2:3b"      # Für normale Konversation
    ollama_rag_model: str = "llama3.2:latest"   # Für RAG-Antworten (via .env überschreibbar)
    ollama_embed_model: str = "nomic-embed-text" # Für Embeddings (768 Dimensionen)
    ollama_intent_model: str = "llama3.2:3b"    # Für Intent-Erkennung
    
    # Home Assistant
    home_assistant_url: Optional[str] = None
    home_assistant_token: Optional[str] = None
    
    # n8n
    n8n_webhook_url: Optional[str] = None
    
    # Frigate
    frigate_url: Optional[str] = None

    # Plugin System
    plugins_enabled: bool = True
    plugins_dir: str = "integrations/plugins"

    # Weather Plugin
    weather_enabled: bool = False
    openweather_api_url: Optional[str] = "https://api.openweathermap.org/data/2.5"
    openweather_api_key: Optional[str] = None

    # News Plugin
    news_enabled: bool = False
    newsapi_url: Optional[str] = "https://newsapi.org/v2"
    newsapi_key: Optional[str] = None

    # Search Plugin
    search_enabled: bool = False
    duckduckgo_api_url: Optional[str] = "https://api.duckduckgo.com"

    # Music Plugin
    music_enabled: bool = False
    spotify_api_url: Optional[str] = "https://api.spotify.com"
    spotify_client_id: Optional[str] = None
    spotify_client_secret: Optional[str] = None
    spotify_access_token: Optional[str] = None

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
    advertise_host: Optional[str] = None  # Hostname/IP that external services (like HA) can reach
    advertise_port: int = 8000            # Port for advertise_host
    backend_internal_url: str = "http://backend:8000"  # Internal URL for Docker networking (fallback when advertise_host not set)

    # Wake Word Detection
    wake_word_enabled: bool = False  # Disabled by default (opt-in)
    wake_word_default: str = "alexa"  # Default wake word for satellites (alexa has 32-bit ONNX model)
    wake_word_threshold: float = 0.5
    wake_word_cooldown_ms: int = 2000

    # Satellite OTA Updates
    satellite_latest_version: str = "1.0.0"  # Latest available satellite version
    
    # RAG (Retrieval-Augmented Generation)
    rag_enabled: bool = True
    rag_chunk_size: int = 512           # Token-Limit pro Chunk
    rag_chunk_overlap: int = 50         # Überlappung zwischen Chunks
    rag_top_k: int = 5                  # Anzahl der relevantesten Chunks
    rag_similarity_threshold: float = 0.4  # Minimum Similarity für Ergebnisse (0-1)

    # Document Upload
    upload_dir: str = "/app/data/uploads"
    max_file_size_mb: int = 50
    allowed_extensions: str = "pdf,docx,doc,txt,md,html,pptx,xlsx"  # Comma-separated

    # Logging
    log_level: str = "INFO"
    
    # Security
    secret_key: str = "changeme-in-production-use-strong-random-key"

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
    default_admin_password: str = "changeme"  # MUST be changed in production!

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

    # WebSocket Connection Limits
    ws_max_connections_per_ip: int = 10
    ws_max_message_size: int = 1_000_000  # 1MB max message size
    ws_max_audio_buffer_size: int = 10_000_000  # 10MB max audio buffer per session

    # WebSocket Protocol
    ws_protocol_version: str = "1.0"

    @property
    def allowed_extensions_list(self) -> List[str]:
        """Gibt allowed_extensions als Liste zurück"""
        return [ext.strip().lower() for ext in self.allowed_extensions.split(",")]

    @property
    def supported_languages_list(self) -> List[str]:
        """Returns supported_languages as a list"""
        return [lang.strip().lower() for lang in self.supported_languages.split(",")]

    @property
    def piper_voice_map(self) -> Dict[str, str]:
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

    class Config:
        env_file = ".env"
        case_sensitive = False


# Globale Settings Instanz
settings = Settings()


@lru_cache()
def get_settings() -> Settings:
    """Gibt die Settings-Instanz zurück (cached)"""
    return settings
