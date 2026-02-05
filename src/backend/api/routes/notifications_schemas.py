"""
Pydantic Request/Response Schemas for Notification API
"""

from pydantic import BaseModel, Field, field_validator

VALID_URGENCIES = {"critical", "info", "low"}


class WebhookRequest(BaseModel):
    """Incoming webhook payload from HA automation."""
    event_type: str = Field(..., min_length=1, max_length=100)
    title: str = Field(..., min_length=1, max_length=255)
    message: str = Field(..., min_length=1, max_length=5000)
    urgency: str = Field(default="info", max_length=20)
    room: str | None = Field(None, max_length=100)
    tts: bool | None = None
    data: dict | None = None

    @field_validator("urgency")
    @classmethod
    def validate_urgency(cls, v: str) -> str:
        if v not in VALID_URGENCIES:
            raise ValueError(f"urgency must be one of {VALID_URGENCIES}")
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
