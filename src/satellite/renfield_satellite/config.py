"""
Configuration management for Renfield Satellite

Loads configuration from YAML file and environment variables.
"""

import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional
import yaml

logger = logging.getLogger(__name__)


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
    # Connection robustness (tuned for the Pi Zero 2 W's marginal 2.4GHz WiFi):
    # faster dead-link detection + bounded handshake so a blip self-heals instead
    # of wedging. See docs note in websocket_client.py.
    ping_interval: int = 15  # WS keepalive ping cadence (was hardcoded 20)
    ping_timeout: int = 8    # close the link if no pong within this (was 10)
    register_timeout: float = 15.0  # cap the post-connect register handshake
    # If the satellite cannot reconnect for this long, exit so systemd restarts a
    # fresh process (clears any wedged in-process state). 0 disables.
    max_disconnected_seconds: int = 300
    # Authentication (required when server has WS_AUTH_ENABLED=true)
    auth_enabled: bool = False  # Whether to fetch and use auth token
    auth_token: Optional[str] = None  # Pre-configured token (optional)
    # Per-satellite enrollment PSK (security review H1). Provisioned out-of-band
    # (Ansible host_vars / k8s secret), presented in the register frame and
    # verified server-side against the `satellites` table. Independent of
    # auth_enabled (the WS-JWT path); empty = not enrolled (legacy behavior).
    enrollment_token: Optional[str] = None
    # TLS verification (set False only for self-signed certificates)
    verify_tls: bool = True


@dataclass
class BeamformingConfig:
    """Beamforming settings for ReSpeaker 2-Mics HAT"""
    enabled: bool = False
    mic_spacing: float = 0.058  # ReSpeaker 2-Mics: 58mm
    steering_angle: float = 0.0  # 0 = front-facing


@dataclass
class AudioConfig:
    """Audio capture and playback settings"""
    sample_rate: int = 16000
    chunk_size: int = 1280  # 80ms at 16kHz
    channels: int = 1
    # Physical mic count (the HARDWARE — separate from `channels` which is
    # the post-DSP capture stream). For XVF3800-USB / AC108 4-mic arrays
    # `channels=1` post-beamforming but `physical_mics=4`; the satellite
    # reports this as a capability so the fleet page can show the actual
    # array size. Defaults to ``channels`` for 2-mic HATs where the two
    # match — provisioning sets it explicitly only when they differ.
    physical_mics: int | None = None
    use_arecord: bool = False  # Use arecord subprocess (required for AC108 4-mic + onnxruntime)
    device: str = "plughw:1,0"  # ReSpeaker default
    playback_device: str = "plughw:1,0"
    # Stereo→mono combine (docs/design/satellite-audio-combine-pipeline.md).
    # The wakeword/STT need one mono channel; how the native multi-channel
    # capture is reduced to it depends on the hardware:
    #   beamform    — delay-and-sum of raw mics (2-mic HAT)
    #   select      — keep one channel, drop the rest (XVF3800 processed beam,
    #                 AC108 mic channel) — the others are residual/reference,
    #                 NOT mics, so downmixing them is wrong (it cancels to
    #                 silence on the XVF3800 — the Fitnessraum wakeword-deaf
    #                 incident)
    #   passthrough — already mono, emit as-is
    # None = auto-derive from the legacy signals so un-reprovisioned sats stay
    # byte-identical: beamforming.enabled→beamform, channels>1→select, else
    # passthrough.
    combine: Optional[str] = None
    # Which channel `select` keeps. None = legacy default resolved by the
    # capture backend (AC108 4-mic → ch1, its ch0 is the silent reference;
    # everything else → ch0). Set explicitly per hardware (XVF3800 → 0).
    select_channel: Optional[int] = None
    # Upstream audio transport codec (C1, voice-identity design): "pcm"
    # (legacy base64-in-JSON, default) or "opus" (binary WS frames; needs
    # opuslib + a backend with satellite_opus_enabled — negotiated at
    # register time, degrades to pcm automatically otherwise).
    codec: str = "pcm"
    beamforming: BeamformingConfig = field(default_factory=BeamformingConfig)

    @property
    def effective_physical_mics(self) -> int:
        """Resolve `physical_mics` (explicit hw count) or fall back to
        `channels`. Single read-site so callers never accidentally treat
        capture channels as a mic count."""
        return self.physical_mics if self.physical_mics is not None else self.channels


