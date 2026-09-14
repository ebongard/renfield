"""Scan-job return path: who asked for a scan, and telling them how it ended.

`mcp.scanner.scan_document` only STARTS a scan (renfield-mcp-scanner job model): it
answers with a `job_id` and the scan runs on the scanner host in the background.
When it ends, the scanner POSTs the outcome to `/api/scanner/job-event` of the
instance that requested it — an event, never a poll. This module is both halves of
that seam on our side.

Before the job model the scan ran inside the tool call: on 2026-09-14 the 30s MCP
timeout reported a scan as FAILED that was filed 80ms later, and a longer timeout
only let a refresh tear the call down so the automatic retry scanned an empty
feeder.

Trust boundary. The requester (user + conversation) is recorded from the
AUTHENTICATED turn when the tool returns, keyed by the scanner's job_id. The event
carries no user identity at all, so a push token can never write into somebody's
chat — a job_id this instance did not record is simply ignored.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from loguru import logger

from services.user_events import EVENT_SCAN_JOB_FINISHED, publish_user_event
from utils.config import settings

SCAN_JOB_EVENT_CONTRACT_VERSION = "1"

_KEY = "renfield:scanner:job:{job_id}"
# A scan finishes in minutes; a day covers a sleeping scanner host catching up on
# its event retries after wake without keeping requester records around forever.
_TTL_SECONDS = 24 * 3600
# The delivery claim is a LEASE, not the delivered state: long enough to cover one
# message write, short enough that the scanner's retry (backoff 2, 4, 8 … s)
# delivers soon after a pod died holding it. A write that outlives it is still
# safe — the message check under the per-conversation delivery lock catches the
# overlap.
_CLAIM_TTL_SECONDS = 60
# Advisory-lock namespace for scan-job deliveries ("SJ"); distinct from every
# other two-key advisory lock in the backend (see the *_LOCK_NS constants).
_DELIVERY_LOCK_NS = 0x534A
# Compare-and-delete: only the delivery that took the claim may free it. A
# delivery whose lease already lapsed must not delete the lease a newer one holds.
_RELEASE_CLAIM_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""
# Mirrors renfield-mcp-scanner's job id format (uuid4 hex).
JOB_ID_PATTERN = r"[0-9a-f]{32}"
_JOB_ID = re.compile(JOB_ID_PATTERN)
_TITLE_MAX_CHARS = 200

TERMINAL_STATUSES = frozenset({"done", "unrouted", "failed", "interrupted"})


def extract_job_id(result: dict | None) -> str | None:
    """The job_id from a successful `scan_document` tool result, else None.

    The scanner answers with a JSON object as the tool's text content. A `busy`
    reply carries the RUNNING job's id with ok=false — that job already has its
    requester, so it is deliberately not re-recorded here."""
    if not isinstance(result, dict) or not result.get("success"):
        return None
    try:
        body = json.loads(result.get("message") or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(body, dict) or body.get("ok") is not True:
        return None
    job_id = body.get("job_id")
    return job_id if isinstance(job_id, str) and _JOB_ID.fullmatch(job_id) else None


async def remember_scan_requester(
    result: dict | None,
    *,
    user_id: int | None,
    session_id: str | None,
    title: str = "",
    room_id: int | None = None,
    redis: Any = None,
) -> bool:
    """Record who started a scan job. Best-effort: never breaks the tool result.

    Everything the completion message later shows about the request comes from
    HERE — the authenticated turn — not from the event: the title the requester
    asked for, and the room a voice request came from. Without a session there is
    no conversation to report into, so nothing is recorded; the scan still runs."""
    job_id = extract_job_id(result)
    if job_id is None or not session_id:
        return False
    try:
        if redis is None:
            from services.redis_client import get_redis

            redis = get_redis()
        await redis.set(
            _KEY.format(job_id=job_id),
            json.dumps({"user_id": user_id, "session_id": session_id,
                        "title": title[:_TITLE_MAX_CHARS], "room_id": room_id}),
            ex=_TTL_SECONDS,
        )
        return True
    except Exception as exc:  # noqa: BLE001 - losing the return path must not fail the scan
        logger.warning(f"scanner: could not record requester for job {job_id}: {exc}")
        return False


_TEXT = {
    "de": {
        "done_one": "Der Scan{title} ist fertig: {pages} Seite(n) wurden an „{target}“ übergeben"
                    "{doc}. Die Ablage in Paperless folgt separat.",
        "done_split": "Der Scan{title} ist fertig und wurde in {count} Dokumente aufgeteilt und "
                      "übergeben. Die Ablage in Paperless folgt separat.",
        "unrouted": "Der Scan{title} ist fertig, aber sein Ziel ist noch offen. Er wartet auf dem "
                    "Scanner-Rechner auf eine Zuordnung — nichts wurde abgelegt.",
        "failed": "Der Scan{title} ist fehlgeschlagen: {error}",
        "interrupted": "Der Scan{title} wurde unterbrochen, weil der Scanner-Dienst neu gestartet "
                       "ist. Bitte nicht davon ausgehen, dass etwas abgelegt wurde; wartende "
                       "Scans lassen sich auf dem Scanner prüfen.",
        "doc": " (dort Dokument {id})",
        "unknown_error": "unbekannter Fehler",
        "title": " „{title}“",
        "errors": {
            "unknown_target": "das angegebene Ziel ist nicht konfiguriert",
            "device_unavailable": "der Scanner ist nicht erreichbar",
            "scan_error": "beim Einzug ist ein Fehler aufgetreten",
            "scanner_fault": "der Scanner hat mitten im Stapel einen Fehler gemeldet; die "
                             "bisherigen Seiten liegen auf dem Scanner-Rechner",
            "no_pages": "es wurden keine Seiten eingezogen – liegt Papier im Einzug?",
            "missing_token": "für das Ziel fehlt die Zugangskonfiguration",
            "ingest_rejected": "die Zielinstanz hat das Dokument abgelehnt",
            "push_pending": "das Dokument konnte noch nicht übergeben werden und wartet "
                            "auf dem Scanner-Rechner",
            "crashed": "der Scan-Dienst ist abgestürzt",
        },
        "spoken": {
            "done": "Der Scan ist fertig.",
            "unrouted": "Der Scan ist fertig, das Ziel ist aber noch offen.",
            "failed": "Der Scan ist fehlgeschlagen.",
            "interrupted": "Der Scan wurde unterbrochen.",
        },
    },
    "en": {
        "done_one": "The scan{title} is done: {pages} page(s) were handed to “{target}”{doc}. "
                    "Filing into Paperless follows separately.",
        "done_split": "The scan{title} is done and was split into {count} documents and handed "
                      "over. Filing into Paperless follows separately.",
        "unrouted": "The scan{title} is done, but its destination is still open. It is waiting "
                    "on the scanner host for a routing decision — nothing was filed.",
        "failed": "The scan{title} failed: {error}",
        "interrupted": "The scan{title} was interrupted because the scanner service restarted. "
                       "Do not assume anything was filed; waiting scans can be checked on the "
                       "scanner.",
        "doc": " (document {id} there)",
        "unknown_error": "unknown error",
        "title": " “{title}”",
        "errors": {
            "unknown_target": "the requested destination is not configured",
            "device_unavailable": "the scanner is not reachable",
            "scan_error": "feeding the paper failed",
            "scanner_fault": "the scanner reported a fault mid-stack; the pages so far "
                             "are kept on the scanner host",
            "no_pages": "no pages were fed — is there paper in the feeder?",
            "missing_token": "the destination's access configuration is missing",
            "ingest_rejected": "the destination instance rejected the document",
            "push_pending": "the document could not be handed over yet and is waiting "
                            "on the scanner host",
            "crashed": "the scan service crashed",
        },
        "spoken": {
            "done": "The scan is done.",
            "unrouted": "The scan is done, but its destination is still open.",
            "failed": "The scan failed.",
            "interrupted": "The scan was interrupted.",
        },
    },
}


def render_completion_message(status: str, title: str, result: dict, lang: str) -> str:
    """The assistant message for a finished job. Plain text, rendered escaped.

    Built ONLY from fixed, localised templates: the title comes from the
    requester's own tool call, a failure is described by its `error_code`, and
    no free-form text from the event reaches the chat — it is re-injected into
    later agent turns, so it must not be a prompt-injection channel."""
    text = _TEXT.get(lang) or _TEXT["en"]
    title_part = text["title"].format(title=title) if title else ""
    if status == "done":
        documents = result.get("documents")
        if isinstance(documents, list) and documents:
            return text["done_split"].format(title=title_part, count=len(documents))
        doc_id = result.get("renfield_document_id")
        return text["done_one"].format(
            title=title_part,
            pages=result.get("pages", "?"),
            target=result.get("target") or "?",
            doc=text["doc"].format(id=doc_id) if isinstance(doc_id, int) else "",
        )
    if status == "unrouted":
        return text["unrouted"].format(title=title_part)
    if status == "interrupted":
        return text["interrupted"].format(title=title_part)
    error = text["errors"].get(str(result.get("error_code") or ""), text["unknown_error"])
    return text["failed"].format(title=title_part, error=error)


async def _announce_in_origin_room(requester: dict, status: str) -> None:
    """Speak a short, fixed outcome sentence in the room a voice request came
    from. Best-effort: the message is already in the conversation; this is the
    channel for someone standing at the scanner who never opens that chat.

    Deliberately no title or document detail — it is spoken into a shared room."""
    room_id = requester.get("room_id")
    if not isinstance(room_id, int):
        return
    text = (_TEXT.get(settings.default_language) or _TEXT["en"])["spoken"].get(status)
    if not text:
        return
    try:
        from utils.hooks import run_hooks

        await run_hooks("announce_in_room", room_id=room_id, text=text)
    except Exception as exc:  # noqa: BLE001 - a failed announcement never fails delivery
        logger.warning(f"scanner: outcome announcement in room {room_id} failed: {exc}")


async def _outcome_already_in_conversation(db: Any, session_id: str, job_id: str) -> bool:
    """Whether this job's outcome message already exists — the DURABLE record of a
    delivery, which Redis markers are only a cache of.

    First takes the per-conversation delivery lock,
    ``pg_advisory_xact_lock(_DELIVERY_LOCK_NS, hashtext(session_id))``, in the
    caller's transaction. It is held until that transaction ends — the commit in
    ``save_message``, or the caller's rollback when this returns True — so check
    and insert are one atomic step: a second delivery of the same job waits here
    until the first commits, then finds its message.

    An ADVISORY lock rather than the conversation row lock, because the row may not
    exist yet: ``chat_handler`` saves a turn only when it ends, so a scan requested
    in a brand-new conversation can finish first. With a row lock, two overlapping
    deliveries would then both see no conversation and both append. The advisory
    lock needs no row and creates none, so ``chat_handler``'s own later save (owner,
    title) is untouched. ``hashtext`` collisions only serialize two unrelated
    deliveries; they never merge them.

    Postgres only. SQLite (the unit-test harness) has no advisory locks; there the
    check runs unlocked — deliberately, it serves one test connection, never
    concurrent pods."""
    from sqlalchemy import select, text

    from models.database import Conversation, Message

    if db.get_bind().dialect.name == "postgresql":
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:ns, hashtext(:session_id))"),
            {"ns": _DELIVERY_LOCK_NS, "session_id": session_id},
        )
    conversation_id = (
        await db.execute(select(Conversation.id).where(Conversation.session_id == session_id))
    ).scalar_one_or_none()
    if conversation_id is None:
        return False
    found = (
        await db.execute(
            select(Message.id)
            .where(
                Message.conversation_id == conversation_id,
                Message.role == "assistant",
                Message.message_metadata[("scanner_job", "job_id")].as_string() == job_id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return found is not None


async def _release_claim(redis: Any, claim_key: str, token: str) -> None:
    """Free the claim only if it is still ours. Best-effort: the TTL frees it anyway."""
    try:
        await redis.eval(_RELEASE_CLAIM_LUA, 1, claim_key, token)
    except Exception as exc:  # noqa: BLE001 - the TTL releases it
        logger.warning(f"scanner: could not release claim {claim_key}: {exc}")


async def _mark_reported(redis: Any, reported_key: str, outcome: str) -> None:
    """Cache that this job is settled so a retry answers without touching the DB.
    Best-effort: if it is lost, the next retry finds the message and settles then."""
    try:
        await redis.set(reported_key, outcome, ex=_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 - the message is already durable
        logger.warning(f"scanner: could not cache the delivery marker {reported_key}: {exc}")


async def handle_job_event(db: Any, event: dict, *, redis: Any = None) -> str:
    """Deliver one terminal job event. Returns the outcome for the route's reply.

    - ``unknown_job``: not recorded here (other instance, expired, or forged) — ignored.
    - ``in_progress``: another delivery holds the claim, or its pod died holding
      it — retryable; the retry after the claim lapses delivers.
    - ``duplicate``: already delivered (or refused); the scanner is retrying a lost reply.
    - ``delivered``: appended to the requesting conversation and pushed live.
    - ``refused``: the conversation belongs to another user — final, not retried.

    Delivery guarantee. The scanner sends at-least-once (it retries anything but
    a 2xx/401/403/404 for 24 h); this side makes the message idempotent on the
    job_id, so the conversation gets it exactly once. Three layers, each covering
    the gap the one above leaves:

    1. ``…:reported`` (24 h) — a settled job answers ``duplicate`` from Redis.
    2. ``…:claim`` (``SET NX``, ``_CLAIM_TTL_SECONDS``) — serializes concurrent
       deliveries. It is a LEASE, never the delivered state: a pod that dies
       between claiming and writing leaves only a claim that lapses, and the
       scanner's next retry delivers. (Before 2026-09-14 the claim WAS the
       delivered marker, so that crash lost the outcome for good.) Its value is a
       per-delivery token released only by compare-and-delete, so a delivery whose
       lease already lapsed cannot free the lease a newer delivery holds.
    3. The message itself, marked ``message_metadata.scanner_job.job_id`` and
       checked under a per-conversation advisory lock right before the insert, in
       the same transaction. It covers a crash after the commit but before the
       marker, a lost Redis marker, and a claim that lapsed while a slow write was
       still running — also for a conversation whose row does not exist yet.

    The live side effects — the ``/ws/user`` event and the room announcement —
    are at-most-once: they follow a successful write only, and a delivery found
    already written does not repeat them.
    """
    job_id = event["job_id"]
    status = event["status"]
    if redis is None:
        from services.redis_client import get_redis

        redis = get_redis()

    key = _KEY.format(job_id=job_id)
    raw = await redis.get(key)
    if raw is None:
        logger.info(f"scanner: event for unknown job {job_id} ({status}) ignored")
        return "unknown_job"
    try:
        requester = json.loads(raw)
        session_id = requester["session_id"]
    except (ValueError, KeyError, TypeError):
        logger.warning(f"scanner: unreadable requester record for job {job_id} — ignored")
        return "unknown_job"

    reported_key = f"{key}:reported"
    if await redis.get(reported_key) is not None:
        return "duplicate"
    claim_key = f"{key}:claim"
    claim_token = uuid.uuid4().hex
    if not await redis.set(claim_key, claim_token, nx=True, ex=_CLAIM_TTL_SECONDS):
        return "in_progress"

    try:
        # The title is the requester's own (recorded at request time), never the
        # event's — the event is only trusted for WHICH outcome happened.
        content = render_completion_message(
            status, str(requester.get("title") or ""), event.get("result") or {},
            settings.default_language,
        )
        try:
            if await _outcome_already_in_conversation(db, session_id, job_id):
                await db.rollback()  # release the delivery lock; nothing to write
                await _mark_reported(redis, reported_key, "delivered")
                logger.info(f"scanner: job {job_id} was already in its conversation")
                return "duplicate"

            from services.conversation_service import ConversationService

            message = await ConversationService(db).save_message(
                session_id=session_id,
                role="assistant",
                content=content,
                metadata={"scanner_job": {"job_id": job_id, "status": status}},
                user_id=requester.get("user_id"),
                enforce_ownership=True,
            )
            if message is None:
                raise RuntimeError("message was not saved")
        except PermissionError:
            # The conversation belongs to someone else. That never changes on a
            # retry, so this is final: settle it and answer 2xx — a 5xx would
            # make the scanner hammer a write that can never succeed.
            logger.warning(f"scanner: job {job_id} refused — conversation owned by another user")
            await _mark_reported(redis, reported_key, "refused")
            return "refused"
        await _mark_reported(redis, reported_key, "delivered")
    finally:
        # Free OUR claim on every path, a failed write included, so the scanner's
        # next retry can deliver at once. If Redis is gone too, the TTL frees it.
        await _release_claim(redis, claim_key, claim_token)

    # In auth-off mode the household's sockets live in the broadcast bucket. The
    # session id lets only the tab driving that conversation show the notice.
    target = requester.get("user_id") if settings.ws_auth_enabled else None
    await publish_user_event(
        redis, target, EVENT_SCAN_JOB_FINISHED, reason=status, session_id=session_id,
    )
    await _announce_in_origin_room(requester, status)
    logger.info(f"scanner: job {job_id} ({status}) reported to its conversation")
    return "delivered"
