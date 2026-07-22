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

from models.database import MEETING_TRANSCRIPT_SOURCE, KnowledgeBase, Meeting
from services.database import AsyncSessionLocal
from services.voice_server_client import transcribe_meeting
from utils.config import settings

# Provenance marker on the ingested transcript Document (§2 D14): the Schicht-A
# hook skips docs with this source (single constant shared with the hook, so a
# rename can't silently re-open fact-mining on meeting small talk).
MEETING_SOURCE = MEETING_TRANSCRIPT_SOURCE
_MEETINGS_KB_NAME = "Meetings"


def _service_token() -> str:
    """Mint a short-lived service-account JWT for the pod-to-pod voice-server call.

    Returns "" when ``voice_server_auth_enabled`` is off (this instance shares
    another instance's voice-server, so our SECRET_KEY-signed token would 401 on
    signature verification) — the client then sends no token and the
    auth_required=false voice-server treats the call as anonymous."""
    from utils.config import settings

    if not settings.voice_server_auth_enabled:
        return ""
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
    # Hidden per-meeting marker: makes the content hash UNIQUE per meeting so the
    # (file_hash, kb_id) dedup in the shared Meetings KB can never cross-link two
    # owners' byte-identical transcripts to one Document.
    lines.append(f"<!-- meeting-id: {meeting.id} -->")
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
    body = "\n".join(lines).strip() + "\n"

    # Phase 3: append CONFIRMED minutes to the same document (D-M2 — one KB
    # artifact per meeting). Draft minutes are never rendered into the doc.
    if getattr(meeting, "minutes_status", "none") == "confirmed" and meeting.minutes:
        from services.meeting_minutes import render_minutes_markdown

        section = render_minutes_markdown(meeting.minutes)
        if section:
            body = body.rstrip() + "\n\n" + section
    return body


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


async def _overwrite_transcript_and_reindex(db, meeting: Meeting, segments: list[dict]) -> None:
    """Overwrite the existing transcript Document's file with the re-rendered
    markdown and trigger the reindex path (stable ``transcript_document_id``).

    Shared by re-attribution AND a crash-redelivery reprocess — NEVER a second
    ``ingest_document`` (diarization is non-deterministic run-to-run, so a
    re-ingest of drifted content would mint a second doc and orphan the first).

    Ordering: the file is written BEFORE the DB commit, so a write failure
    aborts (raises) before any state is persisted — segments/doc-status never
    end up inconsistent with the on-disk transcript. Requires
    ``meeting.segments`` already mutated in-memory (committed here, together
    with ``doc.status``)."""
    import aiofiles

    from models.database import DOC_STATUS_PENDING, Document
    from services.redis_client import get_redis
    from services.task_queue import DocumentTaskQueue

    doc = await db.get(Document, meeting.transcript_document_id)
    if doc is None or not doc.file_path:
        # doc deleted out of band (FK SET NULL) — just persist the segments.
        await db.commit()
        return

    markdown = render_transcript_markdown(meeting, segments)
    async with aiofiles.open(doc.file_path, "wb") as f:
        await f.write(markdown.encode("utf-8"))
    doc.status = DOC_STATUS_PENDING
    await db.commit()  # persists meeting.segments + doc.status together

    queue = DocumentTaskQueue(redis_client=get_redis())
    await queue.enqueue({
        "document_id": doc.id,
        "force_ocr": False,
        "user_id": meeting.owner_user_id,
        "trigger": "user_reindex",
    })


async def reattribute(db, meeting: Meeting, speaker_key: str, new_label: str) -> bool:
    """Relabel one speaker cluster (pseudonym → person, or fix a name).

    Rewrites every segment whose ``speaker_key`` matches, then overwrites the
    transcript in place + reindexes (stable ``transcript_document_id``).
    Returns False if no segment matched the cluster.
    """
    from sqlalchemy.orm.attributes import flag_modified

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
    if meeting.transcript_document_id:
        await _overwrite_transcript_and_reindex(db, meeting, segments)
    else:
        await db.commit()
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
        # §2 Track A: resolve each diarized cluster to a stable cross-meeting
        # anonymous fingerprint and ride it onto the segments (dark by default;
        # display pseudonyms unchanged). Best-effort — a matcher failure must not
        # fail the transcript.
        if settings.meeting_fingerprints_enabled:
            try:
                from services.meeting_fingerprint_service import (
                    annotate_segments,
                    resolve_meeting_fingerprints,
                )

                # SAVEPOINT: a mid-resolve failure rolls back ONLY the matcher's
                # partial fingerprint writes (else already-flushed rows would ride
                # the later meeting.segments commit as orphans).
                async with db.begin_nested():
                    resolved = await resolve_meeting_fingerprints(db, meeting, raw_segments)
                annotate_segments(segments, resolved)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"meeting {meeting_id}: fingerprint matching failed: {e}")
        meeting.segments = segments

        if meeting.transcript_document_id:
            # Crash-redelivery reprocess: a prior attempt already ingested the
            # transcript but died before status=completed. Overwrite that doc in
            # place — re-ingesting drifted (non-deterministic) diarization output
            # would orphan the first doc.
            await _overwrite_transcript_and_reindex(db, meeting, segments)
        else:
            markdown = render_transcript_markdown(meeting, segments)
            doc_id = await _ingest_transcript(db, meeting, markdown)
            if doc_id is not None:
                meeting.transcript_document_id = doc_id

        meeting.status = "completed"
        meeting.error = None
        await db.commit()
        logger.info(
            f"meeting {meeting_id}: {len(segments)} segments, "
            f"{len({s['speaker'] for s in segments})} speaker(s), "
            f"doc={meeting.transcript_document_id}"
        )
