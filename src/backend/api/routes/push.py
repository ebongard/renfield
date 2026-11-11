"""
Web Push API routes (#79) -- one browser's subscription for the logged-in user.

Endpoints:
- GET    /vapid-key      -- the public VAPID key the browser subscribes with
- GET    /subscriptions  -- the caller's own subscriptions
- POST   /subscribe      -- bind this browser to the caller
- DELETE /subscribe      -- remove the caller's subscription for this browser

Mounted only when ``PUSH_NOTIFICATIONS_ENABLED`` is set (``main.py``). While a
precondition is missing (``services/push_preconditions.py``) every route
answers the 404 of a path that does not exist -- before authentication and
before input validation, so a dark instance reveals nothing.

The owner is ALWAYS the authenticated user. No route takes a user id, and one
in the body is ignored: a subscription can only be listed or removed by the
account it belongs to.
"""

from __future__ import annotations

import base64
import binascii

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import User
from models.permissions import Permission
from services import push_service
from services.api_rate_limiter import limiter
from services.auth_service import require_permission
from services.database import get_db
from services.push_preconditions import push_active, vapid_public_key
from utils.config import settings


def require_push_active() -> None:
    """Answer as an absent route unless Web Push is fully configured."""
    if not push_active():
        raise HTTPException(status_code=404, detail="Not Found")


# The router-level dependency runs first: 404 beats 401, 403 and 422.
router = APIRouter(dependencies=[Depends(require_push_active)])

# The same permission that names "may view notifications". A role without it
# (Gast, the kiosk device account) gets no push.
_require_user = require_permission(Permission.NOTIFICATIONS_VIEW)


def _owner_id(user: User | None) -> int:
    """Fail closed: push is only active with auth on, so there is always a
    user here -- and if there ever is not, nothing is stored without one."""
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user.id


def _decodes_to(value: str, length: int) -> bool:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError):
        return False
    return len(raw) == length


class SubscriptionKeys(BaseModel):
    p256dh: str = Field(min_length=1, max_length=255)
    auth: str = Field(min_length=1, max_length=255)

    @field_validator("p256dh")
    @classmethod
    def _p256dh_is_a_public_key(cls, value: str) -> str:
        if not _decodes_to(value, 65):
            raise ValueError("invalid_p256dh")
        return value

    @field_validator("auth")
    @classmethod
    def _auth_is_a_secret(cls, value: str) -> str:
        if not _decodes_to(value, 16):
            raise ValueError("invalid_auth")
        return value


class SubscribeRequest(BaseModel):
    """What ``PushSubscription.toJSON()`` yields. Deliberately no user id."""

    endpoint: str = Field(min_length=1, max_length=2048)
    keys: SubscriptionKeys


class UnsubscribeRequest(BaseModel):
    endpoint: str = Field(min_length=1, max_length=2048)


class VapidKeyResponse(BaseModel):
    public_key: str


class SubscriptionResponse(BaseModel):
    id: int
    endpoint: str
    device_label: str | None
    created_at: str


class SubscriptionListResponse(BaseModel):
    subscriptions: list[SubscriptionResponse]


def _to_response(row) -> SubscriptionResponse:
    return SubscriptionResponse(
        id=row.id,
        endpoint=row.endpoint,
        device_label=row.device_label,
        created_at=row.created_at.isoformat() if row.created_at else "",
    )


@router.get("/vapid-key", response_model=VapidKeyResponse)
@limiter.limit(settings.api_rate_limit_default)
async def get_vapid_key(
    request: Request,
    user: User | None = Depends(_require_user),
) -> VapidKeyResponse:
    """The application server key for ``pushManager.subscribe``."""
    _owner_id(user)
    return VapidKeyResponse(public_key=vapid_public_key())


@router.get("/subscriptions", response_model=SubscriptionListResponse)
@limiter.limit(settings.api_rate_limit_default)
async def list_subscriptions(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(_require_user),
) -> SubscriptionListResponse:
    """The caller's own subscriptions -- never anyone else's."""
    rows = await push_service.list_subscriptions(db, _owner_id(user))
    return SubscriptionListResponse(subscriptions=[_to_response(row) for row in rows])


@router.post("/subscribe", response_model=SubscriptionResponse, status_code=201)
@limiter.limit(settings.api_rate_limit_default)
async def subscribe(
    request: Request,
    body: SubscribeRequest,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(_require_user),
) -> SubscriptionResponse:
    """Bind this browser's subscription to the caller (idempotent)."""
    owner_id = _owner_id(user)
    if not push_service.endpoint_allowed(body.endpoint):
        # A code, not a sentence: the UI translates it.
        raise HTTPException(status_code=422, detail="endpoint_not_allowed")
    try:
        row = await push_service.save_subscription(
            db,
            user_id=owner_id,
            endpoint=body.endpoint,
            p256dh=body.keys.p256dh,
            auth=body.keys.auth,
            device_label=request.headers.get("user-agent"),
        )
    except push_service.SubscriptionLimitReached:
        raise HTTPException(status_code=409, detail="subscription_limit_reached") from None
    return _to_response(row)


@router.delete("/subscribe", status_code=204)
@limiter.limit(settings.api_rate_limit_default)
async def unsubscribe(
    request: Request,
    body: UnsubscribeRequest,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(_require_user),
) -> Response:
    """Remove the caller's subscription for this browser."""
    removed = await push_service.delete_subscription(
        db, user_id=_owner_id(user), endpoint=body.endpoint
    )
    if not removed:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return Response(status_code=204)