@dataclass
class WakeWordConfig:
    """Wake word detection settings"""
    model: str = "hey_jarvis"
    threshold: float = 0.5
    models_path: str = "/opt/renfield-satellite/models"
    refractory_seconds: float = 2.0  # Cooldown before re-triggering
    stop_words: List[str] = field(default_factory=list)  # Words to cancel interaction
    # VAD-gating: only run (expensive) wake-word inference when the VAD reports
    # speech, freeing CPU headroom so audio frames are not dropped under load.
    # OFF by default: with a high vad_silero_threshold this can SUPPRESS quiet
    # speech (the VAD never opens the gate), so it must be paired with a low VAD
    # threshold and validated per-room before enabling. The real cure for
    # amplitude-sensitive scoring is input-level AGC (e.g. the WM8960 ALC), not
    # this optimization. Opt-in via `wakeword.vad_gated: true`.
    vad_gated: bool = False
    vad_gate_preroll_chunks: int = 4   # chunks of context fed at speech onset (~320ms)
    vad_gate_tail_chunks: int = 15     # chunks to keep running after last speech (~1.2s)


@dataclass
class VADConfig:
    """Voice Activity Detection settings"""
    backend: str = "rms"  # "rms", "webrtc", or "silero"
    silence_threshold: int = 500  # RMS threshold (for RMS backend)
    silence_duration_ms: int = 1500  # ms of silence to end recording
    min_listening_seconds: float = 2.0  # Grace period before silence detection starts
    max_recording_seconds: float = 15.0  # Maximum recording length
    webrtc_aggressiveness: int = 2  # WebRTC VAD aggressiveness (0-3)
    silero_threshold: float = 0.5  # Silero VAD threshold (0-1)
    silero_model_path: Optional[str] = None  # Path to silero_vad.onnx


@dataclass
class LEDConfig:
    """LED control settings"""
    type: str = "apa102"  # "apa102", "gpio_rgb", "xvf3800", or "none"
    brightness: int = 20  # 0-31 (APA102/XVF3800 scale)
    # Per-device brightness FLOOR (0-31). Backend-pushed brightness
    # (`led_config` / register_ack led_brightness, incl. night-dimming) is
    # clamped UP to this value locally. Default 0 = no floor (fleet-identical:
    # the backend value is applied verbatim). Set > 0 only for a device whose
    # ring must stay visible regardless of the fleet daypart dimming — e.g. the
    # Esszimmer XVF3800 behind a milled (gefräste) faceplate that needs ~90%
    # (=28) to shine through. See satellite.py::_on_led_config_update.
    min_brightness: int = 0  # 0-31 (APA102/XVF3800 scale)
    # Documentation/default only — the LIVE night brightness is pushed by the
    # backend over the WebSocket (`led_config` / register_ack led_brightness).
    # This is the local fallback default the backend's led_night_brightness
    # mirrors; the satellite does not apply it on its own.
    night_brightness: int = 5
    spi_bus: int = 0
    spi_device: int = 0
    num_leds: int = 3
    led_power_pin: Optional[int] = None  # GPIO pin to enable LED power (4-mic HAT: 5)
    idle_color: Optional[str] = None  # IDLE pulse color name (e.g. "green", "yellow"); None = blue
    # GPIO RGB LED pins (Whisplay HAT)
    gpio_red: Optional[int] = None
    gpio_green: Optional[int] = None
    gpio_blue: Optional[int] = None
    # XVF3800 LED control
    xvf_host_path: Optional[str] = None  # Path to xvf_host binary


@dataclass
class ButtonConfig:
    """Button settings"""
    gpio_pin: int = 17
    debounce_ms: int = 50


@dataclass
class DisplayConfig:
    """Display settings (SPI TFT — ST7789/ILI9341). gpio_backend selects the pin
    driver: 'rpi' (gpiozero/BCM, Whisplay on a Pi) or 'sunxi' (libgpiod, Orange Pi
    A733 — dc/rst/bl are line offsets on `gpiochip`)."""
    enabled: bool = False
    width: int = 240
    height: int = 280
    gpio_backend: str = "rpi"
    spi_bus: int = 0
    spi_device: int = 0
    spi_speed_hz: int = 80_000_000
    dc_pin: int = 27
    rst_pin: int = 4
    bl_pin: int = 22
    gpiochip: str = "/dev/gpiochip0"


