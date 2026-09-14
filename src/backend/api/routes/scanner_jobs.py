"""Scan-job completion events from renfield-mcp-scanner.

The scanner authenticates with the SAME folder-ingest token it pushes documents
with — it already holds it, so no new secret exists. The body carries no user
identity: the requester was recorded server-side when the scan started, and a
job this instance did not record is ignored (see services/scanner_jobs.py).

Replies: 403 for a bad token, 404 when folder ingest is off (final), 409 for a
job this instance has not recorded (retryable — the outcome may simply have
outrun the requester record), 5xx when the message could not be written
(retryable), 2xx otherwise. The scanner treats 401/403/404 as final and retries
anything else.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field
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
    documents: list[Any] = Field(default_factory=list, max_length=500)
    skipped: list[Any] = Field(default_factory=list, max_length=500)
    # A code, never the free-form error text: the message is rendered from fixed
    # localised templates (see services/scanner_jobs.render_completion_message).
    error_code: str | None = Field(default=None, max_length=40)


class ScanJobEvent(BaseModel):
    contract_version: str = Field(default="", max_length=16)
    job_id: str = Field(pattern=f"^{JOB_ID_PATTERN}$")
    status: str = Field(max_length=32)
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
        # to report. 404 is final for the scanner, so it stops retrying.
        raise HTTPException(status_code=404, detail="Folder ingest is disabled")
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
    if outcome == "unknown_job":
        # NOT a 2xx: a scan that fails instantly (no device, empty feeder) can
        # report back before this instance has recorded who asked. 409 makes the
        # scanner retry with backoff until the record exists — and an event sent
        # to the wrong instance ends as a loud give-up instead of a silent
        # "delivered". Never 404, which the scanner treats as final.
        raise HTTPException(status_code=409, detail="unknown job")
    return {"status": outcome}
