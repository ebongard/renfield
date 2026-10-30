"""Reports when an enrolled satellite is no longer there.

WHY THIS EXISTS
---------------
On 2026-09-26 half the fleet was dark, and NOTHING said so:

    Fitnessraum, Kinderbad, Wohnzimmer   connected
    Esszimmer                            gone for 1 day (node NotReady)
    Arbeitszimmer                        gone for 2 days
    BensZimmer                           gone for 30 DAYS

In 36 hours of notifications: exclusively `mcp_health`, not a single one about a
satellite. The MCP servers have health probes with alerting, the tasks have
failure-streak detection -- the satellites had nothing. A room stops answering,
and you find out by walking in and talking.

🛑 WHY NOT `last_authenticated_at` ALONE
----------------------------------------
That was the obvious shot and it is wrong. The column is set when the
CONNECTION IS ESTABLISHED, not continuously. Its age measures "time since the
last reconnect", not "time since the last sign of life" -- a satellite that has
been stably connected for days therefore looks like one that has been gone for
days.

Measured on 2026-09-26: immediately after a backend restart all three HEALTHY
satellites showed four minutes, because they had re-authenticated. Before the
restart the same value would have been hours old. A watchdog on this column
would therefore have reported the healthy ones and kept quiet about the dead
ones.

WHAT INSTEAD
------------
The `SatelliteManager` registry says who is connected NOW -- that is the truth
the user interface shows too. But it lives in memory and is empty after a
restart until everyone has reported back. Hence:

* **Grace period after the start.** Before `SETTLE_SECONDS` the task does NOT
  judge. Without it every rollout reported the whole fleet as dead -- a false
  alarm that makes a real report worthless.
* **Expected** are the enrolled, non-revoked, activated rows from
  `satellites`.
* **Missing** = expected minus connected. `last_authenticated_at` then only
  supplies the SINCE WHEN for the text, not the WHETHER.

🛑 ONLY A CHANGE IS REPORTED
----------------------------
The first version relied on the `dedup_key` of `notify_admin` and I claimed
that this meant it would "not be reported again every hour". That was wrong,
and operation showed it within three hours: three identical notifications at
13:59, 14:59, 15:59. The reason is in `config.py` --
`proactive_suppression_window` is **60 seconds**. That suppresses bursts, not
an hourly repetition. For BensZimmer (gone for 30 days) that would be 24
reports a day: alert fatigue by design, and thereby exactly the thing broken
that this task was built for.

That is why the watchdog remembers the last REPORTED set in `system_settings`
(the same key-value furniture `chat_upload_tool` already uses) and reports only
when it changes -- including the return: when a room comes back, that is its
own piece of good news. Every run still goes into the LOG, so the situation
stays readable without a notification.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import text

from services.database import AsyncSessionLocal

#: How long after the process start NO judgement is made. A satellite
#: reconnects within seconds of a backend restart (measured: all three healthy
#: ones within four minutes); five minutes is ample room.
SETTLE_SECONDS = 300

#: Time of the process start. Module-wide, because the task runs stateless.
_STARTED_AT = time.monotonic()

#: Key in `system_settings` holding the last REPORTED set. Durable, not in
#: memory: otherwise every restart would report again, and with an outage
#: lasting weeks that would be noise all over again.
STATE_KEY = "satellite_fleet_watchdog:last_reported"


def _uptime_seconds() -> float:
    return time.monotonic() - _STARTED_AT


async def _fetch_enrolled() -> list[tuple[str, str | None, datetime | None]]:
    """Enrolled = not revoked AND activated. Revoked devices are deliberately
    gone and must not be reported as an outage."""
    async with AsyncSessionLocal() as db:
        return list((await db.execute(text("""
            SELECT satellite_id, room, last_authenticated_at
            FROM satellites
            WHERE revoked_at IS NULL AND is_enabled = true
            ORDER BY satellite_id
        """))).all())


async def _read_last_reported() -> str:
    from models.database import SystemSetting

    async with AsyncSessionLocal() as db:
        row = await db.get(SystemSetting, STATE_KEY)
        return (row.value or "") if row else ""


async def _write_last_reported(value: str) -> None:
    from models.database import SystemSetting

    async with AsyncSessionLocal() as db:
        row = await db.get(SystemSetting, STATE_KEY)
        if row is None:
            db.add(SystemSetting(key=STATE_KEY, value=value))
        else:
            row.value = value
        await db.commit()


async def check_satellite_fleet(
    *, notify=None, manager=None, fetch_enrolled=None,
    read_state=None, write_state=None,
) -> str | None:
    """Compare enrolled against connected satellites.

    ``notify`` and ``manager`` can be injected for tests only -- without them
    neither the grace period nor the reporting path would be testable without a
    real satellite.

    Returns: one line for the run log, or None when there is nothing to report.
    The task stays silent in the normal case.
    """
    if _uptime_seconds() < SETTLE_SECONDS:
        # NO judgement. The registry is empty after a restart until everyone
        # has reported back -- reporting here would mean reporting every
        # rollout as a total outage.
        logger.debug("Flottenwache: Karenz nach dem Start, kein Urteil")
        return None

    if manager is None:
        # 🛑 The import stands in the FUNCTION BODY and behind this gate, so
        # that a platform installation without `ha_glue` never reaches it --
        # the caller in `scheduled_tasks/builtins.py` already checks the same
        # condition, here once more for direct callers. This is the reason for
        # the entry in `ALLOWED_IMPORTERS` of `test_ha_glue_boundary.py`.
        from utils.config import settings as _platform_settings

        if not _platform_settings.features.get("satellites"):
            logger.debug("Flottenwache: Satellitenmerkmal aus, kein Urteil")
            return None

        from ha_glue.services.satellite_manager import get_satellite_manager

        manager = get_satellite_manager()
    connected = set(getattr(manager, "satellites", {}) or {})

    if fetch_enrolled is None:
        fetch_enrolled = _fetch_enrolled
    rows = await fetch_enrolled()

    missing = [(sid, room, seen) for sid, room, seen in rows if sid not in connected]

    # The last REPORTED set, so that only a change becomes a message.
    read_state = read_state or _read_last_reported
    write_state = write_state or _write_last_reported
    signature = ",".join(sorted(sid for sid, _, _ in missing))
    previous = await read_state()
    changed = signature != previous

    if not missing:
        if changed:
            # A return is its own piece of good news -- and the marker MUST be
            # cleared, otherwise the next outage would stay silent.
            logger.info("🛰 Flottenwache: alle Satelliten wieder verbunden")
            await _try_notify(
                notify, title="Alle Satelliten wieder verbunden",
                message="Es fehlt kein Satellit mehr.",
                dedup_key=f"{STATE_KEY}:clear",
            )
            await write_state("")
        return None

    now = datetime.now(UTC).replace(tzinfo=None)
    parts: list[str] = []
    for sid, room, seen in missing:
        if seen is None:
            parts.append(f"{room or sid} (nie verbunden)")
            continue
        hours = int((now - seen).total_seconds() // 3600)
        parts.append(f"{room or sid} (seit {hours} h)" if hours < 48
                     else f"{room or sid} (seit {hours // 24} Tagen)")

    summary = ", ".join(parts)
    line = (f"🛰 Flottenwache: {len(missing)} von {len(rows)} Satelliten nicht "
            f"verbunden — {summary}")
    # EVERY run goes into the LOG: the situation is meant to be readable
    # without a notification. Only the urgency distinguishes unchanged from
    # new.
    (logger.warning if changed else logger.info)(
        line if changed else line + " (unveraendert, keine neue Meldung)")

    if changed:
        await _try_notify(
            notify, title=f"{len(missing)} Satellit(en) offline",
            # Room names are not private CONTENT -- they appear the same way in
            # the device administration. No conversation and no document goes
            # along.
            message=f"Nicht verbunden: {summary}.",
            dedup_key=f"satellite_fleet_offline:{signature}",
        )
        await write_state(signature)

    return f"offline: {summary}" + ("" if changed else " (unveraendert)")


async def _try_notify(notify, *, title: str, message: str, dedup_key: str) -> None:
    """ONE report to the owner. A failure here must never kill the task -- the
    situation is in the log anyway."""
    if notify is None:
        from services import ops_alert

        notify = ops_alert.notify_admin
    try:
        await notify(title=title, message=message, dedup_key=dedup_key,
                     event_type="satellite_health",
                     source="satellite_fleet_watchdog")
    except Exception as exc:  # pragma: no cover - a report must never disturb
        logger.warning(f"Flottenwache: Benachrichtigung fehlgeschlagen: {exc}")
