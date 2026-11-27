"""Voicemail transcript (#1669): the text of a message in its notification.

Design: ``docs/design/voicemail-transcript.md``. Dark behind ``VOICEMAIL_TRANSCRIPT_ENABLED``.

- The telephony client queues a job per voicemail (``enqueue``, never raises:
  a queue problem must never become "delivery failed", which would retry the
  event and notify everyone twice). ``call_id`` is unique, a replay is a no-op.
- A loop in the backend claims one due job at a time (``FOR UPDATE SKIP LOCKED``
  + a lease, so several replicas and a crashed pod are fine), fetches the audio
  from the gateway (``GET /v1/recordings/<basename>``, the instance token, the
  pinned CA), transcribes it with ``embed=false`` and REQUIRES the reply to say
  ``embedded: false`` -- a caller's voice never becomes a voiceprint.
- The text goes ONLY into the existing voicemail notification rows of the
  original recipients (not dismissed, not expired), which become confidential.
  No new notification, no push, no TTS, no embedding, never into an LLM prompt:
  anyone who can dial the number can speak into it.
- Content stays out of logs: no transcript, no number, no recording name.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import timedelta
from urllib.parse import urlsplit, urlunsplit

from utils.config import settings
from utils.log_safety import error_text_without_content

logger = logging.getLogger(__name__)

EVENT_TYPE = "telephony.voicemail"
# The gateway's own name check (telefonie gateway/recordings.py), ASCII digits only.
RECORDING_RE = re.compile(r"[0-9]{8}-[0-9]{6}-[0-9]{1,20}\.[0-9]{1,10}")
MAX_ATTEMPTS = 3
RETRY_DELAYS_S = (120, 480)          # after attempt 1 and 2: ~10 minutes in all
LEASE_S = 300                        # a claimed job a crashed pod left behind runs again after this
IDLE_POLL_S = 60.0
FETCH_TIMEOUT_S = 30.0               # whole download, not per read
STT_TIMEOUT_S = 60.0
MAX_RECORDING_BYTES = 8 * 1024 * 1024  # the gateway's cap (180 s of 16 kHz mono = ~5.8 MB)
MAX_TRANSCRIPT_CHARS = 1000
NOTE = "(automatisch verschriftet, kann Fehler enthalten)"

_wake: asyncio.Event | None = None   # created by the loop; enqueue only sets it


class _Retry(Exception):
    """Transient: gateway down/busy/not ready, STT unreachable or 5xx, timeout."""


class _Fail(Exception):
    """Terminal: the file is gone, a refused request, no ``embedded: false``."""


def enabled() -> bool:
    return bool(settings.voicemail_transcript_enabled and settings.telephony_enabled and settings.voice_server_url)


def valid_recording(name: object) -> bool:
    return isinstance(name, str) and RECORDING_RE.fullmatch(name) is not None


async def enqueue(event: dict) -> None:
    """Queue the transcript of a voicemail event. Never raises."""
    if not settings.voicemail_transcript_enabled:
        return
    call_id = str(event.get("call_id") or "")[:64]
    recording = event.get("recording")
    if not call_id or not valid_recording(recording):
        return  # an older gateway (no ``recording``) or a forged name: nothing to fetch
    try:
        from sqlalchemy.dialects.postgresql import insert

        from models.database import VOICEMAIL_PENDING, TelephonyVoicemail, _utcnow
        from services.database import AsyncSessionLocal

        now = _utcnow()
        stmt = (
            insert(TelephonyVoicemail)
            .values(
                call_id=call_id, recording=recording, status=VOICEMAIL_PENDING, attempts=0,
                next_attempt_at=now, created_at=now, updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=["call_id"])
        )
        async with AsyncSessionLocal() as db:
            await db.execute(stmt)
            await db.commit()
    except Exception as e:
        logger.warning("Voicemail transcript not queued: %s", error_text_without_content(e))
        return
    if _wake is not None:
        _wake.set()


def recording_url(name: str) -> str:
    parts = urlsplit(settings.telephony_gateway_url)
    scheme = "https" if parts.scheme == "wss" else "http"
    return urlunsplit((scheme, parts.netloc, f"/v1/recordings/{name}", "", ""))


async def fetch_recording(name: str) -> bytes:
    """The WAV from the gateway; size cap and deadline enforced while streaming."""
    import httpx

    from services.telephony_client import _is_loopback, ssl_context_for

    url = settings.telephony_gateway_url
    if url.startswith("wss://"):
        verify = ssl_context_for(settings.telephony_gateway_ca_pem)
    elif _is_loopback(url):
        verify = True  # plain http to localhost only, like the event socket
    else:
        raise _Fail("gateway url is not wss://")
    headers = {"Authorization": f"Bearer {settings.telephony_gateway_token.get_secret_value()}"}

    async def _download() -> bytes:
        async with httpx.AsyncClient(verify=verify, timeout=FETCH_TIMEOUT_S, follow_redirects=False) as client:
            async with client.stream("GET", recording_url(name), headers=headers) as resp:
                code = resp.status_code
                if code == 404:
                    raise _Fail("recording gone (404)")
                if code in (425, 429) or code >= 500:
                    raise _Retry(f"gateway answered {code}")
                if code != 200:
                    raise _Fail(f"gateway answered {code}")
                try:
                    declared = int(resp.headers.get("content-length", "0"))
                except ValueError:
                    declared = 0
                if declared > MAX_RECORDING_BYTES:
                    raise _Fail("recording too large")
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf += chunk
                    if len(buf) > MAX_RECORDING_BYTES:
                        raise _Fail("recording too large")
                return bytes(buf)

    try:
        audio = await asyncio.wait_for(_download(), FETCH_TIMEOUT_S)
    except (_Retry, _Fail):
        raise
    except Exception as e:  # connect error, TLS, timeout
        raise _Retry(f"gateway fetch failed ({type(e).__name__})") from e
    if audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
        raise _Fail("not a WAV file")
    return audio


def _service_token() -> str:
    if not settings.voice_server_auth_enabled:
        return ""
    from services.auth_service import create_access_token

    return create_access_token({"sub": "service:voicemail", "scope": "voice"})


async def transcribe(audio: bytes) -> str:
    """Cleaned, bounded text ("" for a silent message). No voiceprint, verified."""
    from services.caller_lookup import _clean
    from services.voice_server_client import VoiceServerError, stt

    try:
        reply = await asyncio.wait_for(
            stt(
                audio, filename="voicemail.wav", content_type="audio/wav", language="de",
                auth_token=_service_token(), timeout_s=STT_TIMEOUT_S, embed=False,
            ),
            STT_TIMEOUT_S + 5,
        )
    except VoiceServerError as e:
        code = e.status_code
        if code is not None and 400 <= code < 500 and code != 429:
            raise _Fail(f"voice-server refused ({code})") from e
        raise _Retry(f"voice-server failed ({code})") from e
    except TimeoutError as e:
        raise _Retry("voice-server timed out") from e
    # An older voice-server ignores embed=false and computes the voiceprint anyway:
    # only an explicit confirmation counts, the text of any other reply is dropped.
    if not isinstance(reply, dict) or reply.get("embedded") is not False or reply.get("speaker_embedding") is not None:
        raise _Fail("voice-server did not confirm embedded:false")
    return _clean(reply.get("text") or "", MAX_TRANSCRIPT_CHARS)


async def apply_transcript(db, call_id: str, text: str) -> list[int]:
    """Write the text into the call's voicemail rows; returns the users to refresh.

    Only rows that exist for this call (the original recipients, never
    recomputed), not dismissed, not expired; ``expires_at`` is not touched. A row
    that already carries a ``transcript_status`` is left alone (a repeated job)."""
    from sqlalchemy import or_, select

    from models.database import NOTIFICATION_DISMISSED, Notification, _utcnow

    rows = (
        await db.execute(
            select(Notification)
            .where(
                Notification.event_type == EVENT_TYPE,
                Notification.source_data["call_id"].as_string() == call_id,
                Notification.status != NOTIFICATION_DISMISSED,
                or_(Notification.expires_at.is_(None), Notification.expires_at > _utcnow()),
            )
            .with_for_update()
        )
    ).scalars().all()
    refresh: list[int] = []
    for row in rows:
        data = dict(row.source_data or {})
        if data.get("transcript_status"):
            continue
        if text:
            row.message = f"{row.message}: „{text}“ {NOTE}"
            row.privacy = "confidential"  # third-party words: never on a shared display
            data["transcript_status"] = "done"
            if row.target_user_id is not None:
                refresh.append(row.target_user_id)
        else:
            data["transcript_status"] = "empty"
        row.source_data = data  # a new dict: JSON columns do not track in-place changes
    return refresh


async def _claim():
    from sqlalchemy import select

    from models.database import VOICEMAIL_PENDING, TelephonyVoicemail, _utcnow
    from services.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        now = _utcnow()
        job = (
            await db.execute(
                select(TelephonyVoicemail)
                .where(TelephonyVoicemail.status == VOICEMAIL_PENDING, TelephonyVoicemail.next_attempt_at <= now)
                .order_by(TelephonyVoicemail.next_attempt_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if job is None:
            return None
        job.attempts += 1
        job.next_attempt_at = now + timedelta(seconds=LEASE_S)  # the lease
        claimed = (job.id, job.call_id, job.recording, job.attempts)
        await db.commit()
        return claimed


async def _settle(db, job_id: int, status: str, delay_s: float | None = None) -> None:
    from sqlalchemy import update

    from models.database import TelephonyVoicemail, _utcnow

    values = {"status": status, "updated_at": _utcnow()}
    if delay_s is not None:
        values["next_attempt_at"] = _utcnow() + timedelta(seconds=delay_s)
    await db.execute(update(TelephonyVoicemail).where(TelephonyVoicemail.id == job_id).values(**values))


async def process_one() -> bool:
    """Run one due job. False = nothing was due."""
    from models.database import VOICEMAIL_DONE, VOICEMAIL_FAILED, VOICEMAIL_PENDING
    from services.database import AsyncSessionLocal
    from services.notification_service import NotificationService

    claimed = await _claim()
    if claimed is None:
        return False
    job_id, call_id, recording, attempts = claimed
    try:
        text = await transcribe(await fetch_recording(recording))
        async with AsyncSessionLocal() as db:
            refresh = await apply_transcript(db, call_id, text)
            await _settle(db, job_id, VOICEMAIL_DONE)
            await db.commit()  # text and "done" together: a crash in between repeats the job
        for user_id in refresh:
            await NotificationService._emit_changed(user_id, "updated")
        logger.info("Voicemail transcript job %d done (%d notification(s))", job_id, len(refresh))
        return True
    except _Fail as e:
        status, delay, why = VOICEMAIL_FAILED, None, str(e)
    except Exception as e:  # _Retry and anything unexpected
        why = str(e) if isinstance(e, _Retry) else type(e).__name__
        if attempts >= MAX_ATTEMPTS:
            status, delay = VOICEMAIL_FAILED, None
        else:
            status, delay = VOICEMAIL_PENDING, RETRY_DELAYS_S[min(attempts, len(RETRY_DELAYS_S)) - 1]
    logger.warning("Voicemail transcript job %d attempt %d/%d: %s -> %s", job_id, attempts, MAX_ATTEMPTS, why, status)
    try:
        async with AsyncSessionLocal() as db:
            await _settle(db, job_id, status, delay)
            await db.commit()
    except Exception as e:  # the lease runs out and the job comes back
        logger.warning("Voicemail transcript job %d not settled: %s", job_id, error_text_without_content(e))
    return True


async def run_voicemail_transcripts() -> None:
    """The backend loop (lifecycle task). Picks up pending jobs on start as well."""
    global _wake
    if not enabled():
        logger.warning("VOICEMAIL_TRANSCRIPT_ENABLED needs TELEPHONY_ENABLED and VOICE_SERVER_URL — not started")
        return
    _wake = asyncio.Event()
    while True:
        _wake.clear()  # before the claim: an enqueue during the job wakes the next round
        try:
            busy = await process_one()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # e.g. the database is down: wait and try again
            logger.warning("Voicemail transcript loop: %s", error_text_without_content(e))
            busy = False
        if busy:
            continue
        try:
            await asyncio.wait_for(_wake.wait(), IDLE_POLL_S)
        except TimeoutError:
            pass
