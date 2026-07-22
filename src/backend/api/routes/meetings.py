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
    project_id: int | None
    language: str | None
    # Lets the meeting list surface a "Protokoll: Entwurf bereit" badge without
    # a per-card minutes fetch (§2 Phase 0 / Track D UX).
    minutes_status: str
    created_at: str


def _to_response(m: Meeting) -> MeetingResponse:
    return MeetingResponse(
        id=m.id,
        status=m.status,
        title=m.title,
        date=m.date.isoformat() if m.date else None,
        error=m.error,
        transcript_document_id=m.transcript_document_id,
        project_id=m.project_id,
        language=getattr(m, "language", None),
        minutes_status=getattr(m, "minutes_status", "none") or "none",
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
    project_id: int | None = Form(None),
    language: str | None = Form(None),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
) -> MeetingResponse:
    """Upload a recording for transcription. Returns 202 with the meeting id to poll.

    Optional ``project_id`` (Phase 4A) scopes the meeting to a Project so it
    surfaces on ``/projects/{id}/timeline``; owner-validated when provided."""
    _require_enabled()
    if settings.auth_enabled and not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    # Validate an optional project scope: it must exist and (auth on) be the
    # caller's, so a meeting can't be attached to someone else's project.
    if project_id is not None:
        from models.database import Project

        project = await db.get(Project, project_id)
        if project is None or (settings.auth_enabled and user and project.owner_id != user.id):
            raise HTTPException(status_code=404, detail="Project not found")

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

    # ASR language: "auto" (whisper detects) or a 2-letter ISO code ("en"/"de");
    # None → the voice-server default. Reject junk so it can't reach whisper.
    meeting_language: str | None = None
    if language:
        norm = language.strip().lower()
        if norm != "auto" and not (len(norm) == 2 and norm.isalpha()):
            raise HTTPException(
                status_code=422,
                detail="language must be 'auto' or a 2-letter ISO code (e.g. 'en', 'de')",
            )
        meeting_language = norm

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
        project_id=project_id,
        language=meeting_language,
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


class UpdateMeetingRequest(BaseModel):
    """PATCH body — currently just the project link. ``project_id=null`` unlinks;
    a non-null id must be an existing project the caller owns."""
    project_id: int | None = None


@router.patch("/{meeting_id}", response_model=MeetingResponse)
async def update_meeting(
    meeting_id: int,
    request: UpdateMeetingRequest,
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
) -> MeetingResponse:
    """Change or clear a meeting's project link (Phase 4A follow-up). Owner-gated
    404 on the meeting. ``project_id=null`` unlinks; a non-null id is owner-
    validated exactly like the upload path, so a meeting can't be attached to
    someone else's project. Idempotent — sets the link to the value provided."""
    _require_enabled()
    meeting = await _get_owned_meeting(meeting_id, user, db)

    if request.project_id is not None:
        from models.database import Project

        project = await db.get(Project, request.project_id)
        if project is None or (settings.auth_enabled and user and project.owner_id != user.id):
            raise HTTPException(status_code=404, detail="Project not found")

    meeting.project_id = request.project_id
    await db.commit()
    await db.refresh(meeting)
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


class RelabelResponse(MeetingResponse):
    # §2 Track A merge-on-enroll: how many OTHER meetings this relabel also renamed
    # (same anonymous fingerprint). Lets the UI say "also applied to N meetings" so
    # cross-meeting propagation is visible, not a surprise. 0 when the flag is off
    # or the cluster has no cross-meeting fingerprint.
    cross_meeting_applied: int = 0


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


@router.post("/{meeting_id}/relabel", response_model=RelabelResponse)
async def relabel_speaker(
    meeting_id: int,
    data: RelabelRequest,
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
) -> RelabelResponse:
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

    # Merge-on-enroll (§2 Track A): propagate the human name to other meetings
    # sharing this cluster's fingerprint. Best-effort — the primary relabel above
    # already committed, so a propagation hiccup must not fail the request.
    cross_meeting_applied = 0
    if settings.meeting_fingerprints_enabled:
        from services.meeting_pipeline import enroll_fingerprint_across_meetings

        try:
            affected = await enroll_fingerprint_across_meetings(
                db, meeting, data.speaker_key, data.label
            )
            cross_meeting_applied = len(affected)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"meeting {meeting_id}: merge-on-enroll propagation failed: {e}")

    return RelabelResponse(**_to_response(meeting).model_dump(),
                           cross_meeting_applied=cross_meeting_applied)


# --------------------------------------------------------------------------- #
# §2 Phase 3 — minutes (summary / decisions / action-items with human confirm)
# --------------------------------------------------------------------------- #

