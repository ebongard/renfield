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


async def cleanup_meetings() -> tuple[int, int]:
    """Run both mechanisms. Returns ``(audio_deleted, meetings_purged)``."""
    audio_deleted = 0
    meetings_purged = 0
    now = datetime.utcnow()

    async with AsyncSessionLocal() as db:
        # 1. Audio grace cleanup (unless the deployment opts to keep audio).
        if not settings.meeting_keep_audio:
            cutoff = now - timedelta(days=settings.meeting_audio_grace_days)
            completed = (
                await db.execute(
                    select(Meeting).where(
                        Meeting.status == "completed",
                        Meeting.created_at < cutoff,
                    )
                )
            ).scalars().all()
            for m in completed:
                if _delete_audio(m.id):
                    audio_deleted += 1

        # 2. Full retention: purge meetings past retention_until.
        expired = (
            await db.execute(
                select(Meeting).where(
                    Meeting.retention_until.is_not(None),
                    Meeting.retention_until <= now,
                )
            )
        ).scalars().all()
        for m in expired:
            if m.transcript_document_id is not None:
                try:
                    from services.rag_service import RAGService

                    await RAGService(db).delete_document(m.transcript_document_id)
                except Exception as e:  # noqa: BLE001 - never let one bad row block the sweep
                    logger.warning(
                        f"meeting retention: delete_document {m.transcript_document_id} "
                        f"for meeting {m.id} failed: {e}"
                    )
            _delete_audio(m.id)
            await db.delete(m)
            meetings_purged += 1

        await db.commit()

    return audio_deleted, meetings_purged
