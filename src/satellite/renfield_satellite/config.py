"""
Configuration management for Renfield Satellite

Loads configuration from YAML file and environment variables.
"""

import os
from dataclasses import dataclass, field
from typing import List, Optional
import yaml


# Type alias for list default factory
def _empty_list() -> List[str]:
    return []


@dataclass
class SatelliteConfig:
    """Satellite identification"""
    id: str = "sat-default"
    room: str = "Default Room"
    language: str = "de"  # Language code for STT/TTS (e.g., 'de', 'en')


@dataclass
class ServerConfig:
    """Backend server connection settings"""
    url: Optional[str] = None  # WebSocket URL - if None, uses auto-discovery
    auto_discover: bool = True  # Use zeroconf to find server automatically
    discovery_timeout: float = 10.0  # Seconds to wait for discovery
    reconnect_interval: int = 5  # seconds
    heartbeat_interval: int = 30  # seconds
    # Authentication (required when server has WS_AUTH_ENABLED=true)
    auth_enabled: bool = False  # Whether to fetch and use auth token
    auth_token: Optional[str] = None  # Pre-configured token (optional)


@dataclass
class AudioConfig:
    """Audio capture and playback settings"""
    sample_rate: int = 16000
    chunk_size: int = 1280  # 80ms at 16kHz
    channels: int = 1
    format_bits: int = 16
    device: str = "plughw:1,0"  # ReSpeaker default
    playback_device: str = "plughw:1,0"


@dataclass
class WakeWordConfig:
    """Wake word detection settings"""
    model: str = "hey_jarvis"
    threshold: float = 0.5
    models_path: str = "/opt/renfield-satellite/models"
    refractory_seconds: float = 2.0  # Cooldown before re-triggering
    stop_words: List[str] = field(default_factory=list)  # Words to cancel interaction


@dataclass
class VADConfig:
    """Voice Activity Detection settings"""
    backend: str = "rms"  # "rms", "webrtc", or "silero"
    silence_threshold: int = 500  # RMS threshold (for RMS backend)
    silence_duration_ms: int = 1500  # ms of silence to end recording
    max_recording_seconds: float = 15.0  # Maximum recording length
    webrtc_aggressiveness: int = 2  # WebRTC VAD aggressiveness (0-3)
    silero_threshold: float = 0.5  # Silero VAD threshold (0-1)
    silero_model_path: Optional[str] = None  # Path to silero_vad.onnx


@dataclass
class LEDConfig:
    """LED control settings"""
    brightness: int = 20  # 0-255
    spi_bus: int = 0
    spi_device: int = 0
    num_leds: int = 3


@dataclass
class ButtonConfig:
    """Button settings"""
    gpio_pin: int = 17
    debounce_ms: int = 50


