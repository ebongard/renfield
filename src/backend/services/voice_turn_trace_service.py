"""
Voice turn traces (#1613) -- one CONTENT-FREE row per voice turn end.

A turn ends on silence today, and nobody could see how often a turn is cut off
or why turns end: the satellite history that carries ``turn_end_reason`` lives
in memory, and the cluster has no Prometheus, so an in-process counter would
reset on every deploy. This module persists one row per turn end, following the
routing-trace pattern (``routing_trace_service``): written by
``record_voice_turn`` while ``VOICE_TURN_TRACES_ENABLED`` is on, read through
``turn_stats`` by ``GET /api/admin/voice-turns/stats``, purged after
``VOICE_TURN_TRACES_RETENTION_DAYS`` by the ``voice_turn_trace_cleanup``
built-in task (which runs with the flag off too).

The row holds NO user, NO text and NO audio: the source, the device id and its
room (both device configuration, not a person), the device's turn-end reason
clamped to ``REASONS``, the recording length, the device's own VAD verdict and
the ``resumed`` flag.

Times, and what "resumed" means:

- A trace's ``created_at`` is the turn's END: it is stamped at ``audio_end``
  (``schedule_voice_turn`` takes the time before the task runs).
- A turn's START is the session start the caller passes (``started_at``, wall
  clock seconds; for a satellite ``SatelliteSession.started_at``, i.e. the
  wake word).
- When a turn is recorded, the latest earlier trace of the SAME source and
  ``device_key`` whose END lies within ``RESUME_WINDOW_SECONDS`` before the new
  turn's START is marked ``resumed=true``: the speaker came back right away,
  which is what a turn cut off mid-sentence looks like.

This is a known NOISY proxy: a quick follow-up question also counts as
resumed. That noise is the point of measuring -- the rate gives a baseline to
compare against once the end-of-turn decision changes.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import VoiceTurnTrace
from utils.config import settings
from utils.log_safety import error_text_without_content

#: A new turn on the same device starting this soon after the previous turn's
#: END marks the previous turn as resumed.
RESUME_WINDOW_SECONDS = 10

#: The turn-end reasons a device reports in ``audio_end``. Anything else is
#: stored as ``other``, so a device cannot fill the column with free text.
#: The browser path (a later change) adds its own reasons here.
REASONS = frozenset({"silence", "timeout", "button", "canceled", "unknown"})
OTHER_REASON = "other"

SOURCES = frozenset({"satellite", "browser"})

# Mirrors the String(64) columns; a longer id/room name is cut, not refused.
_NAME_MAX = 64

# Strong references to the scheduled writes: the event loop keeps only weak
# ones, so an unreferenced task can be garbage-collected mid-flight.
_background_tasks: set[asyncio.Task] = set()


def clamp_reason(reason: object) -> str:
    """The reported reason if it is in the vocabulary, else ``other``."""
    return reason if isinstance(reason, str) and reason in REASONS else OTHER_REASON


def _naive_utc_from_epoch(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, UTC).replace(tzinfo=None)


async def record_voice_turn(
    *,
    source: str,
    device_key: str | None,
    room: str | None,
    reason: object,
    audio_seconds: float | None = None,
    vad_speech_seen: bool | None = None,
    started_at: float | None = None,
    ended_at: datetime | None = None,
) -> None:
    """Persist one turn end and mark the device's previous turn as resumed.

    Re-asserts the flag so a caller that scheduled it never writes once the
    switch is off. Uses its own DB session. Best-effort: a failed write is
    logged (content-free), never raised into the caller.
    """
    if not settings.voice_turn_traces_enabled:
        return
    if source not in SOURCES:
        logger.warning("Voice turn trace not recorded: unknown source")
        return
    try:
        from services.database import AsyncSessionLocal

        end = ended_at or datetime.now(UTC).replace(tzinfo=None)
        device = device_key[:_NAME_MAX] if device_key else None

        async with AsyncSessionLocal() as db:
            if device and started_at is not None:
                start = _naive_utc_from_epoch(started_at)
                previous_id = (await db.execute(
                    select(VoiceTurnTrace.id)
                    .where(
                        VoiceTurnTrace.source == source,
                        VoiceTurnTrace.device_key == device,
                        VoiceTurnTrace.created_at >= start - timedelta(seconds=RESUME_WINDOW_SECONDS),
                        VoiceTurnTrace.created_at < end,
                    )
                    .order_by(VoiceTurnTrace.created_at.desc(), VoiceTurnTrace.id.desc())
                    .limit(1)
                )).scalar()
                if previous_id is not None:
                    await db.execute(
                        update(VoiceTurnTrace)
                        .where(VoiceTurnTrace.id == previous_id)
                        .values(resumed=True)
                    )
            db.add(VoiceTurnTrace(
                created_at=end,
                source=source,
                device_key=device,
                room=room[:_NAME_MAX] if room else None,
                reason=clamp_reason(reason),
                audio_seconds=float(audio_seconds) if audio_seconds is not None else None,
                vad_speech_seen=vad_speech_seen if isinstance(vad_speech_seen, bool) else None,
                resumed=False,
            ))
            await db.commit()
    except Exception as e:
        logger.warning(f"Voice turn trace not recorded: {error_text_without_content(e)}")


def _log_task_failure(task: asyncio.Task) -> None:
    _background_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning(f"Voice turn trace task failed: {error_text_without_content(exc)}")


def schedule_voice_turn(**fields) -> asyncio.Task | None:
    """Fire-and-forget ``record_voice_turn``; never delays or fails the turn.

    The END time is taken HERE, at ``audio_end``, not when the task runs.
    Flag off = no task at all, so the voice path is unchanged.
    """
    if not settings.voice_turn_traces_enabled:
        return None
    fields.setdefault("ended_at", datetime.now(UTC).replace(tzinfo=None))
    task = asyncio.create_task(record_voice_turn(**fields))
    _background_tasks.add(task)
    task.add_done_callback(_log_task_failure)
    return task


async def turn_stats(db: AsyncSession, days: int) -> dict:
    """Aggregates over the last ``days`` days. Counts and percentiles only."""
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)
    in_window = VoiceTurnTrace.created_at >= cutoff

    by_source_reason: dict[str, dict[str, int]] = {}
    total = 0
    for source, reason, count in (await db.execute(
        select(VoiceTurnTrace.source, VoiceTurnTrace.reason, func.count(VoiceTurnTrace.id))
        .where(in_window)
        .group_by(VoiceTurnTrace.source, VoiceTurnTrace.reason)
        .order_by(func.count(VoiceTurnTrace.id).desc())
    )).all():
        by_source_reason.setdefault(source, {})[reason] = count
        total += count

    resumed = (await db.execute(
        select(func.count(VoiceTurnTrace.id)).where(in_window, VoiceTurnTrace.resumed.is_(True))
    )).scalar() or 0

    p50, p90 = (await db.execute(
        select(
            func.percentile_cont(0.5).within_group(VoiceTurnTrace.audio_seconds.asc()),
            func.percentile_cont(0.9).within_group(VoiceTurnTrace.audio_seconds.asc()),
        ).where(in_window, VoiceTurnTrace.audio_seconds.is_not(None))
    )).one()

    by_room = {
        room: count for room, count in (await db.execute(
            select(VoiceTurnTrace.room, func.count(VoiceTurnTrace.id))
            .where(in_window, VoiceTurnTrace.room.is_not(None))
            .group_by(VoiceTurnTrace.room)
            .order_by(func.count(VoiceTurnTrace.id).desc())
        )).all()
    }

    return {
        "days": days,
        "total": total,
        "by_source_reason": by_source_reason,
        "resumed": resumed,
        "resumed_rate": round(resumed / total, 4) if total else None,
        "audio_seconds_p50": float(p50) if p50 is not None else None,
        "audio_seconds_p90": float(p90) if p90 is not None else None,
        "by_room": by_room,
    }


async def purge_expired(db: AsyncSession) -> int:
    """Delete rows older than the retention window; returns how many."""
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
        days=settings.voice_turn_traces_retention_days,
    )
    result = await db.execute(delete(VoiceTurnTrace).where(VoiceTurnTrace.created_at < cutoff))
    await db.commit()
    return result.rowcount or 0
