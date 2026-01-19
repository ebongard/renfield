"""
Konfiguration und Settings
"""
from pydantic_settings import BaseSettings
from typing import Optional

class Settings(BaseSettings):
    """Anwendungs-Einstellungen"""
    
    # Datenbank
    database_url: str = "postgresql://renfield:changeme@postgres:5432/renfield"
    
    # Redis
    redis_url: str = "redis://redis:6379"
    
    # Ollama
    ollama_url: str = "http://ollama:11434"
    ollama_model: str = "llama3.2:3b"
    
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

    # Wake Word Detection
    wake_word_enabled: bool = False  # Disabled by default (opt-in)
    wake_word_default: str = "alexa"  # Default wake word for satellites (alexa has 32-bit ONNX model)
    wake_word_threshold: float = 0.5
    wake_word_cooldown_ms: int = 2000
    
    # Logging
    log_level: str = "INFO"
    
    # Security
    secret_key: str = "changeme-in-production-use-strong-random-key"
    
    class Config:
        env_file = ".env"
        case_sensitive = False

# Globale Settings Instanz
settings = Settings()
