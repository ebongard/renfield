"""Shared operator-alert path — ONE way for a backend subsystem to tell the
owner admin that something is broken.

Extracted from ``mcp_health_monitor`` (2026-09-12) because three subsystems now
need exactly the same three things and must not each grow their own version:

1. **Who to tell** — the owner admin (lowest-id active user whose role grants
   ADMIN), falling back to the first user. ``None`` on an empty users table
   (auth-off single-user install), which is the correct un-scoped case.
2. **How to tell them** — ONE privacy-aware proactive notification via
   ``NotificationService.process_webhook``. Best-effort: a dedup/suppression
   ``ValueError`` or any other failure is swallowed, because an alert that
   breaks its caller is worse than a missed alert.
3. **How often** — a per-issue in-process ledger with a re-alert TTL, so an
   ONGOING problem alerts once rather than every tick, while a recurrence (or a
   new problem) still gets through. An attempt that did NOT reach the admin is
   deferred by a bounded retry backoff (``defer_alert``), never re-armed for the
   very next tick — a pipeline that keeps failing must not become an alert storm.

The ledger is deliberately in-process: it is a *rate limiter*, not a record. A
pod restart re-arming an alert for a problem that is still broken is the safe
direction to fail. Callers that need restart-durable "already told them" state
keep their own column (see ``ScheduledTask.error_alerted_at``).

Consumers: ``mcp_health_monitor`` (MCP fleet), ``scheduled_tasks.engine``
(repeatedly failing jobs), ``watchdog`` (unreachable HTTP targets).
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from utils.config import settings

# Issue-key → last-alert monotonic time.
_alerted: dict[str, float] = {}
# Issue-key → monotonic time before which a failed attempt must not be retried.
_retry_at: dict[str, float] = {}


def _now() -> float:
    """Monotonic clock — a seam so tests can move time without patching ``time``."""
    return time.monotonic()


def should_alert(key: str, *, realert_seconds: float | None = None) -> bool:
    """True if this issue-key hasn't been alerted, or its re-alert TTL elapsed.

    Marks the key as alerted as a side effect. A key deferred by ``defer_alert``
    stays closed until its retry time has passed.
    """
    ttl = settings.mcp_health_realert_seconds if realert_seconds is None else realert_seconds
    now = _now()
    retry = _retry_at.get(key)
    if retry is not None:
        if now < retry:
            return False
        _retry_at.pop(key, None)
    last = _alerted.get(key)
    if last is None or (now - last) >= ttl:
        _alerted[key] = now
        return True
    return False


def defer_alert(key: str, retry_seconds: float) -> None:
    """An attempt did not reach the admin: allow the next one only after a bounded
    backoff. Replaces the stamp ``should_alert`` set, so a real delivery later is
    not held back by the full re-alert TTL — and a pipeline that keeps failing is
    retried every ``retry_seconds``, not every tick."""
    _alerted.pop(key, None)
    _retry_at[key] = _now() + max(0.0, retry_seconds)


def clear_alert(key: str) -> None:
    """Forget an issue-key — call on recovery so a re-failure alerts promptly."""
    _alerted.pop(key, None)
    _retry_at.pop(key, None)


def alerted_keys(prefix: str = "") -> list[str]:
    """The ledger's current keys (optionally filtered), for recovery sweeps.
    Includes deferred keys, so a recovery also forgets a pending retry."""
    keys = dict.fromkeys([*_alerted, *_retry_at])
    return [k for k in keys if k.startswith(prefix)]


def reset_ledger() -> None:
    """Test hook — drop all ledger state."""
    _alerted.clear()
    _retry_at.clear()


async def resolve_admin_user_id(db) -> int | None:
    """The ops target for an alert = the owner admin (lowest-id active user
    whose role grants ADMIN), falling back to the first user by id. Returns None
    on an empty users table (auth-off single-user install — the notification then
    isn't per-user-scoped, which is correct for that mode)."""
    try:
        from sqlalchemy import select

        from models.database import User
        from services.auth_service import active_admin_ids

        admin_ids = await active_admin_ids(db)
        if admin_ids:
            return min(admin_ids)
        # No admin-granting role (auth-off / dev): fall back to the first user.
        return (
            await db.execute(select(User.id).order_by(User.id).limit(1))
        ).scalar_one_or_none()
    except Exception:  # noqa: BLE001
        return None


async def _persisted_since(
    *, title: str, message: str, source: str, target_user_id: int | None, since: datetime
) -> bool:
    """Did ``process_webhook`` commit the notification row before it failed?

    ``process_webhook`` persists the row FIRST and delivers after — a delivery or
    status-update error then raises although the admin can already see the
    notification in their list. Checked on a fresh session (the failed one may be
    unusable). Any doubt reads as "not persisted": the cost is one bounded retry,
    never a silent loss.
    """
    try:
        from sqlalchemy import select

        from models.database import Notification
        from services.database import AsyncSessionLocal

        target_clause = (
            Notification.target_user_id.is_(None)
            if target_user_id is None
            else Notification.target_user_id == target_user_id
        )
        async with AsyncSessionLocal() as db:
            found = (
                await db.execute(
                    select(Notification.id)
                    .where(
                        Notification.source == source,
                        Notification.title == title,
                        Notification.message == message,
                        Notification.created_at >= since,
                        target_clause,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
        return found is not None
    except Exception:  # noqa: BLE001
        return False


async def notify_admin(
    *,
    title: str,
    message: str,
    dedup_key: str,
    data: dict[str, Any] | None = None,
    event_type: str = "ops_health",
    source: str = "ops_alert",
    urgency: str = "critical",
) -> bool:
    """Fire ONE privacy-aware proactive notification to the admin/owner.

    Returns **whether the admin has been told** — i.e. whether a notification row
    exists for them — so a caller keeping a durable "already told them" marker only
    stamps it on a real hand-off. Without that, an alert attempted while the
    pipeline is transiently down — or while ``PROACTIVE_ENABLED`` is still off —
    would be recorded as delivered and then suppressed for the whole re-alert TTL.

    Counted as told:
    - a dedup/suppression ``ValueError`` (an equivalent notification exists);
    - a failure AFTER the row was persisted (live push/TTS or the status update
      failed, but the notification is in the admin's list). Reporting that as
      "not delivered" made the caller retry and store a new row every tick.

    Best-effort by design: nothing here raises, so a broken notification pipeline
    never breaks the caller's tick.
    """
    if not settings.proactive_enabled:
        return False
    started = datetime.now(UTC).replace(tzinfo=None)
    target: int | None = None
    try:
        from services.database import AsyncSessionLocal
        from services.notification_service import NotificationService

        # Dedicated session — process_webhook commits its own db.
        async with AsyncSessionLocal() as db:
            target = await resolve_admin_user_id(db)
            await NotificationService(db).process_webhook(
                event_type=event_type,
                title=title,
                message=message,
                urgency=urgency,
                source=source,
                privacy="personal",
                target_user_id=target,
                data={"dedup_key": dedup_key, **(data or {})},
            )
    except ValueError:
        return True  # deduped / suppressed by the pipeline — they already know
    except Exception as e:  # noqa: BLE001
        if await _persisted_since(
            title=title, message=message, source=source,
            target_user_id=target, since=started,
        ):
            logger.warning(
                f"ops_alert: {dedup_key} persisted but delivery failed ({e}) — "
                "counted as told, not retried"
            )
            return True
        logger.warning(f"ops_alert: notify failed for {dedup_key}: {e}")
        return False
    return True