class _Decision(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    made_by: str = Field(default="", max_length=200)


class _ActionItem(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    owner: str = Field(default="", max_length=200)
    due_hint: str = Field(default="", max_length=200)


class MinutesBody(BaseModel):
    summary: str = Field(default="", max_length=4000)
    decisions: list[_Decision] = Field(default_factory=list, max_length=100)
    action_items: list[_ActionItem] = Field(default_factory=list, max_length=200)


class MinutesResponse(BaseModel):
    id: int
    minutes_status: str
    minutes: dict | None


def _require_minutes_enabled() -> None:
    _require_enabled()
    if not settings.meeting_minutes_enabled:
        raise HTTPException(status_code=404, detail="Meeting minutes are not enabled")


def _minutes_response(m: Meeting) -> MinutesResponse:
    return MinutesResponse(id=m.id, minutes_status=m.minutes_status, minutes=m.minutes)


@router.post("/{meeting_id}/minutes/generate", response_model=MinutesResponse)
async def generate_minutes(
    meeting_id: int,
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
) -> MinutesResponse:
    """Extract DRAFT minutes from a completed meeting's transcript (owner-gated).
    409 if the meeting isn't completed. Re-running overwrites the draft."""
    _require_minutes_enabled()
    meeting = await _get_owned_meeting(meeting_id, user, db)
    if meeting.status != "completed":
        raise HTTPException(status_code=409, detail="meeting not completed")

    from services.meeting_minutes import MinutesExtractor

    draft = await MinutesExtractor().extract(meeting.segments or [])
    meeting.minutes = draft
    meeting.minutes_status = "draft"
    meeting.minutes_generated_at = datetime.utcnow()
    meeting.minutes_confirmed_at = None
    await db.commit()
    return _minutes_response(meeting)


@router.get("/{meeting_id}/minutes", response_model=MinutesResponse)
async def get_minutes(
    meeting_id: int,
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
) -> MinutesResponse:
    """Current minutes + status (owner-gated 404)."""
    _require_minutes_enabled()
    meeting = await _get_owned_meeting(meeting_id, user, db)
    return _minutes_response(meeting)


@router.put("/{meeting_id}/minutes", response_model=MinutesResponse)
async def update_minutes(
    meeting_id: int,
    body: MinutesBody,
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
) -> MinutesResponse:
    """Owner edits the draft. 409 unless status is draft (confirmed minutes are
    re-opened by editing → back to draft, so a re-confirm re-renders)."""
    _require_minutes_enabled()
    meeting = await _get_owned_meeting(meeting_id, user, db)
    if meeting.minutes_status not in ("draft", "confirmed"):
        raise HTTPException(status_code=409, detail="no minutes to edit — generate first")
    meeting.minutes = body.model_dump()
    meeting.minutes_status = "draft"
    meeting.minutes_confirmed_at = None
    await db.commit()
    return _minutes_response(meeting)


@router.post("/{meeting_id}/minutes/confirm", response_model=MinutesResponse)
async def confirm_minutes(
    meeting_id: int,
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
) -> MinutesResponse:
    """Confirm the draft → renders the minutes into the transcript document (same
    stable-doc reindex path as re-attribution). 409 unless status is draft."""
    _require_minutes_enabled()
    meeting = await _get_owned_meeting(meeting_id, user, db)
    if meeting.minutes_status != "draft":
        raise HTTPException(status_code=409, detail="minutes not in draft state")

    meeting.minutes_status = "confirmed"
    meeting.minutes_confirmed_at = datetime.utcnow()
    # Re-render the transcript doc WITH the confirmed minutes + reindex in place
    # (commits meeting + doc status). No new ingest — stable transcript_document_id.
    from services.meeting_pipeline import _overwrite_transcript_and_reindex

    await _overwrite_transcript_and_reindex(db, meeting, meeting.segments or [])
    return _minutes_response(meeting)


@router.delete("/{meeting_id}/minutes", response_model=MinutesResponse)
async def delete_minutes(
    meeting_id: int,
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
) -> MinutesResponse:
    """Discard minutes → back to none. Does NOT strip already-confirmed minutes
    from the transcript document (a subsequent relabel/reindex would drop them)."""
    _require_minutes_enabled()
    meeting = await _get_owned_meeting(meeting_id, user, db)
    meeting.minutes = None
    meeting.minutes_status = "none"
    meeting.minutes_generated_at = None
    meeting.minutes_confirmed_at = None
    await db.commit()
    return _minutes_response(meeting)
