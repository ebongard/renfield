"""``internal.ingest_file`` — the interactive folder-ingest agent tool (T6).

The auto path is the REST push (MCP → backend). This is the agent-driven path:
the user points at a file on a watched share and the agent ingests it. The
bytes are pulled through the filesystem MCP (``mcp.files.read_file`` with
``truncate=False`` so the 128 KB execute_tool cap doesn't corrupt the payload),
NOT off the backend disk — the no-mount constraint holds. Everything after the
read goes through the same ``folder_ingest.ingest_document`` bridge as the push
route, so dedup / owner+tier / Paperless filing behave identically.

Dispatched as a platform-core internal tool in ``services/action_executor.py``
(it needs ``mcp_manager`` + the authenticated ``user_id`` injected).
"""

from __future__ import annotations

import base64
import json
import os

from loguru import logger

from services.folder_ingest import (
    IngestMeta,
    IngestStatus,
    ingest_document,
    resolve_owner_user_id,
    resolve_target_kb,
)
from utils.config import settings


def _fail(message: str) -> dict:
    return {"success": False, "message": message, "action_taken": False}


def _unwrap_mcp(raw: dict | None) -> dict:
    """Unwrap the MCPManager envelope to the files-tool's own dict (its return
    is JSON-encoded in ``message``). ``{"error": ...}`` on transport failure /
    unparseable body."""
    if not raw or not raw.get("success"):
        return {"error": (raw or {}).get("message") or "mcp_transport_error"}
    msg = raw.get("message")
    if isinstance(msg, str):
        try:
            inner = json.loads(msg)
            if isinstance(inner, dict):
                return inner
        except (json.JSONDecodeError, TypeError):
            pass
    return {"error": "unparseable_read_file_response"}


async def ingest_file(parameters: dict, *, mcp_manager, user_id: int | None = None) -> dict:
    """Ingest a file the agent points at (``{path}``) via the filesystem MCP.

    Returns the standard internal-tool envelope. The 4-state ingest result is
    surfaced in ``data.status`` so the agent can explain duplicate / retry
    outcomes to the user.
    """
    path = (parameters or {}).get("path")
    if not path or not isinstance(path, str):
        return _fail("ingest_file requires a 'path' string pointing at a watched file.")
    if not settings.folder_ingest_enabled:
        return _fail("Folder ingest is disabled (set FOLDER_INGEST_ENABLED).")
    if mcp_manager is None:
        return _fail("The filesystem MCP is unavailable; cannot read the file.")

    # 1. Pull the full file through the filesystem MCP (truncate=False).
    body = _unwrap_mcp(
        await mcp_manager.execute_tool(
            "mcp.files.read_file", {"path": path, "truncate": False}
        )
    )
    b64 = body.get("content_base64") or body.get("content")
    if body.get("error") or not b64:
        return _fail(f"Could not read {path}: {body.get('error') or 'no content returned'}")
    try:
        # validate=True so a corrupt/truncated payload fails loudly instead of
        # silently dropping non-alphabet chars into garbage bytes (mirrors the
        # paperless MCP's strict decode).
        file_bytes = base64.b64decode(b64, validate=True)
    except Exception:
        return _fail(f"The filesystem MCP returned invalid base64 for {path}.")

    filename = body.get("filename") or os.path.basename(path)
    try:
        meta = IngestMeta.from_dict({"filename": filename, "relpath": path, "root": "internal"})
    except ValueError as exc:
        return _fail(f"Bad file metadata: {exc}")

    # 2. Don't enqueue into a stream no worker is consuming.
    from api.routes.knowledge import _worker_is_alive

    if not await _worker_is_alive():
        return _fail("The document worker is unavailable right now; try again shortly.")

    # 3. Build the Paperless leg (when enabled) + resolve the destination, then
    # run the shared bridge. The asker owns what they ingest (user_id); fall
    # back to the configured target_user only in unauthenticated single-user mode.
    from services.database import AsyncSessionLocal

    leg = None
    if settings.folder_ingest_to_paperless:
        from services.folder_ingest_paperless import make_paperless_leg

        leg = make_paperless_leg(mcp_manager, user_id=user_id)

    async with AsyncSessionLocal() as db:
        kb = await resolve_target_kb(db)
        owner = user_id if user_id is not None else await resolve_owner_user_id(db)
        result = await ingest_document(
            file_bytes,
            meta,
            db=db,
            kb_id=kb.id,
            owner_user_id=owner,
            default_tier=settings.folder_ingest_default_tier,
            paperless_leg=leg,
        )

    logger.info(f"internal.ingest_file: {filename} → {result.status.value} (doc {result.document_id})")
    human = {
        IngestStatus.INGESTED: f"Ingested {filename} (document {result.document_id}); it is being indexed.",
        IngestStatus.DUPLICATE: f"{filename} is already in the knowledge base — nothing to do.",
        IngestStatus.RETRY: f"{filename} couldn't be ingested right now; please try again shortly.",
        IngestStatus.FAILED: f"{filename} was rejected ({result.detail}).",
    }[result.status]
    return {
        "success": result.status is not IngestStatus.FAILED,
        "message": human,
        "action_taken": result.status is IngestStatus.INGESTED,
        "data": {"status": result.status.value, "document_id": result.document_id},
    }
