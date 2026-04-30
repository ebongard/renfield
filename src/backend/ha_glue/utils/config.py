"""HA-glue Pydantic settings class.

Phase 1 Week 1.2 Day 3 of the platform/ha-glue extraction. This module
owns all HomeAssistant-flavored configuration (home_assistant_*,
frigate_*, paperless_*, presence_*, media_follow_*, ha_*, radio_*,
jellyfin_*, satellite_*) that used to live inline in the platform
`utils/config.py::Settings` class.

## Why a separate BaseSettings class

Pydantic's `BaseSettings` reads env vars into class fields at
instantiation time. Multiple `BaseSettings` subclasses can coexist in
the same Python process, each reading from the same env space — the
only constraint is that field names don't conflict. Since this class
and `utils.config.Settings` declare a disjoint set of fields, both can
load from the same `.env` file / K8s ConfigMap / Docker secrets dir
without interference.

Critically, this means env var names **stay exactly as they are**.
`HOME_ASSISTANT_URL`, `PAPERLESS_API_URL`, `PRESENCE_WEBHOOK_SECRET`,
etc. all map to `HaGlueSettings` fields with no prefix change. No
existing deploy breaks. The only migration is updating the consumer
import path from `from utils.config import settings` to
`from ha_glue.utils.config import ha_glue_settings` — one line per
consumer file, and most consumers will move to `ha_glue/` anyway in
Week 2.

## When is HaGlueSettings loaded

Lazy — the module-level instance `ha_glue_settings = HaGlueSettings()`
is constructed on first import of this module. Platform-only
deployments (future `X-idra/renfield`) simply don't have this module
on disk; consumers that try to `from ha_glue.utils.config import ...`
get a clean `ModuleNotFoundError` at import time, naming the missing
package.

Platform code that currently leaks HA references (see
`docs/architecture/renfield-platform-boundary.md` in the parent Reva
repo) can either:

1. Import `ha_glue_settings` directly with a try/except guard for
   platform-only deploys (transitional — Week 4 CI lint catches this)
2. Call into ha-glue via a hook and have ha_glue check its own settings
   (the clean long-term answer — planned for Week 2/Day 5)

## Fields owned by this module

See the `HaGlueSettings` class below. Total: 39 fields, mapping 1:1
to what was removed from `utils.config.Settings` in the same commit.
"""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings


class HaGlueSettings(BaseSettings):
    """HomeAssistant / consumer-hardware configuration for ha_glue."""

    # === Home Assistant ===
    home_assistant_url: str | None = None
    home_assistant_token: SecretStr | None = None

    # === Frigate (cameras / NVR) ===
    frigate_url: str | None = None
    frigate_timeout: float = Field(default=10.0, ge=1.0, le=120.0)
    frigate_mqtt_broker: str = "localhost"
    frigate_mqtt_port: int = Field(default=1883, ge=1, le=65535)

    # === HA integration cache / timeout ===
    ha_timeout: float = Field(default=10.0, ge=1.0, le=120.0)
    ha_cache_ttl: int = Field(default=300, ge=10, le=86400)
    ha_mcp_enabled: bool = False

    # === Paperless-NGX ===
    paperless_enabled: bool = False
    paperless_api_url: str | None = None
    paperless_api_token: SecretStr | None = None

    # === Paperless Audit ===
    paperless_audit_enabled: bool = False
    paperless_audit_model: str = ""              # Empty = use default model
    paperless_audit_schedule: str = "02:00"      # Daily at 02:00
    paperless_audit_fix_mode: str = "review"     # review | auto_threshold | auto_all
    paperless_audit_confidence_threshold: float = 0.9
    paperless_audit_ocr_threshold: int = 2       # OCR <= 2 → suggest re-OCR
    paperless_audit_batch_delay: float = 2.0     # Seconds between documents

    # === Presence Detection (BLE-based room-level) ===
    presence_enabled: bool = False                      # Master-Switch for BLE presence detection
    presence_stale_timeout: int = 120                   # Seconds before user marked absent
    presence_hysteresis_scans: int = 2                  # Consecutive scans before room change
    presence_rssi_threshold: int = -80                  # dBm, signals weaker than this are ignored
    presence_household_roles: str = "Admin,Familie"     # Roles considered household members for privacy TTS
    presence_webhook_url: str = ""                      # URL to POST presence events (empty = disabled)
    presence_webhook_secret: SecretStr | None = None    # Shared secret for webhook auth (X-Webhook-Secret header)
    presence_analytics_retention_days: int = 90         # Days to keep presence events for analytics

    # === Media Follow Me (playback follows user between rooms) ===
    media_follow_enabled: bool = False                         # Master switch (requires presence_enabled)
    media_follow_suspend_timeout: float = 600.0                # Seconds before suspended session expires
    media_follow_resume_delay: float = 2.0                     # Delay before resuming in new room

    # === Radio (TuneIn) ===
    radio_enabled: bool = False
    tunein_partner_id: str = ""

    # === Jellyfin ===
    jellyfin_enabled: bool = False
    jellyfin_url: str | None = None
    jellyfin_base_url: str | None = None
    jellyfin_api_key: SecretStr | None = None
    jellyfin_token: SecretStr | None = None
    jellyfin_user_id: str | None = None

    # === Satellite OTA Updates ===
    satellite_latest_version: str = "1.0.0"  # Latest available satellite version
    satellite_package_cache_ttl: int = Field(default=300, ge=10, le=86400)

    # === Rooms ===
    rooms_auto_create_from_satellite: bool = True  # Auto-create rooms when satellites register

    class Config:
        env_file = ".env"
        secrets_dir = "/run/secrets"
        case_sensitive = False
        extra = "ignore"  # Allow platform fields to coexist in the same env space


# Module-level singleton. Constructed once, on first import of this module.
ha_glue_settings = HaGlueSettings()
