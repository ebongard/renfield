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
   new problem) still gets through.

The ledger is deliberately in-process: it is a *rate limiter*, not a record. A
pod restart re-arming an alert for a problem that is still broken is the safe
direction to fail. Callers that need restart-durable "already told them" state
keep their own column (see ``ScheduledTask.error_alerted_at``).

Consumers: ``mcp_health_monitor`` (MCP fleet), ``scheduled_tasks.engine``
(repeatedly failing jobs), ``watchdog`` (unreachable HTTP targets).
"""
from __future__ import annotations

import time
from typing import Any

from loguru import logger

from utils.config import settings

# Issue-key → last-alert monotonic time.
_alerted: dict[str, float] = {}


def should_alert(key: str, *, realert_seconds: float | None = None) -> bool:
    """True if this issue-key hasn't been alerted, or its re-alert TTL elapsed.

    Marks the key as alerted as a side effect, so a caller that then fails to
    deliver simply waits out the TTL rather than spinning.
    """
    ttl = settings.mcp_health_realert_seconds if realert_seconds is None else realert_seconds
    now = time.monotonic()
    last = _alerted.get(key)
    if last is None or (now - last) >= ttl:
        _alerted[key] = now
        return True
    return False


def clear_alert(key: str) -> None:
    """Forget an issue-key — call on recovery so a re-failure alerts promptly."""
    _alerted.pop(key, None)


def alerted_keys(prefix: str = "") -> list[str]:
    """The ledger's current keys (optionally filtered), for recovery sweeps."""
    return [k for k in _alerted if k.startswith(prefix)]


def reset_ledger() -> None:
    """Test hook — drop all ledger state."""
    _alerted.clear()


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


async def notify_admin(
    *,
    title: str,
    message: str,
    dedup_key: str,
    data: dict[str, Any] | None = None,
    event_type: str = "ops_health",
    source: str = "ops_alert",
    urgency: str = "critical",
) -> None:
    """Fire ONE privacy-aware proactive notification to the admin/owner.

    Best-effort by design: a dedup/suppression ``ValueError`` or any failure is
    swallowed so a broken notification pipeline never breaks the caller's tick.
    No-op when ``PROACTIVE_ENABLED`` is off (there is no delivery path then).
    """
    if not settings.proactive_enabled:
        return
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
        pass  # deduped / suppressed by the notification pipeline
    except Exception as e:  # noqa: BLE001
        logger.warning(f"ops_alert: notify failed for {dedup_key}: {e}")
