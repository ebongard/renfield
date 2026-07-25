"""Inbound MCP-health report endpoint.

The ingest MCPs (renfield-mcp-filesystem / renfield-mcp-email-ingest) already DETECT
their own failures (SMB-auth, IMAP-drop, retry-exhausted, fatal token) and fire an
``OPERATOR-NOTIFY`` — which, without a webhook, dead-ends in container logs. Pointing
their ``*_NOTIFY_WEBHOOK_URL`` at this endpoint routes those failures to a user-facing
surface + a proactive alert (``services/mcp_health_monitor``). Same revocable Bearer
token as folder-ingest (the MCPs already hold it)."""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from fastapi import Depends
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from services.database import get_db
from services.folder_ingest import verify_folder_ingest_token
from services.mcp_health_monitor import ingest_report

router = APIRouter()


@router.post("/report")
async def report_mcp_health(
    payload: dict,
    authorization: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Record an ingest MCP's OPERATOR-NOTIFY (``{source, event, reason, root?}``) →
    surface + proactively alert. Bearer-auth (folder-ingest token). Never raises into
    the MCP's notify path beyond the auth gate — a malformed body is just ignored."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    if not await verify_folder_ingest_token(db, token):
        raise HTTPException(status_code=403, detail="Invalid token")
    try:
        await ingest_report(payload if isinstance(payload, dict) else {})
    except Exception as e:  # noqa: BLE001 — never fail the MCP's fire-and-forget notify
        logger.warning(f"mcp-health report handling failed: {e}")
    return {"status": "recorded"}