@dataclass
class CameraConfig:
    """Camera settings for visual queries"""
    enabled: bool = False
    resolution: str = "1280x720"
    quality: int = 85
    backend: str = "rpicam"  # "rpicam" (Pi) | "sunxi_isp" (Orange Pi A733 + AW ISP)


@dataclass
class BLEConfig:
    """BLE presence detection settings"""
    enabled: bool = False
    scan_interval: int = 30       # seconds between scans
    scan_duration: float = 5.0    # seconds per scan
    rssi_threshold: int = -80     # ignore weaker signals
    known_devices: List[str] = field(default_factory=list)  # MAC whitelist, pushed from backend
    classic_rssi: bool = True     # read real Classic-BT RSSI (hcitool cc/rssi); off => synthetic -50
    classic_rssi_interval: float = 300.0  # seconds between real RSSI reads per device (throttle connect churn)
    # Continuous scanning (BT 5.x / mains-powered nodes): keep a single BleakScanner
    # running with a detection callback instead of periodic discover() bursts, and
    # report a smoothed (EWMA) per-device RSSI. Lower latency + steadier RSSI for
    # room arbitration. Falls back to the discover() loop when False.
    continuous: bool = False
    smoothing_alpha: float = 0.4      # EWMA weight for each new RSSI sample (0..1)
    freshness_seconds: float = 20.0   # drop a device from presence if unseen this long
    # Identity Resolving Keys for resolving rotating RPAs (iPhones/Android) to a
    # stable identity — name -> 32-char hex IRK (16 bytes, MSO-first). Obtained
    # out-of-band (iPhone: Mac/iCloud keychain; Android: bonded-device info),
    # pushed from the backend. See docs/design/ble-presence-improvement.md.
    irks: Dict[str, str] = field(default_factory=dict)


@dataclass
class EnviroConfig:
    """Enviro pHAT sensor settings"""
    enabled: bool = False
    read_interval: int = 30  # seconds between sensor reads


