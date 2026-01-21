"""
Konfiguration und Settings
"""
from pydantic_settings import BaseSettings
from typing import Optional, List
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
    ollama_rag_model: str = "llama3.3:latest"   # Für RAG-Antworten (größer = besser)
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
    whisper_model: str = "base"
    whisper_initial_prompt: str = ""  # Leer = kein Kontext-Bias (Renfield ist ein offenes System)
    piper_voice: str = "de_DE-thorsten-high"

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

    # Wake Word Detection
    wake_word_enabled: bool = False  # Disabled by default (opt-in)
    wake_word_default: str = "alexa"  # Default wake word for satellites (alexa has 32-bit ONNX model)
    wake_word_threshold: float = 0.5
    wake_word_cooldown_ms: int = 2000
    
    # RAG (Retrieval-Augmented Generation)
    rag_enabled: bool = True
    rag_chunk_size: int = 512           # Token-Limit pro Chunk
    rag_chunk_overlap: int = 50         # Überlappung zwischen Chunks
    rag_top_k: int = 5                  # Anzahl der relevantesten Chunks
    rag_similarity_threshold: float = 0.5  # Minimum Similarity für Ergebnisse (0-1)

    # Document Upload
    upload_dir: str = "/app/data/uploads"
    max_file_size_mb: int = 50
    allowed_extensions: str = "pdf,docx,doc,txt,md,html,pptx,xlsx"  # Comma-separated

    # Logging
    log_level: str = "INFO"
    
    # Security
    secret_key: str = "changeme-in-production-use-strong-random-key"

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

    class Config:
        env_file = ".env"
        case_sensitive = False


# Globale Settings Instanz
settings = Settings()


@lru_cache()
def get_settings() -> Settings:
    """Gibt die Settings-Instanz zurück (cached)"""
    return settings
