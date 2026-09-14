"""Scan-job completion events from renfield-mcp-scanner.

The scanner authenticates with the SAME folder-ingest token it pushes documents
with — it already holds it, so no new secret exists. The body carries no user
identity: the requester was recorded server-side when the scan started, and a
job this instance did not record is ignored (see services/scanner_jobs.py).

Replies: 403 for a bad token, 2xx for everything else — including an unknown job
or a non-terminal status. The scanner treats 401/403/404 as final and retries
anything else, so an unknown job must NOT be a 404 of its own making, and a real
failure to write the message surfaces as a 5xx so the scanner tries again.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from services.database import get_db
from services.folder_ingest import resolve_folder_ingest_client
from services.scanner_jobs import TERMINAL_STATUSES, handle_job_event

router = APIRouter()


class ScanJobEvent(BaseModel):
    contract_version: str = ""
    job_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    status: str = Field(max_length=32)
    title: str = Field(default="", max_length=500)
    started_at: str | None = None
    finished_at: str | None = None
    result: dict = Field(default_factory=dict)


@router.post("/job-event")
async def scan_job_event(
    event: ScanJobEvent,
    authorization: str = Header(default=""),
    db: AsyncSession = Depends(get_db),
) -> dict:
    token = authorization.removeprefix("Bearer ").strip()
    if not token or await resolve_folder_ingest_client(db, token) is None:
        raise HTTPException(status_code=403, detail="Invalid token")
    if event.status not in TERMINAL_STATUSES:
        return {"status": "ignored", "detail": "not a terminal status"}
    return {"status": await handle_job_event(db, event.model_dump())}
