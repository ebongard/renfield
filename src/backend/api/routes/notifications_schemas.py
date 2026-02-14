"""
Pydantic Request/Response Schemas for Notification API
"""

from pydantic import BaseModel, Field, field_validator

VALID_URGENCIES = {"critical", "info", "low", "auto"}


VALID_PRIVACY_LEVELS = {"public", "personal", "confidential"}


class WebhookRequest(BaseModel):
    """Incoming webhook payload from HA automation."""
    event_type: str = Field(..., min_length=1, max_length=100)
    title: str = Field(..., min_length=1, max_length=255)
    message: str = Field(..., min_length=1, max_length=5000)
    urgency: str = Field(default="info", max_length=20)
    room: str | None = Field(None, max_length=100)
    tts: bool | None = None
    data: dict | None = None
    enrich: bool = False
    privacy: str = Field(default="public", max_length=20)
    target_user_id: int | None = None

    @field_validator("urgency")
    @classmethod
    def validate_urgency(cls, v: str) -> str:
        if v not in VALID_URGENCIES:
            raise ValueError(f"urgency must be one of {VALID_URGENCIES}")
        return v

    @field_validator("privacy")
    @classmethod
    def validate_privacy(cls, v: str) -> str:
        if v not in VALID_PRIVACY_LEVELS:
            raise ValueError(f"privacy must be one of {VALID_PRIVACY_LEVELS}")
        return v


class WebhookResponse(BaseModel):
    """Response after webhook processing."""
    notification_id: int
    status: str
    delivered_to: list[str]


class NotificationResponse(BaseModel):
    """Single notification in list response."""
    id: int
    event_type: str
    title: str
    message: str
    urgency: str
    room_name: str | None
    source: str
    status: str
    tts_delivered: bool
    enriched: bool = False
    original_message: str | None = None
    created_at: str
    delivered_at: str | None
    acknowledged_at: str | None


class NotificationListResponse(BaseModel):
    """Paginated notification list."""
    notifications: list[NotificationResponse]
    total: int
    limit: int
    offset: int


class TokenResponse(BaseModel):
    """Webhook token response."""
    token: str


# -- Suppression Schemas (Phase 2c) --

class SuppressRequest(BaseModel):
    """Request to suppress similar notifications."""
    reason: str | None = Field(None, max_length=255)


class SuppressionResponse(BaseModel):
    """Single suppression rule."""
    id: int
    event_pattern: str
    reason: str | None
    is_active: bool
    created_at: str


class SuppressionListResponse(BaseModel):
    """List of suppression rules."""
    suppressions: list[SuppressionResponse]


# -- Reminder Schemas --

class ReminderRequest(BaseModel):
    """Request to create a reminder."""
    message: str = Field(..., min_length=1, max_length=5000)
    trigger_at: str = Field(..., description="ISO datetime or relative like 'in 30 Minuten'")
    room: str | None = Field(None, max_length=100)
    session_id: str | None = Field(None, max_length=255)


class ReminderResponse(BaseModel):
    """Single reminder."""
    id: int
    message: str
    trigger_at: str
    room_name: str | None
    status: str
    created_at: str
    fired_at: str | None


class ReminderListResponse(BaseModel):
    """List of reminders."""
    reminders: list[ReminderResponse]
