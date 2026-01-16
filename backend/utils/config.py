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
    piper_voice: str = "de_DE-thorsten-high"
    
    # Logging
    log_level: str = "INFO"
    
    # Security
    secret_key: str = "changeme-in-production-use-strong-random-key"
    
    class Config:
        env_file = ".env"
        case_sensitive = False

# Globale Settings Instanz
settings = Settings()
