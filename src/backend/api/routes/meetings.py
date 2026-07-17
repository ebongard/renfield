"""Meetings API — §2 meeting transcription (upload → diarized transcript).

``POST /api/meetings/transcribe`` — multipart upload of a multi-speaker
recording (``consent_confirmed`` REQUIRED), streamed chunk-by-chunk to disk on
the shared uploads PVC (never whole-file-in-RAM), a ``pending`` Meeting row is
created and the audio PATH (not bytes) is enqueued to the meeting worker;
returns 202 ``{id}``. ``GET /api/meetings/{id}`` — owner-gated status poll.

Gated by ``settings.meeting_transcription_enabled`` — every route 404s when off,
so both instances stay byte-identical until the feature is turned on. Owner-
scoped when auth is on; auth-disabled single-user mode sees all (Circles-v1
pattern, mirrors projects.py). See docs/design/meeting-transcription.md.
"""
from __future__ import annotations

import os
from datetime import date as date_cls
from datetime import datetime, timedelta

import aiofiles
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Meeting, User
from services.auth_service import get_optional_user
from services.database import get_db
from services.redis_client import get_redis
from services.task_queue import MeetingTaskQueue
from utils.config import settings

router = APIRouter()

# Worker liveness key — written every 30 s by the meeting-worker pod with a 90 s
# TTL. Missing while the flag is on => 503 the upload rather than enqueue into a
# stream nobody consumes (mirrors knowledge.py::_worker_is_alive).
_MEETING_WORKER_HEARTBEAT_KEY = "renfield:worker:meeting:heartbeat"

_CHUNK = 1024 * 1024  # 1 MiB streaming read
# Uncompressed 44.1 kHz stereo 16-bit PCM is ~176 KB/s ≈ 620 MB/h; cap generously
# at 768 MB/h so a legitimate high-bitrate WAV is accepted. The WORKER enforces
# the authoritative DURATION cap — bytes can't tell duration without decoding.
_MAX_BYTES_PER_HOUR = 768 * 1024 * 1024
_ALLOWED_EXT = {
    ".wav", ".mp3", ".m4a", ".ogg", ".opus", ".flac", ".webm", ".aac", ".mp4",
}


class _AudioTooLarge(Exception):
    """Raised mid-stream when the upload exceeds the size ceiling."""


def _require_enabled() -> None:
    """404 the whole surface when the feature flag is off."""
    if not settings.meeting_transcription_enabled:
        raise HTTPException(status_code=404, detail="Meeting transcription is not enabled")


async def _meeting_worker_is_alive() -> bool:
    redis = get_redis()
    try:
        value = await redis.get(_MEETING_WORKER_HEARTBEAT_KEY)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"meeting worker heartbeat check failed: {e}; treating as unavailable")
        return False
    return value is not None


def _meetings_dir() -> str:
    d = os.path.join(settings.upload_dir, "meetings")
    os.makedirs(d, exist_ok=True)
    return d


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning(f"could not remove partial meeting audio {path}: {e}")


class MeetingResponse(BaseModel):
    id: int
    status: str
    title: str | None
    date: str | None
    error: str | None
    transcript_document_id: int | None
    created_at: str


def _to_response(m: Meeting) -> MeetingResponse:
    return MeetingResponse(
        id=m.id,
        status=m.status,
        title=m.title,
        date=m.date.isoformat() if m.date else None,
        error=m.error,
        transcript_document_id=m.transcript_document_id,
        created_at=m.created_at.isoformat() if m.created_at else "",
    )


