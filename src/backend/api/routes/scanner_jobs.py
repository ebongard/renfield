"""Scan-job completion events from renfield-mcp-scanner.

The scanner authenticates with the SAME folder-ingest token it pushes documents
with — it already holds it, so no new secret exists. The body carries no user
identity: the requester was recorded server-side when the scan started, and a
job this instance did not record is ignored (see services/scanner_jobs.py).

Replies: 403 for a bad token, 404 when folder ingest is off (final), 409 for a
job this instance has not recorded (retryable — the outcome may simply have
outrun the requester record), 5xx when the message could not be written
(retryable), 2xx otherwise. The scanner treats 401/403 as final and retries
anything else.

A 404 is only final when it carries the application's own marker::

    {"detail": {"reason": "folder_ingest_disabled", "message": "<human text>"}}

Any other 404 — above all one without that body — is RETRYABLE for the scanner.
The number alone cannot be trusted: while no backend pod is ready the ingress
answers 404 itself, and a completion event in flight during a rollout was then
discarded for good (#1391, measured 2026-10-11: two correct 503, then a Traefik
404 that never reached the application). The marker is the only thing that says
"the application saw this request and the feature is off"; the reason string is
what the scanner compares against, so never rename it without changing the
scanner with it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from services.api_rate_limiter import limiter
from services.database import get_db
from services.folder_ingest import resolve_folder_ingest_client
from services.scanner_jobs import (
    JOB_ID_PATTERN,
    SCAN_JOB_EVENT_CONTRACT_VERSION,
    TERMINAL_STATUSES,
    handle_job_event,
)
from utils.config import settings

router = APIRouter()

# The machine-readable marker that makes THIS 404 distinguishable from a
# transient ingress 404 during a rollout (#1391). The scanner treats a 404 as
# final only when ``detail.reason`` equals this string — a cross-repo contract,
# hence a named constant and never an inline literal.
FOLDER_INGEST_DISABLED_REASON = "folder_ingest_disabled"

#: Caller of a scan started by the blue button on the device.
#: Must match `BUTTON_CALLER` in renfield-mcp-scanner/button.py.
BUTTON_CALLER = "button"

#: Marker of a 409 that is FINAL -- the far side may stop retrying.
#:
#: 🛑 A bare 409 "unknown job" is deliberately RETRYABLE (see below): a scan
#: that fails immediately can report before we have recorded who asked. For a
#: BUTTON scan that never applies -- it has no session a completion notice
#: could be bound to, so no entry is ever created here. On 2026-10-21 the
#: scanner therefore retried 409 after 409 against an answer that could not
#: change. Same pattern as the 404: a refusal is only final WITH a marker.
NO_RECIPIENT_REASON = "no_recipient_for_caller"


class ScanJobResult(BaseModel):
    """Only the fields the completion message reads — bounded, extras dropped.

    A free `dict` let any authenticated sender post an arbitrarily large, deeply
    nested object for pydantic to parse."""

    model_config = ConfigDict(extra="ignore")

    ok: bool | None = None
    routed: bool | None = None
    target: str | None = Field(default=None, max_length=64)
    pages: int | None = Field(default=None, ge=0, le=100_000)
    renfield_document_id: int | None = None
    # Id of the staging area for `unrouted` -- the answer to "and which
    # scan?". LENIENT here (length only): a malformed value must not reject
    # the completion notice with a 422. It is validated at USE, in
    # `render_completion_message`, against the known shape; if it does not fit,
    # the id is dropped and the sentence stays as it was.
    stage_id: str | None = Field(default=None, max_length=64)
    documents: list[Any] = Field(default_factory=list, max_length=500)
    skipped: list[Any] = Field(default_factory=list, max_length=500)
    # Page numbers of the back sides the scanner set aside as blank
    # (SCANNER_BLANK_BACKS=drop). Only the LENGTH reaches the message, so the
    # field is LENIENT like `stage_id`: a malformed value must not reject the
    # completion notice with a 422 -- see `_lenient_blank_backs`.
    blank_backs_dropped: list[Any] = Field(default_factory=list)
    # A code, never the free-form error text: the message is rendered from fixed
    # localised templates (see services/scanner_jobs.render_completion_message).
    error_code: str | None = Field(default=None, max_length=40)

    @field_validator("blank_backs_dropped", mode="before")
    @classmethod
    def _lenient_blank_backs(cls, value: Any) -> list[Any]:
        """Not a list → none dropped; longer than the `pages` bound → capped."""
        if not isinstance(value, list):
            return []
        return value[:100_000]


class ScanJobEvent(BaseModel):
    contract_version: str = Field(default="", max_length=16)
    job_id: str = Field(pattern=f"^{JOB_ID_PATTERN}$")
    status: str = Field(max_length=32)
    # Who triggered the scan. Empty for a scanner version that does not send
    # the field yet -- the old, retryable answer then applies.
    caller: str = Field(default="", max_length=64)
    title: str = Field(default="", max_length=500)
    started_at: str | None = Field(default=None, max_length=40)
    finished_at: str | None = Field(default=None, max_length=40)
    result: ScanJobResult = Field(default_factory=ScanJobResult)


@router.post("/job-event")
@limiter.limit(settings.api_rate_limit_ingest)
async def scan_job_event(
    request: Request,
    event: ScanJobEvent,
    authorization: str = Header(default=""),
    db: AsyncSession = Depends(get_db),
) -> dict:
    # Rate-limited like the other ingest routes: every request with a
    # well-formed token costs a bcrypt verify, which is a cheap lever for
    # tying up the worker threads.
    if not settings.folder_ingest_enabled:
        # The scanner pushes through folder ingest; with it off there is nothing
        # to report. 404 is final for the scanner, so it stops retrying — but
        # ONLY because of the marker below: a bare 404 is the ingress speaking
        # for a pod that is not ready yet, and that is the opposite of final.
        raise HTTPException(
            status_code=404,
            detail={
                "reason": FOLDER_INGEST_DISABLED_REASON,
                "message": "Folder ingest is disabled",
            },
        )
    token = authorization.removeprefix("Bearer ").strip() if authorization.startswith("Bearer ") else ""
    client = await resolve_folder_ingest_client(db, token) if token else None
    if client is None:
        raise HTTPException(status_code=403, detail="Invalid token")
    # ONLY the scanner's own ingest credential may report scan outcomes. Every
    # folder-ingest client could otherwise post an event for a known job_id and
    # put text into someone's conversation. Fail-closed: no allowlist, no events.
    allowed = {c.strip() for c in settings.scanner_ingest_client_ids.split(",") if c.strip()}
    if client.client_id not in allowed:
        logger.warning(
            f"scanner job-event refused: ingest client {client.client_id!r} is not in "
            f"SCANNER_INGEST_CLIENT_IDS"
        )
        raise HTTPException(status_code=403, detail="Not a scanner credential")
    if event.contract_version and event.contract_version != SCAN_JOB_EVENT_CONTRACT_VERSION:
        # Lenient like the ingest seam: log the skew, never drop a real outcome.
        logger.warning(
            f"scanner job-event contract skew: scanner={event.contract_version} "
            f"ours={SCAN_JOB_EVENT_CONTRACT_VERSION}"
        )
    if event.status not in TERMINAL_STATUSES:
        return {"status": "ignored", "detail": "not a terminal status"}
    outcome = await handle_job_event(db, event.model_dump())
    if outcome == "unknown_job" and event.caller == BUTTON_CALLER:
        # 🛑 For the BUTTON this refusal is final, and only for it. A completion
        # notice is bound to the `session_id` of the requesting chat
        # session; a button press has no session, so NO entry is ever created
        # here. Without the marker the scanner retried on 2026-10-21
        # against an answer that could not change.
        #
        # The DOCUMENT is unaffected: it arrives over the separate ingest
        # path and was filed long ago. All that remains open is WHO a
        # completion notice would go to -- a button press names no recipient
        # (#1390).
        logger.info(
            f"scanner: button scan {event.job_id} ({event.status}) without a "
            f"report address — document is filed, no report is sent"
        )
        raise HTTPException(
            status_code=409,
            detail={
                "reason": NO_RECIPIENT_REASON,
                "message": "No recipient for a button-initiated scan",
            },
        )
    if outcome == "unknown_job":
        # NOT a 2xx: a scan that fails instantly (no device, empty feeder) can
        # report back before this instance has recorded who asked. 409 makes the
        # scanner retry with backoff until the record exists — and an event sent
        # to the wrong instance ends as a loud give-up instead of a silent
        # "delivered". Never 404: 409 says retryable outright, while a 404 only
        # stays retryable as long as it is kept free of the disabled marker.
        raise HTTPException(status_code=409, detail="unknown job")
    if outcome == "in_progress":
        # Another delivery of this job holds the short claim — or held it and its
        # pod died mid-write. NOT a 2xx: the retry after the claim lapses is what
        # delivers an event a crashed pod never wrote.
        raise HTTPException(status_code=409, detail="delivery in progress")
    return {"status": outcome}
