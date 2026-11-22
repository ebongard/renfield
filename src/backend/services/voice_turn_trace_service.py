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

- A turn has three times, all wall clock: its START (``started_at``; for a
  satellite ``SatelliteSession.started_at``, i.e. the wake word), the end of
  its RECORDING (``created_at``; stamped at ``audio_end``) and its FINISH
  (``finished_at``): the later of the session end and the EXPECTED end of the
  answer's playback (TTS send time + the WAV's duration). The session ends
  right after the TTS is sent, and the satellite re-arms its wake word while
  it still plays, so the session end alone would make a resume depend on the
  answer's length. A turn without an ``audio_end`` (expired while listening, a
  disconnect) has ``created_at == finished_at`` = the session end.
- The trace is written at the session END, not at ``audio_end``: a satellite
  can only start its next session after the previous one ended
  (``start_session`` refuses while ``current_session_id`` is set, and the
  answer runs inline). Measured from ``audio_end``, a resume would depend on
  how fast the answer was -- it would measure LLM latency, not cut-offs.
- When a turn is recorded, the latest earlier trace P of the SAME source and
  ``device_key`` with ``P.created_at < start <= P.finished_at +
  RESUME_WINDOW_SECONDS`` is marked ``resumed=true``: the speaker woke the
  device again while the answer was still playing (a barge-in) or within
  10 s after it ended -- what a turn cut off mid-sentence looks like.

This is a known NOISY proxy: a quick follow-up question also counts as
resumed. That noise is the point of measuring -- the rate gives a baseline to
compare against once the end-of-turn decision changes.
"""
from __future__ import annotations

import asyncio
import io
import time
import wave
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

#: The turn-end reasons a device reports in ``audio_end``, plus the ones the
#: backend knows for a session that ended WITHOUT an ``audio_end`` (see
#: ``session_end_reason``). Anything else is stored as ``other``, so a device
#: cannot fill the column with free text. The browser path (a later change)
#: adds its own reasons here.
REASONS = frozenset({
    "silence", "timeout", "button", "canceled", "unknown",
    "expired", "disconnect", "orphaned",
})
OTHER_REASON = "other"

#: The backend's reason for ending a session WITHOUT an ``audio_end`` -> the
#: trace reason. ``timeout`` there is the cleanup sweep's recording limit
#: (``device_session_timeout``) on a session still LISTENING, so it is stored
#: as ``expired`` -- never confused with the device's own ``timeout`` (its
#: ``vad.max_recording_seconds`` cut the utterance).
_SESSION_END_REASONS = {"timeout": "expired", "disconnect": "disconnect", "orphaned": "orphaned"}

SOURCES = frozenset({"satellite", "browser"})

# Mirrors the String(64) columns; a longer id/room name is cut, not refused.
_NAME_MAX = 64

# Strong references to the scheduled writes: the event loop keeps only weak
# ones, so an unreferenced task can be garbage-collected mid-flight.
_background_tasks: set[asyncio.Task] = set()


def clamp_reason(reason: object) -> str:
    """The reported reason if it is in the vocabulary, else ``other``."""
    return reason if isinstance(reason, str) and reason in REASONS else OTHER_REASON


def session_end_reason(end_reason: object) -> str:
    """The trace reason for a session that ended without an ``audio_end``."""
    return _SESSION_END_REASONS.get(end_reason, "unknown") if isinstance(end_reason, str) else "unknown"


def wav_duration_seconds(data: bytes) -> float | None:
    """Duration of a WAV body from its header, or None if it is not a readable WAV."""
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            rate = wav.getframerate()
            return wav.getnframes() / rate if rate > 0 else None
    except Exception:
        return None


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
    recording_ended_at: float | None = None,
    finished_at: float | None = None,
) -> None:
    """Persist one finished turn and mark the device's previous turn as resumed.

    Times are wall-clock epoch seconds (``time.time()``). ``finished_at`` (the
    later of session end and expected playback end) defaults to now,
    ``recording_ended_at`` to ``finished_at``.

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

        finished = _naive_utc_from_epoch(finished_at) if finished_at is not None else (
            datetime.now(UTC).replace(tzinfo=None)
        )
        recording_end = (
            _naive_utc_from_epoch(recording_ended_at) if recording_ended_at is not None else finished
        )
        device = device_key[:_NAME_MAX] if device_key else None

        async with AsyncSessionLocal() as db:
            if device and started_at is not None:
                start = _naive_utc_from_epoch(started_at)
                previous_id = (await db.execute(
                    select(VoiceTurnTrace.id)
                    .where(
                        VoiceTurnTrace.source == source,
                        VoiceTurnTrace.device_key == device,
                        # Re-woken after this trace's recording ended and no
                        # later than 10 s after its answer finished playing:
                        # a barge-in during playback counts too.
                        VoiceTurnTrace.created_at < start,
                        VoiceTurnTrace.finished_at >= start - timedelta(seconds=RESUME_WINDOW_SECONDS),
                    )
                    .order_by(VoiceTurnTrace.finished_at.desc(), VoiceTurnTrace.id.desc())
                    .limit(1)
                )).scalar()
                if previous_id is not None:
                    await db.execute(
                        update(VoiceTurnTrace)
                        .where(VoiceTurnTrace.id == previous_id)
                        .values(resumed=True)
                    )
            db.add(VoiceTurnTrace(
                created_at=recording_end,
                finished_at=finished,
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
    """Fire-and-forget ``record_voice_turn``; never delays or fails the caller.

    ``finished_at`` is taken HERE, when the session ends, not when the task
    runs. Flag off = no task at all, so the voice path is unchanged.
    """
    if not settings.voice_turn_traces_enabled:
        return None
    fields.setdefault("finished_at", time.time())
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
