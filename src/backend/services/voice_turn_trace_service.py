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

The BROWSER path (``source="browser"``, ``schedule_browser_voice_turn``): the
chat message a voice turn produces carries a content-free ``voice_turn`` object
(end reason, recording length, the client-measured gap from the end of the
previous answer's playback to this recording's start, and a random per-page-load
``turn_key``). The row is written when that message arrives, i.e. at the end of
the recording; ``room`` stays NULL and the turn key is the ``device_key``. The
server does not know when a browser answer finished playing, so the resume
decision uses the client's gap instead of ``finished_at``: a gap of at most
``RESUME_WINDOW_SECONDS`` marks the latest earlier trace with the same turn key
as resumed -- the same "previous turn was resumed" meaning as a satellite, so
the two rates compare. A reload is a new key, so a resume never crosses page
loads, and the key names a page, never a person.

This is a known NOISY proxy: a quick follow-up question also counts as
resumed. That noise is the point of measuring -- the rate gives a baseline to
compare against once the end-of-turn decision changes.
"""
from __future__ import annotations

import asyncio
import io
import math
import re
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
#: ``session_end_reason``), plus the browser's: the voice-server's
#: ``server_vad`` / ``max_utterance`` / ``client_flush``, the last refined by
#: the client to ``silence`` (its VAD) or ``button`` (a manual stop). Anything
#: else is stored as ``other``, so a client cannot fill the column with free text.
REASONS = frozenset({
    "silence", "timeout", "button", "canceled", "unknown",
    "expired", "disconnect", "orphaned",
    "server_vad", "max_utterance", "client_flush",
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

#: Bound on every client-supplied number (seconds); outside it is stored as NULL.
_BROWSER_SECONDS_MAX = 3600.0
#: The browser's per-page-load turn key: a random id, nothing else accepted.
_TURN_KEY_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")

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
    resume_gap_s: float | None = None,
) -> None:
    """Persist one finished turn and mark the device's previous turn as resumed.

    Two ways to find a resume. A satellite passes ``started_at``: the server
    knows when its previous answer finished. The browser passes
    ``resume_gap_s``, the client-measured gap from the end of the previous
    answer's playback to this recording's start (0 = barge-in): at most
    ``RESUME_WINDOW_SECONDS`` marks the latest earlier trace with the same
    ``device_key`` (the per-page-load turn key) as resumed.

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
            if device and resume_gap_s is not None:
                if resume_gap_s <= RESUME_WINDOW_SECONDS:
                    previous_id = (await db.execute(
                        select(VoiceTurnTrace.id)
                        .where(
                            VoiceTurnTrace.source == source,
                            VoiceTurnTrace.device_key == device,
                            VoiceTurnTrace.created_at < recording_end,
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
            elif device and started_at is not None:
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


def _bounded_seconds(value: object) -> float | None:
    """A client-supplied number of seconds in [0, 3600], else None. A JSON
    boolean is not a number here, nor NaN/inf."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value) or not 0.0 <= value <= _BROWSER_SECONDS_MAX:
        return None
    return value


def browser_turn_fields(raw: object) -> dict | None:
    """The chat message's ``voice_turn`` object -> ``record_voice_turn`` fields.

    Every value is a client-supplied METRIC, never identity: the reason is
    clamped to ``REASONS``, the numbers bounded, the turn key accepted only as
    a short random id. Extra fields are ignored. Not an object -> None (no
    trace). ``room`` stays NULL; no user id is ever derived.
    """
    if not isinstance(raw, dict):
        return None
    key = raw.get("turn_key")
    return {
        "source": "browser",
        "device_key": key if isinstance(key, str) and _TURN_KEY_RE.match(key) else None,
        "room": None,
        "reason": clamp_reason(raw.get("end_reason")),
        "audio_seconds": _bounded_seconds(raw.get("audio_duration_s")),
        "resume_gap_s": _bounded_seconds(raw.get("gap_since_answer_s")),
    }


def schedule_browser_voice_turn(raw: object) -> asyncio.Task | None:
    """Fire-and-forget trace of a browser voice turn; never raises.

    Flag off = returns before even looking at ``raw``: no task, no work.
    """
    if not settings.voice_turn_traces_enabled:
        return None
    try:
        fields = browser_turn_fields(raw)
        return schedule_voice_turn(**fields) if fields is not None else None
    except Exception as e:
        logger.warning(f"Browser voice turn trace not scheduled: {error_text_without_content(e)}")
        return None


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

    resumed_by_source = {
        source: count for source, count in (await db.execute(
            select(VoiceTurnTrace.source, func.count(VoiceTurnTrace.id))
            .where(in_window, VoiceTurnTrace.resumed.is_(True))
            .group_by(VoiceTurnTrace.source)
        )).all()
    }
    resumed = sum(resumed_by_source.values())
    totals_by_source = {source: sum(reasons.values()) for source, reasons in by_source_reason.items()}

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
        # Per source, so a satellite rate compares with a browser rate.
        "resumed_rate_by_source": {
            source: round(resumed_by_source.get(source, 0) / count, 4)
            for source, count in totals_by_source.items()
        },
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