@dataclass
class Config:
    """Main configuration container"""
    satellite: SatelliteConfig = field(default_factory=SatelliteConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    wakeword: WakeWordConfig = field(default_factory=WakeWordConfig)
    vad: VADConfig = field(default_factory=VADConfig)
    led: LEDConfig = field(default_factory=LEDConfig)
    button: ButtonConfig = field(default_factory=ButtonConfig)


def load_config(config_path: Optional[str] = None) -> Config:
    """
    Load configuration from YAML file.

    Args:
        config_path: Path to config file. If None, uses default locations.

    Returns:
        Config object with loaded settings
    """
    # Default config paths
    default_paths = [
        "/opt/renfield-satellite/config/satellite.yaml",
        "/etc/renfield-satellite/config.yaml",
        os.path.expanduser("~/.renfield-satellite/config.yaml"),
        "config/satellite.yaml",
    ]

    # Find config file
    if config_path:
        paths = [config_path]
    else:
        paths = default_paths

    config_data = {}
    for path in paths:
        if os.path.exists(path):
            with open(path, "r") as f:
                config_data = yaml.safe_load(f) or {}
            print(f"Loaded config from: {path}")
            break

    # Create config with defaults
    config = Config()

    # Override with loaded values
    if "satellite" in config_data:
        sat = config_data["satellite"]
        config.satellite.id = sat.get("id", config.satellite.id)
        config.satellite.room = sat.get("room", config.satellite.room)
        config.satellite.language = sat.get("language", config.satellite.language)

    if "server" in config_data:
        srv = config_data["server"]
        # URL is optional - only set if explicitly provided
        if "url" in srv:
            config.server.url = srv["url"]
        config.server.auto_discover = srv.get("auto_discover", config.server.auto_discover)
        config.server.discovery_timeout = srv.get("discovery_timeout", config.server.discovery_timeout)
        config.server.reconnect_interval = srv.get("reconnect_interval", config.server.reconnect_interval)
        config.server.heartbeat_interval = srv.get("heartbeat_interval", config.server.heartbeat_interval)
        config.server.auth_enabled = srv.get("auth_enabled", config.server.auth_enabled)
        if "auth_token" in srv:
            config.server.auth_token = srv["auth_token"]

    if "audio" in config_data:
        aud = config_data["audio"]
        config.audio.sample_rate = aud.get("sample_rate", config.audio.sample_rate)
        config.audio.chunk_size = aud.get("chunk_size", config.audio.chunk_size)
        config.audio.device = aud.get("device", config.audio.device)
        config.audio.playback_device = aud.get("playback_device", config.audio.playback_device)

    if "wakeword" in config_data:
        ww = config_data["wakeword"]
        config.wakeword.model = ww.get("model", config.wakeword.model)
        config.wakeword.threshold = ww.get("threshold", config.wakeword.threshold)
        config.wakeword.models_path = ww.get("models_path", config.wakeword.models_path)
        config.wakeword.refractory_seconds = ww.get("refractory_seconds", config.wakeword.refractory_seconds)
        if "stop_words" in ww:
            config.wakeword.stop_words = ww["stop_words"]

    if "vad" in config_data:
        vad = config_data["vad"]
        config.vad.backend = vad.get("backend", config.vad.backend)
        config.vad.silence_threshold = vad.get("silence_threshold", config.vad.silence_threshold)
        config.vad.silence_duration_ms = vad.get("silence_duration_ms", config.vad.silence_duration_ms)
        config.vad.max_recording_seconds = vad.get("max_recording_seconds", config.vad.max_recording_seconds)
        config.vad.webrtc_aggressiveness = vad.get("webrtc_aggressiveness", config.vad.webrtc_aggressiveness)
        config.vad.silero_threshold = vad.get("silero_threshold", config.vad.silero_threshold)
        config.vad.silero_model_path = vad.get("silero_model_path", config.vad.silero_model_path)

    if "led" in config_data:
        led = config_data["led"]
        config.led.brightness = led.get("brightness", config.led.brightness)
        config.led.num_leds = led.get("num_leds", config.led.num_leds)

    if "button" in config_data:
        btn = config_data["button"]
        config.button.gpio_pin = btn.get("gpio_pin", config.button.gpio_pin)

    # Environment variable overrides
    if os.environ.get("RENFIELD_SATELLITE_ID"):
        config.satellite.id = os.environ["RENFIELD_SATELLITE_ID"]
    if os.environ.get("RENFIELD_SATELLITE_ROOM"):
        config.satellite.room = os.environ["RENFIELD_SATELLITE_ROOM"]
    if os.environ.get("RENFIELD_SATELLITE_LANGUAGE"):
        config.satellite.language = os.environ["RENFIELD_SATELLITE_LANGUAGE"]
    if os.environ.get("RENFIELD_SERVER_URL"):
        config.server.url = os.environ["RENFIELD_SERVER_URL"]
    if os.environ.get("RENFIELD_AUTO_DISCOVER"):
        config.server.auto_discover = os.environ["RENFIELD_AUTO_DISCOVER"].lower() in ("true", "1", "yes")
    if os.environ.get("RENFIELD_WAKEWORD_THRESHOLD"):
        config.wakeword.threshold = float(os.environ["RENFIELD_WAKEWORD_THRESHOLD"])
    # Auth settings
    if os.environ.get("RENFIELD_AUTH_ENABLED"):
        config.server.auth_enabled = os.environ["RENFIELD_AUTH_ENABLED"].lower() in ("true", "1", "yes")
    if os.environ.get("RENFIELD_AUTH_TOKEN"):
        config.server.auth_token = os.environ["RENFIELD_AUTH_TOKEN"]

    return config