@dataclass
class UpdateConfig:
    """OTA update authenticity settings (security H6)."""
    # Pinned Ed25519 release public keys (64-hex each); the signed release
    # manifest is verified against these. Multiple = key rotation. Safe in git.
    release_pubkeys: List[str] = field(default_factory=list)
    # When True, reject an OTA update that has no valid signed manifest
    # (fail closed). Default False = verify-if-present.
    require_signature: bool = False


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
    display: DisplayConfig = field(default_factory=DisplayConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)
    ble: BLEConfig = field(default_factory=BLEConfig)
    enviro: EnviroConfig = field(default_factory=EnviroConfig)
    update: UpdateConfig = field(default_factory=UpdateConfig)


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
        config.server.ping_interval = srv.get("ping_interval", config.server.ping_interval)
        config.server.ping_timeout = srv.get("ping_timeout", config.server.ping_timeout)
        config.server.register_timeout = srv.get("register_timeout", config.server.register_timeout)
        config.server.max_disconnected_seconds = srv.get("max_disconnected_seconds", config.server.max_disconnected_seconds)
        config.server.auth_enabled = srv.get("auth_enabled", config.server.auth_enabled)
        config.server.verify_tls = srv.get("verify_tls", config.server.verify_tls)
        if "auth_token" in srv:
            config.server.auth_token = srv["auth_token"]
        if "enrollment_token" in srv:
            # Treat a blank template value ("") as "not enrolled" so the
            # register frame omits the token rather than sending an empty one.
            config.server.enrollment_token = srv["enrollment_token"] or None
            if not config.server.enrollment_token:
                # The key is present but resolved blank — likely a provisioning
                # miss (un-rendered host_var / empty secret). Warn loudly: this
                # satellite registers UNENROLLED and will be rejected once the
                # fleet enforces. (Silent degrade hid the cryptography no-op bug.)
                logger.warning(
                    "server.enrollment_token is present but BLANK — registering "
                    "UNENROLLED. Provision satellite_enrollment_token (host_vars) "
                    "or RENFIELD_ENROLLMENT_TOKEN, or the satellite will be "
                    "rejected once enrollment enforcement is on."
                )

    if "audio" in config_data:
        aud = config_data["audio"]
        config.audio.sample_rate = aud.get("sample_rate", config.audio.sample_rate)
        config.audio.chunk_size = aud.get("chunk_size", config.audio.chunk_size)
        config.audio.channels = aud.get("channels", config.audio.channels)
        if "physical_mics" in aud:
            config.audio.physical_mics = aud["physical_mics"]
        config.audio.use_arecord = aud.get("use_arecord", config.audio.use_arecord)
        config.audio.device = aud.get("device", config.audio.device)
        config.audio.playback_device = aud.get("playback_device", config.audio.playback_device)
        config.audio.codec = aud.get("codec", config.audio.codec)
        config.audio.combine = aud.get("combine", config.audio.combine)
        config.audio.select_channel = aud.get("select_channel", config.audio.select_channel)

        # Beamforming config
        if "beamforming" in aud:
            bf = aud["beamforming"]
            config.audio.beamforming.enabled = bf.get("enabled", config.audio.beamforming.enabled)
            config.audio.beamforming.mic_spacing = bf.get("mic_spacing", config.audio.beamforming.mic_spacing)
            config.audio.beamforming.steering_angle = bf.get("steering_angle", config.audio.beamforming.steering_angle)

    if "wakeword" in config_data:
        ww = config_data["wakeword"]
        config.wakeword.model = ww.get("model", config.wakeword.model)
        config.wakeword.threshold = ww.get("threshold", config.wakeword.threshold)
        config.wakeword.models_path = ww.get("models_path", config.wakeword.models_path)
        config.wakeword.refractory_seconds = ww.get("refractory_seconds", config.wakeword.refractory_seconds)
        config.wakeword.vad_gated = ww.get("vad_gated", config.wakeword.vad_gated)
        config.wakeword.vad_gate_preroll_chunks = ww.get("vad_gate_preroll_chunks", config.wakeword.vad_gate_preroll_chunks)
        config.wakeword.vad_gate_tail_chunks = ww.get("vad_gate_tail_chunks", config.wakeword.vad_gate_tail_chunks)
        if "stop_words" in ww:
            config.wakeword.stop_words = ww["stop_words"]

    if "vad" in config_data:
        vad = config_data["vad"]
        config.vad.backend = vad.get("backend", config.vad.backend)
        config.vad.silence_threshold = vad.get("silence_threshold", config.vad.silence_threshold)
        config.vad.silence_duration_ms = vad.get("silence_duration_ms", config.vad.silence_duration_ms)
        config.vad.min_listening_seconds = vad.get("min_listening_seconds", config.vad.min_listening_seconds)
        config.vad.max_recording_seconds = vad.get("max_recording_seconds", config.vad.max_recording_seconds)
        config.vad.webrtc_aggressiveness = vad.get("webrtc_aggressiveness", config.vad.webrtc_aggressiveness)
        config.vad.silero_threshold = vad.get("silero_threshold", config.vad.silero_threshold)
        config.vad.silero_model_path = vad.get("silero_model_path", config.vad.silero_model_path)

    if "led" in config_data:
        led = config_data["led"]
        config.led.type = led.get("type", config.led.type)
        config.led.brightness = led.get("brightness", config.led.brightness)
        config.led.min_brightness = led.get("min_brightness", config.led.min_brightness)
        config.led.night_brightness = led.get("night_brightness", config.led.night_brightness)
        config.led.num_leds = led.get("num_leds", config.led.num_leds)
        config.led.spi_bus = led.get("spi_bus", config.led.spi_bus)
        config.led.spi_device = led.get("spi_device", config.led.spi_device)
        config.led.led_power_pin = led.get("led_power_pin", config.led.led_power_pin)
        config.led.idle_color = led.get("idle_color", config.led.idle_color)
        config.led.gpio_red = led.get("gpio_red", config.led.gpio_red)
        config.led.gpio_green = led.get("gpio_green", config.led.gpio_green)
        config.led.gpio_blue = led.get("gpio_blue", config.led.gpio_blue)
        config.led.xvf_host_path = led.get("xvf_host_path", config.led.xvf_host_path)

    if "button" in config_data:
        btn = config_data["button"]
        config.button.gpio_pin = btn.get("gpio_pin", config.button.gpio_pin)

    if "display" in config_data:
        disp = config_data["display"]
        config.display.enabled = disp.get("enabled", config.display.enabled)
        config.display.width = disp.get("width", config.display.width)
        config.display.height = disp.get("height", config.display.height)
        config.display.gpio_backend = disp.get("gpio_backend", config.display.gpio_backend)
        config.display.spi_bus = disp.get("spi_bus", config.display.spi_bus)
        config.display.spi_device = disp.get("spi_device", config.display.spi_device)
        config.display.spi_speed_hz = disp.get("spi_speed_hz", config.display.spi_speed_hz)
        config.display.dc_pin = disp.get("dc_pin", config.display.dc_pin)
        config.display.rst_pin = disp.get("rst_pin", config.display.rst_pin)
        config.display.bl_pin = disp.get("bl_pin", config.display.bl_pin)
        config.display.gpiochip = disp.get("gpiochip", config.display.gpiochip)

    if "camera" in config_data:
        cam = config_data["camera"]
        config.camera.enabled = cam.get("enabled", config.camera.enabled)
        config.camera.resolution = cam.get("resolution", config.camera.resolution)
        config.camera.quality = cam.get("quality", config.camera.quality)
        config.camera.backend = cam.get("backend", config.camera.backend)

    if "ble" in config_data:
        ble = config_data["ble"]
        config.ble.enabled = ble.get("enabled", config.ble.enabled)
        config.ble.scan_interval = ble.get("scan_interval", config.ble.scan_interval)
        config.ble.scan_duration = ble.get("scan_duration", config.ble.scan_duration)
        config.ble.rssi_threshold = ble.get("rssi_threshold", config.ble.rssi_threshold)
        config.ble.classic_rssi = ble.get("classic_rssi", config.ble.classic_rssi)
        config.ble.classic_rssi_interval = ble.get("classic_rssi_interval", config.ble.classic_rssi_interval)
        config.ble.continuous = ble.get("continuous", config.ble.continuous)
        config.ble.smoothing_alpha = ble.get("smoothing_alpha", config.ble.smoothing_alpha)
        config.ble.freshness_seconds = ble.get("freshness_seconds", config.ble.freshness_seconds)
        if "irks" in ble and isinstance(ble["irks"], dict):
            config.ble.irks = ble["irks"]
        if "known_devices" in ble:
            config.ble.known_devices = ble["known_devices"]

    if "enviro" in config_data:
        env = config_data["enviro"]
        config.enviro.enabled = env.get("enabled", config.enviro.enabled)
        config.enviro.read_interval = env.get("read_interval", config.enviro.read_interval)

    if "update" in config_data:
        upd = config_data["update"]
        pubkeys = upd.get("release_pubkeys", config.update.release_pubkeys)
        # Tolerate a single string or a list; drop blanks.
        if isinstance(pubkeys, str):
            pubkeys = [pubkeys]
        config.update.release_pubkeys = [str(k).strip() for k in (pubkeys or []) if str(k).strip()]
        config.update.require_signature = bool(
            upd.get("require_signature", config.update.require_signature)
        )

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
    if os.environ.get("RENFIELD_RELEASE_PUBKEYS"):
        config.update.release_pubkeys = [
            k.strip() for k in os.environ["RENFIELD_RELEASE_PUBKEYS"].split(",") if k.strip()
        ]
    if os.environ.get("RENFIELD_OTA_REQUIRE_SIGNATURE"):
        config.update.require_signature = os.environ["RENFIELD_OTA_REQUIRE_SIGNATURE"].lower() in ("true", "1", "yes")
    if "RENFIELD_ENROLLMENT_TOKEN" in os.environ:
        # Presence check (not truthiness) so an env-set-but-BLANK token (e.g. a
        # k8s Secret with an empty value) gets the same loud warning as the YAML
        # path. In the k8s topology the ConfigMap omits enrollment_token, so the
        # env var is the ONLY place this warning can fire. (Secret absent
        # entirely + optional:true stays silent — that is the dark-boot path.)
        config.server.enrollment_token = os.environ["RENFIELD_ENROLLMENT_TOKEN"] or None
        if not config.server.enrollment_token:
            logger.warning(
                "RENFIELD_ENROLLMENT_TOKEN is set but BLANK — registering "
                "UNENROLLED. Provision the k8s Secret / env var with a valid PSK, "
                "or the satellite will be rejected once enrollment enforces."
            )
    if os.environ.get("RENFIELD_VERIFY_TLS"):
        config.server.verify_tls = os.environ["RENFIELD_VERIFY_TLS"].lower() in ("true", "1", "yes")
    if os.environ.get("RENFIELD_BLE_ENABLED"):
        config.ble.enabled = os.environ["RENFIELD_BLE_ENABLED"].lower() in ("true", "1", "yes")

    return config
