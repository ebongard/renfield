"""Meeting retention + audio lifecycle (§2).

Two mechanisms, run by one daily job (``_schedule_meeting_retention``):

1. **Audio grace cleanup** — a completed meeting's original audio is deleted
   after ``meeting_audio_grace_days`` (unless ``meeting_keep_audio``). The
   transcript stays; only the (large) source recording is freed.
2. **Full retention** — a meeting past its ``retention_until`` is deleted whole:
   the transcript Document via the sanctioned document-delete path (purges
   chunks/facts), plus its audio + the meeting row itself.

Audio path is derived from the meeting id (``meeting-{id}.*`` under the uploads
dir) — retention is a mechanism, not a column (design).
"""
from __future__ import annotations

import glob
import os
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import select

from models.database import Meeting
from services.database import AsyncSessionLocal
from utils.config import settings


def _audio_glob(meeting_id: int) -> str:
    return os.path.join(settings.upload_dir, "meetings", f"meeting-{meeting_id}.*")


def _delete_audio(meeting_id: int) -> bool:
    """Delete the meeting's audio file(s). Returns True if anything was removed."""
    removed = False
    for path in glob.glob(_audio_glob(meeting_id)):
        try:
            os.unlink(path)
            removed = True
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning(f"meeting retention: could not delete {path}: {e}")
    return removed


async def purge_meeting(
    db, meeting_id: int, transcript_document_id: int | None
) -> None:
    """Delete a meeting whole: its transcript Document via the sanctioned
    document-delete path (purges chunks/facts), its audio file(s), and the row.
    Commits. Raises on failure (caller decides whether to roll back / skip).

    Shared by the full-retention sweep and ``DELETE /api/meetings/{id}`` so the
    cascade lives in one place.
    """
    if transcript_document_id is not None:
        from services.rag_service import RAGService

        await RAGService(db).delete_document(transcript_document_id)
    _delete_audio(meeting_id)
    m = await db.get(Meeting, meeting_id)
    if m is not None:
        await db.delete(m)
    await db.commit()


async def cleanup_meetings() -> tuple[int, int]:
    """Run both mechanisms. Returns ``(audio_deleted, meetings_purged)``."""
    audio_deleted = 0
    meetings_purged = 0
    now = datetime.utcnow()

    async with AsyncSessionLocal() as db:
        # 1. Audio grace cleanup (unless the deployment opts to keep audio).
        # Includes FAILED meetings: a worker-failed transcription leaves its
        # audio on the PVC (the upload route only unlinks on UPLOAD failure), so
        # the grace sweep must free it too — not just completed ones.
        if not settings.meeting_keep_audio:
            cutoff = now - timedelta(days=settings.meeting_audio_grace_days)
            done = (
                await db.execute(
                    select(Meeting).where(
                        Meeting.status.in_(("completed", "failed")),
                        Meeting.created_at < cutoff,
                    )
                )
            ).scalars().all()
            for m in done:
                if _delete_audio(m.id):
                    audio_deleted += 1

        # 2. Full retention: purge meetings past retention_until. Each is purged
        # in its OWN commit so one bad row can't abort the sweep — delete_document
        # commits on the shared session, and a mid-op failure would poison it, so
        # a failure rolls back and skips (retried next sweep). We loop over plain
        # (id, doc_id) tuples and re-fetch each row fresh: holding ORM objects
        # across a rollback expires them, and the next attribute access would
        # trigger lazy IO → MissingGreenlet.
        expired = (
            await db.execute(
                select(Meeting.id, Meeting.transcript_document_id).where(
                    Meeting.retention_until.is_not(None),
                    Meeting.retention_until <= now,
                )
            )
        ).all()
        for meeting_id, transcript_document_id in expired:
            try:
                await purge_meeting(db, meeting_id, transcript_document_id)
                meetings_purged += 1
            except Exception as e:  # noqa: BLE001 - one bad row must not block the sweep
                logger.warning(f"meeting retention: purge of meeting {meeting_id} failed: {e}")
                await db.rollback()
                continue

    return audio_deleted, meetings_purged
