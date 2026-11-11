"""Web Push (#79): subscription storage, recipient selection and the sender.

Design note: ``docs/design/web-push.md``.

Three rules carry this module:

* **Who gets a push is the in-app rule, not a second one.** A subscription's
  owner receives a push for a notification exactly when
  ``NotificationService.is_visible_to(notification, owner_id)`` holds -- the
  rule that scopes the notification list and the acknowledgements since #1244.
  The admin's "sees everything" oversight is NOT a delivery: an admin is pushed
  only what is addressed to them or public, like anyone else.
* **The payload is content-free.** A push leaves the house through a third
  party's push service. It carries the notification id and the urgency, never
  the title or the text; the browser shows a generic line and fetches the
  content from this instance, where the request is authenticated and filtered
  again.
* **A push never touches the in-app delivery.** ``schedule_push`` only creates
  a task; the task opens its own session, and the blocking HTTP call of
  ``pywebpush`` runs in a worker thread under a timeout. Whatever happens in
  there is logged and dropped.

``pywebpush`` is imported inside the sender, so this module imports without it.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from loguru import logger
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import PushSubscription, User
from services.push_preconditions import push_active, vapid_private_key, vapid_subject
from utils.config import settings

#: One account, this many browsers. Every row is one outbound request per
#: notification, so the count is bounded.
MAX_SUBSCRIPTIONS_PER_USER = 20

#: The push service answers these when a subscription is expired or revoked
#: (RFC 8030 section 7.3). The row is useless from then on.
_GONE_STATUS = frozenset({404, 410})

#: Added to the HTTP timeout for the limit around the worker thread: covers
#: what the HTTP timeout does not (DNS, a stalled TLS handshake).
_OUTER_TIMEOUT_MARGIN_SECONDS = 5.0

_SENT = "sent"
_GONE = "gone"
_FAILED = "failed"

# Strong references: the event loop keeps only weak ones to a running task.
_tasks: set[asyncio.Task] = set()


class SubscriptionLimitReached(Exception):
    """The account already holds ``MAX_SUBSCRIPTIONS_PER_USER`` devices."""


@dataclass(frozen=True)
class PushTarget:
    """What the sender needs from a notification, detached from its session.

    The task outlives the request that created the notification, so it must
    not hold the ORM object. Carries the two fields ``is_visible_to`` reads.
    """

    notification_id: int
    target_user_id: int | None
    privacy: str | None
    urgency: str | None


# ---------------------------------------------------------------------------
# Endpoint allowlist
# ---------------------------------------------------------------------------


def endpoint_allowed(endpoint: str) -> bool:
    """Whether the backend may POST to this subscription endpoint.

    The URL comes from a browser and is requested by the server: without a
    restriction any account could aim the backend at an address inside the
    network. Only https, no credentials, default port, and a host of a known
    push service (``PUSH_ALLOWED_ENDPOINT_HOSTS``).
    """
    try:
        parts = urlsplit(endpoint)
        port = parts.port
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or parts.username or parts.password:
        return False
    if port not in (None, 443):
        return False
    for entry in settings.push_allowed_endpoint_hosts.split(","):
        entry = entry.strip().lower()
        if not entry:
            continue
        if entry.startswith("."):
            if host.endswith(entry):
                return True
        elif host == entry:
            return True
    return False


# ---------------------------------------------------------------------------
# Storage -- every function takes the owner from the caller's session
# ---------------------------------------------------------------------------


async def list_subscriptions(db: AsyncSession, user_id: int) -> list[PushSubscription]:
    result = await db.execute(
        select(PushSubscription)
        .where(PushSubscription.user_id == user_id)
        .order_by(PushSubscription.created_at.desc(), PushSubscription.id.desc())
    )
    return list(result.scalars().all())


async def save_subscription(
    db: AsyncSession,
    *,
    user_id: int,
    endpoint: str,
    p256dh: str,
    auth: str,
    device_label: str | None,
) -> PushSubscription:
    """Bind this browser to ``user_id``.

    One row per endpoint: subscribing again refreshes the keys, and a
    different account on the same browser takes the row over, so the device
    stops receiving the previous account's pushes.
    """
    existing = (
        await db.execute(
            select(PushSubscription.user_id).where(PushSubscription.endpoint == endpoint)
        )
    ).scalar_one_or_none()
    if existing != user_id:
        held = (
            await db.execute(
                select(func.count(PushSubscription.id)).where(PushSubscription.user_id == user_id)
            )
        ).scalar_one()
        if held >= MAX_SUBSCRIPTIONS_PER_USER:
            raise SubscriptionLimitReached

    values = {
        "user_id": user_id,
        "p256dh": p256dh,
        "auth": auth,
        "device_label": (device_label or None) and device_label[:255],
    }
    statement = (
        pg_insert(PushSubscription)
        .values(endpoint=endpoint, **values)
        .on_conflict_do_update(index_elements=[PushSubscription.endpoint], set_=values)
        .returning(PushSubscription.id)
    )
    row_id = (await db.execute(statement)).scalar_one()
    await db.commit()
    return (
        await db.execute(
            select(PushSubscription)
            .where(PushSubscription.id == row_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def delete_subscription(db: AsyncSession, *, user_id: int, endpoint: str) -> bool:
    """Remove the caller's OWN subscription. Another account's row is reported
    as missing, exactly like an endpoint that was never stored."""
    result = await db.execute(
        delete(PushSubscription).where(
            PushSubscription.user_id == user_id,
            PushSubscription.endpoint == endpoint,
        )
    )
    await db.commit()
    return bool(result.rowcount)


# ---------------------------------------------------------------------------
# Recipients and payload
# ---------------------------------------------------------------------------


async def select_push_recipients(db: AsyncSession, notification: Any) -> list[PushSubscription]:
    """The subscriptions whose owner the in-app notification reaches.

    ``NotificationService.is_visible_to`` decides, row by row; the SQL only
    narrows the candidates (active accounts, and the addressee when there is
    one). ``notification`` needs ``target_user_id`` and ``privacy``.
    """
    from services.notification_service import NotificationService

    query = (
        select(PushSubscription)
        .join(User, User.id == PushSubscription.user_id)
        .where(User.is_active.is_(True))
        .order_by(PushSubscription.id)
    )
    if notification.target_user_id is not None:
        query = query.where(PushSubscription.user_id == notification.target_user_id)
    rows = (await db.execute(query)).scalars().all()
    return [row for row in rows if NotificationService.is_visible_to(notification, row.user_id)]


def build_payload(target: PushTarget) -> dict[str, Any]:
    """What leaves the house: an id and the urgency. No title, no text, no
    event type (caller-chosen free text), no room, no name."""
    return {
        "v": 1,
        "kind": "notification",
        "notification_id": target.notification_id,
        "urgency": target.urgency if target.urgency in ("critical", "info", "low") else "info",
    }


# ---------------------------------------------------------------------------
# Sender
# ---------------------------------------------------------------------------


def _send_blocking(subscription_info: dict[str, Any], data: str, timeout: float) -> None:
    """One push, synchronously. Runs in a worker thread -- ``pywebpush`` uses
    ``requests``. Imported here so the module loads without the package."""
    from pywebpush import webpush

    webpush(
        subscription_info=subscription_info,
        data=data,
        vapid_private_key=vapid_private_key(),
        # A fresh dict per call: pywebpush adds ``aud`` and ``exp`` to it.
        vapid_claims={"sub": vapid_subject()},
        ttl=int(settings.proactive_notification_ttl),
        timeout=timeout,
    )


async def _send_one(subscription_id: int, subscription_info: dict[str, Any], data: str) -> str:
    timeout = settings.push_send_timeout_seconds
    try:
        # The thread itself cannot be cancelled; it ends with the library's
        # own timeout. The outer limit only stops this task from waiting on it.
        await asyncio.wait_for(
            asyncio.to_thread(_send_blocking, subscription_info, data, timeout),
            timeout=timeout + _OUTER_TIMEOUT_MARGIN_SECONDS,
        )
        return _SENT
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in _GONE_STATUS:
            return _GONE
        # Never the endpoint (a capability URL) and never the exception text
        # (it quotes the push service's answer): id, type and status only.
        logger.warning(
            f"Web Push to subscription #{subscription_id} failed: "
            f"{type(e).__name__} (status={status})"
        )
        return _FAILED


async def deliver_push(target: PushTarget) -> dict[str, int]:
    """Send one content-free push per recipient subscription; drop dead rows."""
    counts = {_SENT: 0, _GONE: 0, _FAILED: 0}
    if not push_active():
        return counts

    from services.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        recipients = [
            (row.id, {"endpoint": row.endpoint, "keys": {"p256dh": row.p256dh, "auth": row.auth}})
            for row in await select_push_recipients(db, target)
            # Stored before the allowlist was narrowed: not requested any more.
            if endpoint_allowed(row.endpoint)
        ]
    if not recipients:
        return counts

    data = json.dumps(build_payload(target), separators=(",", ":"))
    outcomes = await asyncio.gather(
        *(_send_one(row_id, info, data) for row_id, info in recipients)
    )
    gone = [row_id for (row_id, _info), outcome in zip(recipients, outcomes, strict=True) if outcome == _GONE]
    for outcome in outcomes:
        counts[outcome] += 1

    if gone:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(PushSubscription).where(PushSubscription.id.in_(gone)))
            await db.commit()
    logger.info(
        f"Web Push for notification #{target.notification_id}: "
        f"sent={counts[_SENT]} gone={counts[_GONE]} failed={counts[_FAILED]}"
    )
    return counts


async def _deliver_push_guarded(target: PushTarget) -> None:
    try:
        await deliver_push(target)
    except Exception as e:
        logger.warning(
            f"Web Push for notification #{target.notification_id} failed: {type(e).__name__}"
        )


def schedule_push(notification: Any) -> None:
    """Hand the notification to the push sender WITHOUT waiting for it.

    Called from the one place notifications are delivered
    (``NotificationService._deliver``). Returns at once and never raises: with
    Web Push inactive it does nothing at all, otherwise it starts a task.
    """
    try:
        if not push_active():
            return
        target = PushTarget(
            notification_id=notification.id,
            target_user_id=notification.target_user_id,
            privacy=notification.privacy,
            urgency=notification.urgency,
        )
        task = asyncio.get_running_loop().create_task(_deliver_push_guarded(target))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    except Exception as e:
        logger.warning(f"Web Push could not be scheduled: {type(e).__name__}")