@router.post("/transcribe", status_code=202, response_model=MeetingResponse)
async def transcribe_meeting(
    response: Response,
    audio: UploadFile = File(...),
    consent_confirmed: bool = Form(...),
    title: str | None = Form(None),
    date: str | None = Form(None),
    consent_note: str | None = Form(None),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
) -> MeetingResponse:
    """Upload a recording for transcription. Returns 202 with the meeting id to poll."""
    _require_enabled()
    if settings.auth_enabled and not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    # Consent is mandatory from day one (DE workplace recording — designed in).
    if not consent_confirmed:
        raise HTTPException(status_code=422, detail="consent_confirmed is required")

    ext = os.path.splitext(audio.filename or "")[1].lower()
    if ext not in _ALLOWED_EXT:
        raise HTTPException(
            status_code=422, detail=f"unsupported audio format: {ext or 'unknown'}"
        )

    meeting_date: date_cls | None = None
    if date:
        try:
            meeting_date = date_cls.fromisoformat(date)
        except ValueError:
            raise HTTPException(status_code=422, detail="date must be ISO format YYYY-MM-DD")

    # Worker-alive gate BEFORE any DB/disk work — never enqueue into a dead stream.
    if not await _meeting_worker_is_alive():
        raise HTTPException(
            status_code=503,
            detail={"message": "meeting worker unavailable", "retryable": True},
        )

    # Full-retention deadline, stamped at upload (0 = retain forever). The daily
    # retention job purges the transcript + segments + audio + row past this — a
    # consent-gated recording must not persist indefinitely.
    retention_until = None
    if settings.meeting_retention_days > 0:
        retention_until = datetime.utcnow() + timedelta(days=settings.meeting_retention_days)

    # Create the row first (cheap INSERT) so the audio is named by a durable id;
    # a stream failure below flips it to failed rather than orphaning a file.
    meeting = Meeting(
        owner_user_id=user.id if user else None,
        status="pending",
        title=title,
        date=meeting_date,
        consent_confirmed=True,
        consent_note=consent_note,
        retention_until=retention_until,
    )
    db.add(meeting)
    await db.commit()
    await db.refresh(meeting)

    audio_path = os.path.join(_meetings_dir(), f"meeting-{meeting.id}{ext}")
    max_bytes = settings.meeting_max_duration_h * _MAX_BYTES_PER_HOUR
    written = 0
    try:
        async with aiofiles.open(audio_path, "wb") as f:
            while chunk := await audio.read(_CHUNK):
                written += len(chunk)
                if written > max_bytes:
                    raise _AudioTooLarge()
                await f.write(chunk)
    except _AudioTooLarge:
        _safe_unlink(audio_path)
        meeting.status = "failed"
        meeting.error = "audio exceeds size limit"
        await db.commit()
        raise HTTPException(status_code=413, detail="audio file too large")
    except Exception as e:  # noqa: BLE001
        _safe_unlink(audio_path)
        meeting.status = "failed"
        meeting.error = f"upload failed: {e}"
        await db.commit()
        raise HTTPException(status_code=500, detail="failed to store audio")

    if written == 0:
        _safe_unlink(audio_path)
        meeting.status = "failed"
        meeting.error = "empty audio"
        await db.commit()
        raise HTTPException(status_code=422, detail="empty audio file")

    # Enqueue the PATH, not the bytes (worker reads it off the shared PVC).
    # Wrap it: a Redis outage here would otherwise strand a pending row + orphan
    # the audio with nothing to recover it (retention only touches completed /
    # retention_until rows). Clean up and 503 (retryable) instead.
    try:
        queue = MeetingTaskQueue(redis_client=get_redis())
        await queue.enqueue({"meeting_id": meeting.id, "audio_path": audio_path})
    except Exception as e:  # noqa: BLE001
        _safe_unlink(audio_path)
        meeting.status = "failed"
        meeting.error = f"enqueue failed: {e}"
        await db.commit()
        logger.error(f"meeting {meeting.id}: enqueue failed: {e}")
        raise HTTPException(
            status_code=503,
            detail={"message": "could not queue meeting", "retryable": True},
        )

    logger.info(f"meeting {meeting.id} queued ({written} bytes, ext={ext})")
    response.status_code = 202
    return _to_response(meeting)


@router.get("", response_model=list[MeetingResponse])
async def list_meetings(
    limit: int = Query(100, ge=1, le=200),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
) -> list[MeetingResponse]:
    """List the caller's meetings, newest first (owner-scoped 404-free — an
    unauthenticated caller under auth just gets an empty list). Backs the
    Meetings page; the frontend polls it while any row is pending/processing."""
    _require_enabled()

    stmt = select(Meeting).order_by(Meeting.created_at.desc()).limit(limit)
    if settings.auth_enabled:
        if not user:
            return []
        stmt = stmt.where(Meeting.owner_user_id == user.id)

    result = await db.execute(stmt)
    return [_to_response(m) for m in result.scalars().all()]


async def _get_owned_meeting(
    meeting_id: int, user: User | None, db: AsyncSession
) -> Meeting:
    """Fetch a meeting, enforcing owner scoping. 404 when missing OR not the
    caller's (owner-gated 404 — never leak existence). Auth off => any meeting."""
    meeting = await db.get(Meeting, meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    if settings.auth_enabled:
        if not user:
            raise HTTPException(status_code=401, detail="Authentication required")
        if meeting.owner_user_id != user.id:
            raise HTTPException(status_code=404, detail="Meeting not found")
    return meeting


@router.get("/{meeting_id}", response_model=MeetingResponse)
async def get_meeting(
    meeting_id: int,
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
) -> MeetingResponse:
    """Status poll for one meeting (owner-gated 404)."""
    _require_enabled()
    meeting = await _get_owned_meeting(meeting_id, user, db)
    return _to_response(meeting)


@router.delete("/{meeting_id}")
async def delete_meeting(
    meeting_id: int,
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Delete a meeting whole — transcript document (chunks/facts purged), audio,
    and the row. Owner-gated 404. Any status (a queued/failed meeting has no
    transcript yet; purge_meeting handles a null document id)."""
    _require_enabled()
    meeting = await _get_owned_meeting(meeting_id, user, db)
    from services.meeting_retention import purge_meeting

    await purge_meeting(db, meeting.id, meeting.transcript_document_id)
    return {"status": "deleted", "id": meeting_id}


class RelabelRequest(BaseModel):
    speaker_key: str  # the stable diarization cluster id (segments[].speaker_key)
    label: str = Field(min_length=1, max_length=100)


@router.get("/{meeting_id}/segments")
async def get_segments(
    meeting_id: int,
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The attributed transcript segments (owner-gated 404)."""
    _require_enabled()
    meeting = await _get_owned_meeting(meeting_id, user, db)
    return {"id": meeting.id, "status": meeting.status, "segments": meeting.segments or []}


@router.post("/{meeting_id}/relabel", response_model=MeetingResponse)
async def relabel_speaker(
    meeting_id: int,
    data: RelabelRequest,
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
) -> MeetingResponse:
    """Relabel one speaker cluster (pseudonym → person). Re-renders + reindexes
    the transcript in place (stable transcript_document_id). Owner-gated 404."""
    _require_enabled()
    meeting = await _get_owned_meeting(meeting_id, user, db)
    if meeting.status != "completed":
        raise HTTPException(status_code=409, detail="meeting not completed")
    from services.meeting_pipeline import reattribute

    ok = await reattribute(db, meeting, data.speaker_key, data.label)
    if not ok:
        raise HTTPException(status_code=404, detail="speaker not found")
    return _to_response(meeting)
