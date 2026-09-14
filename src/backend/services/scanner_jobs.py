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
from typing import Any

from loguru import logger

from services.user_events import EVENT_SCAN_JOB_FINISHED, publish_user_event
from utils.config import settings

SCAN_DOCUMENT_INTENT = "mcp.scanner.scan_document"
SCAN_JOB_EVENT_CONTRACT_VERSION = "1"

_KEY = "renfield:scanner:job:{job_id}"
# A scan finishes in minutes; a day covers a sleeping scanner host catching up on
# its event retries after wake without keeping requester records around forever.
_TTL_SECONDS = 24 * 3600
_JOB_ID = re.compile(r"[0-9a-f]{32}")
_ERROR_MAX_CHARS = 300

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
    redis: Any = None,
) -> bool:
    """Record who started a scan job. Best-effort: never breaks the tool result.

    Without a session there is no conversation to report into (e.g. a turn that
    carries no chat session), so nothing is recorded — the scan still runs."""
    job_id = extract_job_id(result)
    if job_id is None or not session_id:
        return False
    try:
        if redis is None:
            from services.redis_client import get_redis

            redis = get_redis()
        await redis.set(
            _KEY.format(job_id=job_id),
            json.dumps({"user_id": user_id, "session_id": session_id}),
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
        "doc": " (Renfield-Dokument {id})",
        "unknown_error": "unbekannter Fehler",
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
        "doc": " (Renfield document {id})",
        "unknown_error": "unknown error",
    },
}


def render_completion_message(status: str, title: str, result: dict, lang: str) -> str:
    """The assistant message for a finished job. Plain text, rendered escaped."""
    text = _TEXT.get(lang) or _TEXT["en"]
    title_part = f" „{title}“" if title and lang == "de" else (f" “{title}”" if title else "")
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
    error = str(result.get("error") or text["unknown_error"])[:_ERROR_MAX_CHARS]
    return text["failed"].format(title=title_part, error=error)


async def handle_job_event(db: Any, event: dict, *, redis: Any = None) -> str:
    """Deliver one terminal job event. Returns the outcome for the route's reply.

    - ``unknown_job``: not recorded here (other instance, expired, or forged) — ignored.
    - ``duplicate``: already delivered; the scanner is retrying a lost reply.
    - ``delivered``: appended to the requesting conversation and pushed live.
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
    requester = json.loads(raw)

    # At-most-once into the chat: the scanner retries until it hears a 2xx, so a
    # reply lost after the write would otherwise append the message twice.
    reported_key = f"{key}:reported"
    if not await redis.set(reported_key, "1", nx=True, ex=_TTL_SECONDS):
        return "duplicate"

    content = render_completion_message(
        status, event.get("title") or "", event.get("result") or {}, settings.default_language
    )
    try:
        from services.conversation_service import ConversationService

        message = await ConversationService(db).save_message(
            session_id=requester["session_id"],
            role="assistant",
            content=content,
            metadata={"scanner_job": {"job_id": job_id, "status": status}},
            user_id=requester.get("user_id"),
            enforce_ownership=True,
        )
        if message is None:
            raise RuntimeError("message was not saved")
    except Exception:
        # Release the claim so the scanner's next retry can deliver it.
        await redis.delete(reported_key)
        raise

    # In auth-off mode the household's sockets live in the broadcast bucket.
    target = requester.get("user_id") if settings.ws_auth_enabled else None
    await publish_user_event(redis, target, EVENT_SCAN_JOB_FINISHED, reason=status)
    logger.info(f"scanner: job {job_id} ({status}) reported to its conversation")
    return "delivered"
