"""Fax reception (#1677, Telefonie Phase 3): a received fax as a document of ONE owner.

Dark behind ``TELEPHONY_FAX_ENABLED`` (plus ``TELEPHONY_ENABLED`` and
``TELEPHONY_FAX_OWNER``); off, the telephony client ignores ``fax_received`` as before.

- The gateway (Asterisk ReceiveFAX, T.38 with G.711 fallback, TIFF -> PDF) sends
  ``fax_received`` {call_id, did, caller, status ok|failed, pages, fax?}.
- ``handle_event`` (called by the telephony client): a failed fax is announced to
  the owner at once; a received one becomes a job (``telephony_faxes``, unique
  ``call_id``, so a replay is a no-op). A queue error RAISES -- nothing was sent
  yet, so the client retries the event; that is what a retry is for.
- A loop claims one due job at a time (``FOR UPDATE SKIP LOCKED`` + a lease, as
  ``voicemail_transcript``), fetches the PDF from the gateway
  (``GET /v1/faxes/<basename>``, the instance token, the pinned CA) and files it
  through the ONE document seam, ``folder_ingest.ingest_document``: dedup, OCR,
  owner + tier, the owner's KB ``TELEPHONY_FAX_KB``, Paperless filing out of band.
- Then the owner -- and only the owner -- gets a personal notification
  ("Fax von … (n Seiten)"), or "die Ablage ist fehlgeschlagen" once the job gives
  up, so a fax is never lost silently (the gateway keeps the PDF 30 days).
- Content stays out of logs: no number, no file name, no text.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from utils.config import settings
from utils.log_safety import error_text_without_content

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
RETRY_DELAYS_S = (120, 480)          # after attempt 1 and 2
LEASE_S = 300                        # a claimed job a crashed pod left behind runs again after this
IDLE_POLL_S = 60.0
MAX_FAX_BYTES = 20 * 1024 * 1024     # the gateway's cap
PDF_MIME = "application/pdf"
INGEST_SOURCE = "telephony_fax"

_wake: asyncio.Event | None = None   # created by the loop; enqueue only sets it


def enabled() -> bool:
    """The filing loop runs. Queuing needs only the flag (see ``handle_event``)."""
    return bool(settings.telephony_fax_enabled and settings.telephony_enabled and settings.telephony_fax_owner.strip())


def _event_for(job, *, filed: bool, kb: str | None = None, document_id: int | None = None) -> dict:
    """The notification event of a job (the gateway's event, rebuilt from the row)."""
    event = {"type": "fax_received", "call_id": job.call_id, "did": job.did, "caller": job.caller,
             "status": "ok", "pages": job.pages, "filed": filed}
    if kb:
        event["kb"] = kb
    if document_id is not None:
        event["document_id"] = document_id
    return event


async def _owner_id(db) -> int | None:
    from services.ingest_common import resolve_user_id

    return await resolve_user_id(db, settings.telephony_fax_owner)


async def notify_owner(event: dict) -> None:
    """One personal notification to the configured owner; the caller is named from
    what THAT owner may see (caller lookup), like every telephony notification."""
    from services.database import AsyncSessionLocal
    from services.notification_service import NotificationService
    from services.telephony_client import notify_users

    async with AsyncSessionLocal() as db:
        owner = await _owner_id(db)
        if owner is None:
            logger.error("Fax notification: TELEPHONY_FAX_OWNER is no user — nobody notified")
            return
        resolve = None
        if settings.caller_lookup_enabled and settings.contact_points_enabled:
            from services.caller_lookup import resolve_caller as resolve
        await notify_users(event, [owner], NotificationService(db).process_webhook, resolve)


async def enqueue(event: dict) -> bool:
    """Queue the filing of a received fax. False = nothing to queue (no valid file).
    Raises on a database error, so the telephony client retries the event."""
    from sqlalchemy.dialects.postgresql import insert

    from models.database import VOICEMAIL_PENDING, TelephonyFax, _utcnow
    from services.database import AsyncSessionLocal
    from services.telephony_client import _pages, _plain_number
    from services.voicemail_transcript import valid_recording

    call_id = str(event.get("call_id") or "")[:64]
    fax = event.get("fax")
    # The gateway names a fax like a recording (<YYYYMMDD-HHMMSS>-<uniqueid>, no extension;
    # telefonie extensions.conf fax-receive): the same check as for recordings.
    if not call_id or not valid_recording(fax):
        return False
    now = _utcnow()
    stmt = (
        insert(TelephonyFax)
        .values(
            call_id=call_id, fax=fax, caller=_plain_number(event.get("caller")),
            did=_plain_number(event.get("did")), pages=_pages(event.get("pages")),
            status=VOICEMAIL_PENDING, attempts=0, next_attempt_at=now, created_at=now, updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["call_id"])
    )
    async with AsyncSessionLocal() as db:
        await db.execute(stmt)
        await db.commit()
    if _wake is not None:
        _wake.set()
    return True


async def handle_event(event: dict) -> None:
    """``fax_received`` from the telephony client. Raises = retry the event.

    A received fax is queued whenever the flag is on -- also without a valid
    owner: the job waits in ``telephony_faxes`` (the loop does not start, or
    retries) instead of the event being marked done and the fax forgotten."""
    if not settings.telephony_fax_enabled:
        return
    if not enabled():
        logger.error("Fax received but TELEPHONY_FAX_OWNER is empty — queued, not filed until it is set")
    if event.get("status") == "ok" and await enqueue(event):
        return
    # Not received (or "ok" without a usable file): tell the owner now, nothing to file.
    # Only the vetted fields: nothing else of a gateway event reaches the notification.
    await notify_owner({
        "type": "fax_received", "status": "failed", "call_id": event.get("call_id"),
        "did": event.get("did"), "caller": event.get("caller"), "pages": event.get("pages"),
    })


async def fetch_fax(name: str) -> bytes:
    from services.voicemail_transcript import _Fail, fetch_gateway_file

    pdf = await fetch_gateway_file("/v1/faxes/", name, MAX_FAX_BYTES, what="fax")
    if not pdf.startswith(b"%PDF-"):
        raise _Fail("not a PDF")
    return pdf


async def file_fax(db, job, pdf: bytes):
    """Into the owner's fax KB through the document pipeline. Returns (IngestResult, kb name)."""
    from services.folder_ingest import IngestMeta, IngestResult, IngestStatus, ingest_document
    from services.ingest_common import get_or_create_owned_kb

    owner = await _owner_id(db)
    if owner is None:
        # A configuration error, not a bad fax: retry (and log loudly) -- an owner that is
        # fixed within the retries still gets the fax; the PDF stays 30 days at the gateway.
        logger.error("Fax job: TELEPHONY_FAX_OWNER is no user — cannot file")
        return IngestResult(IngestStatus.RETRY, detail="no_owner"), None
    kb_name = settings.telephony_fax_kb.strip() or "Fax"
    kb = await get_or_create_owned_kb(db, kb_name, owner, description="Empfangene Faxe (Telefonie)")
    stamp = job.created_at.strftime("%Y-%m-%d_%H%M%S") if job.created_at else "unbekannt"
    meta = IngestMeta(
        filename=f"Fax_{stamp}.pdf",  # no number in the name: it is shown in lists and Paperless
        root=INGEST_SOURCE,
        relpath=f"{job.call_id}.pdf",
        mime=PDF_MIME,
    )
    result = await ingest_document(
        pdf, meta, db=db, kb_id=kb.id, owner_user_id=owner, default_tier=settings.telephony_fax_tier,
        file_to_paperless=settings.telephony_fax_to_paperless, source=INGEST_SOURCE,
    )
    await db.commit()  # nothing left open (get_or_create_owned_kb and ingest_document commit their own rows)
    return result, kb_name


async def _claim():
    """The claimed job (detached copy); None = nothing due; False = a job was due
    but used up its attempts -- it is failed now and its owner was told."""
    from sqlalchemy import select

    from models.database import VOICEMAIL_FAILED, VOICEMAIL_PENDING, TelephonyFax, _utcnow
    from services.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        now = _utcnow()
        job = (
            await db.execute(
                select(TelephonyFax)
                .where(TelephonyFax.status == VOICEMAIL_PENDING, TelephonyFax.next_attempt_at <= now)
                .order_by(TelephonyFax.next_attempt_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if job is None:
            return None
        if job.attempts >= MAX_ATTEMPTS:
            # Every attempt was claimed and none came back (the pod died each time).
            job.status = VOICEMAIL_FAILED
            job.updated_at = now
            event = _event_for(job, filed=False)
            await db.commit()
            logger.warning("Fax job %d failed: no attempt finished", job.id)
            await _notify_quietly(event)
            return False
        job.attempts += 1
        job.next_attempt_at = now + timedelta(seconds=LEASE_S)  # the lease
        await db.commit()
        await db.refresh(job)
        db.expunge(job)
        return job


async def _settle(job_id: int, status: str, delay_s: float | None = None, document_id: int | None = None) -> None:
    from sqlalchemy import update

    from models.database import TelephonyFax, _utcnow
    from services.database import AsyncSessionLocal

    values: dict = {"status": status, "updated_at": _utcnow()}
    if delay_s is not None:
        values["next_attempt_at"] = _utcnow() + timedelta(seconds=delay_s)
    if document_id is not None:
        values["document_id"] = document_id
    async with AsyncSessionLocal() as db:
        await db.execute(update(TelephonyFax).where(TelephonyFax.id == job_id).values(**values))
        await db.commit()


async def _notify_quietly(event: dict) -> None:
    try:
        await notify_owner(event)
    except Exception as e:
        logger.warning("Fax notification failed: %s", error_text_without_content(e))


async def process_one() -> bool:
    """Run one due job. False = nothing was due."""
    from models.database import VOICEMAIL_DONE, VOICEMAIL_FAILED, VOICEMAIL_PENDING
    from services.database import AsyncSessionLocal
    from services.folder_ingest import IngestStatus
    from services.voicemail_transcript import _Fail, _Retry

    job = await _claim()
    if job is None:
        return False
    if job is False:
        return True
    try:
        pdf = await fetch_fax(job.fax)
        from services.task_queue import document_worker_is_alive

        if not await document_worker_is_alive():
            raise _Retry("document worker unavailable")  # enqueued work nobody consumes = an orphan
        async with AsyncSessionLocal() as db:
            result, kb_name = await file_fax(db, job, pdf)
        if result.status is IngestStatus.RETRY:
            raise _Retry(f"ingest: {result.detail}")
        if result.status is IngestStatus.FAILED:
            raise _Fail(f"ingest: {result.detail}")
        # Filed (or already there: a repeated job). Notice first, then "done": a crash
        # in between repeats the job -- the dedup makes it a duplicate, the owner is
        # told twice, never not at all.
        await _notify_quietly(_event_for(job, filed=True, kb=kb_name, document_id=result.document_id))
        await _settle(job.id, VOICEMAIL_DONE, document_id=result.document_id)
        logger.info("Fax job %d filed (%s)", job.id, result.status.value)
        return True
    except _Fail as e:
        status, delay, why = VOICEMAIL_FAILED, None, error_text_without_content(e)
    except Exception as e:  # _Retry and anything unexpected
        why = error_text_without_content(e)
        if job.attempts >= MAX_ATTEMPTS:
            status, delay = VOICEMAIL_FAILED, None
        else:
            status, delay = VOICEMAIL_PENDING, RETRY_DELAYS_S[min(job.attempts, len(RETRY_DELAYS_S)) - 1]
    logger.warning("Fax job %d attempt %d/%d: %s -> %s", job.id, job.attempts, MAX_ATTEMPTS, why, status)
    try:
        await _settle(job.id, status, delay)
    except Exception as e:  # the lease runs out and the job comes back
        logger.warning("Fax job %d not settled: %s", job.id, error_text_without_content(e))
        return True
    if status == VOICEMAIL_FAILED:
        await _notify_quietly(_event_for(job, filed=False))
    return True


async def run_fax_jobs() -> None:
    """The backend loop (lifecycle task). Picks up pending jobs on start as well."""
    global _wake
    if not enabled():
        logger.warning("TELEPHONY_FAX_ENABLED needs TELEPHONY_ENABLED and TELEPHONY_FAX_OWNER — not started")
        return
    _wake = asyncio.Event()
    while True:
        _wake.clear()
        try:
            busy = await process_one()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # e.g. the database is down: wait and try again
            logger.warning("Fax loop: %s", error_text_without_content(e))
            busy = False
        if busy:
            continue
        try:
            await asyncio.wait_for(_wake.wait(), IDLE_POLL_S)
        except TimeoutError:
            pass
