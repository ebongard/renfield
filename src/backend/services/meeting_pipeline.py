"""Meeting transcription pipeline (§2).

``process_meeting`` is the worker-callable entry: it drives the voice-server
diarization+ASR, applies honest pseudonyms to the raw speaker clusters, stores
the attributed segments on the ``Meeting`` row, and (task #10) renders + ingests
the transcript into the KB. Pure helpers (``apply_pseudonyms``,
``render_transcript_markdown``) are unit-tested without a GPU.

Attribution DEFAULT = honest pseudonyms ("Sprecher N") + one-click human
labeling. Auto-match is deferred (``meeting_auto_match_enabled`` dark) — the
spike separation gate was insufficient-data on synthetic audio.
"""
from __future__ import annotations

from loguru import logger
from sqlalchemy import select

from models.database import KnowledgeBase, Meeting
from services.database import AsyncSessionLocal
from services.voice_server_client import transcribe_meeting
from utils.config import settings

# Provenance marker on the ingested transcript Document (§2 D14): the Schicht-A
# hook skips docs with this source, so meeting small talk never spawns phantom
# obligations/calendar events.
MEETING_SOURCE = "meeting_transcript"
_MEETINGS_KB_NAME = "Meetings"


def _service_token() -> str:
    """Mint a short-lived service-account JWT for the pod-to-pod voice-server call."""
    from services.auth_service import create_access_token

    return create_access_token({"sub": "service:meeting", "scope": "voice"})


def apply_pseudonyms(raw_segments: list[dict]) -> list[dict]:
    """Map raw diarization cluster labels (e.g. ``SPEAKER_00``) to stable, human
    pseudonyms (``Sprecher 1``, ``Sprecher 2``, …) in first-appearance order.

    Preserves any embedding/timing fields; only the ``speaker`` label is
    rewritten and a ``speaker_key`` (the original cluster id) is retained so
    human labeling can later remap a whole cluster deterministically.
    """
    mapping: dict[str, str] = {}
    out: list[dict] = []
    for seg in raw_segments:
        cluster = str(seg.get("speaker", "SPEAKER_?"))
        if cluster not in mapping:
            mapping[cluster] = f"Sprecher {len(mapping) + 1}"
        new_seg = dict(seg)
        new_seg["speaker_key"] = cluster
        new_seg["speaker"] = mapping[cluster]
        out.append(new_seg)
    return out


def render_transcript_markdown(meeting: Meeting, segments: list[dict]) -> str:
    """Render attributed segments as a readable speaker-labelled markdown
    transcript (the document body ingested into the KB)."""
    lines: list[str] = []
    title = meeting.title or f"Meeting {meeting.id}"
    lines.append(f"# {title}")
    if meeting.date:
        lines.append(f"\n_{meeting.date.isoformat()}_")
    lines.append("")
    for seg in segments:
        speaker = seg.get("speaker", "Sprecher ?")
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"**{speaker}:** {text}")
    return "\n".join(lines).strip() + "\n"


async def _resolve_meetings_kb(db) -> KnowledgeBase:
    """Get-or-create the dedicated Meetings KB (mirrors resolve_target_kb)."""
    kb = (
        await db.execute(select(KnowledgeBase).where(KnowledgeBase.name == _MEETINGS_KB_NAME))
    ).scalar_one_or_none()
    if kb:
        return kb
    kb = KnowledgeBase(name=_MEETINGS_KB_NAME, description="Meeting transcripts (§2)")
    db.add(kb)
    await db.commit()
    await db.refresh(kb)
    return kb


async def _ingest_transcript(db, meeting: Meeting, markdown: str) -> int | None:
    """Ingest the rendered transcript into the Meetings KB and return its
    Document id. Schicht-A is gated off (source), never filed to Paperless.
    Content-hash dedup makes a re-run of identical content idempotent (returns
    the existing doc). Re-attribution (changed content) uses the reindex path
    instead — see re-attribution in the routes, NOT a second ingest_document."""
    from services.folder_ingest import IngestMeta, ingest_document

    kb = await _resolve_meetings_kb(db)
    meta = IngestMeta.from_dict({"filename": f"meeting-{meeting.id}.md"})
    result = await ingest_document(
        markdown.encode("utf-8"),
        meta,
        db=db,
        kb_id=kb.id,
        owner_user_id=meeting.owner_user_id,
        default_tier=meeting.circle_tier,
        file_to_paperless=False,
        source=MEETING_SOURCE,
    )
    return result.document_id


async def reattribute(db, meeting: Meeting, speaker_key: str, new_label: str) -> bool:
    """Relabel one speaker cluster (pseudonym → person, or fix a name).

    Rewrites every segment whose ``speaker_key`` matches, re-renders the
    transcript, OVERWRITES the existing transcript Document's file in place, and
    triggers the REINDEX path (never a new ingest_document — a content change
    would mint a second doc and orphan the first). ``transcript_document_id``
    stays stable. Returns False if no segment matched the cluster.
    """
    from sqlalchemy.orm.attributes import flag_modified

    import aiofiles

    from models.database import Document
    from services.redis_client import get_redis
    from services.task_queue import DocumentTaskQueue

    segments = [dict(s) for s in (meeting.segments or [])]
    changed = False
    for seg in segments:
        if seg.get("speaker_key") == speaker_key:
            seg["speaker"] = new_label
            changed = True
    if not changed:
        return False

    meeting.segments = segments
    flag_modified(meeting, "segments")
    await db.commit()

    # Overwrite the transcript file + reindex (content changed → reindex, NOT
    # a second ingest). Schicht-A stays gated off (Document.source persists).
    if meeting.transcript_document_id:
        doc = await db.get(Document, meeting.transcript_document_id)
        if doc is not None and doc.file_path:
            markdown = render_transcript_markdown(meeting, segments)
            async with aiofiles.open(doc.file_path, "wb") as f:
                await f.write(markdown.encode("utf-8"))
            doc.status = "pending"
            await db.commit()
            queue = DocumentTaskQueue(redis_client=get_redis())
            await queue.enqueue({
                "document_id": doc.id,
                "force_ocr": False,
                "user_id": meeting.owner_user_id,
                "trigger": "user_reindex",
            })
    return True


async def process_meeting(meeting_id: int, audio_path: str) -> None:
    """Transcribe + diarize a meeting, apply pseudonyms, and ingest the
    speaker-attributed transcript into the KB.

    The worker owns the status/heartbeat machine; this owns the CONTENT:
    voice-server call → pseudonyms → segments → rendered transcript ingest →
    ``completed`` + ``transcript_document_id``.
    """
    async with AsyncSessionLocal() as db:
        meeting = await db.get(Meeting, meeting_id)
        if meeting is None:
            logger.warning(f"process_meeting: meeting {meeting_id} vanished; skipping")
            return

        result = await transcribe_meeting(
            audio_path,
            auth_token=_service_token(),
            whisper_model=(settings.meeting_whisper_model or None),
        )
        raw_segments = result.get("segments") or []
        segments = apply_pseudonyms(raw_segments)
        meeting.segments = segments

        markdown = render_transcript_markdown(meeting, segments)
        doc_id = await _ingest_transcript(db, meeting, markdown)
        if doc_id is not None:
            meeting.transcript_document_id = doc_id

        meeting.status = "completed"
        meeting.error = None
        await db.commit()
        logger.info(
            f"meeting {meeting_id}: {len(segments)} segments, "
            f"{len({s['speaker'] for s in segments})} speaker(s), doc={doc_id}"
        )
