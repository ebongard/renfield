"""Folder-ingest push endpoint (T3) — the REST seam the dedicated
``renfield-mcp-filesystem`` server pushes settled new files to.

One route: ``POST /api/folder-ingest/document`` (multipart: ``file`` +
``metadata`` JSON). Authenticated by a revocable Bearer token in SystemSetting
(NOT a user JWT), like the notifications webhook. The backend never mounts the
share — the bytes ride the multipart body.

The body's ``status`` is the load-bearing 4-state contract (D9): the MCP moves
the source file by it (``ingested|duplicate`` → processed/, ``retry`` → leave in
inbox, ``failed`` → failed/). All four are HTTP 200. Transport-level failures
use status codes the MCP maps separately: ``503`` → retry (feature disabled or
worker down), ``401/403`` → fatal config error (token) — never a file move.
"""

import json

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.knowledge import _worker_is_alive
from models.database import KnowledgeBase, User
from services.api_rate_limiter import limiter
from services.database import get_db
from services.folder_ingest import (
    FOLDER_INGEST_CONTRACT_VERSION,
    IngestMeta,
    IngestStatus,
    ingest_document,
    verify_folder_ingest_token,
)
from utils.config import settings

router = APIRouter()

_CHUNK = 1024 * 1024  # 1 MiB streaming read


class FolderIngestResponse(BaseModel):
    """The 4-state push response. ``status`` ∈ ingested|duplicate|retry|failed."""

    status: str
    document_id: int | None = None
    detail: str | None = None
    contract_version: str = FOLDER_INGEST_CONTRACT_VERSION


async def _resolve_target_kb(db: AsyncSession) -> KnowledgeBase:
    """Get-or-create the configured folder-ingest target KB. Mirrors the
    chat-upload default-KB pattern so a fresh install just works."""
    kb = (
        await db.execute(
            select(KnowledgeBase).where(
                KnowledgeBase.name == settings.folder_ingest_kb_name
            )
        )
    ).scalar_one_or_none()
    if kb:
        return kb
    kb = KnowledgeBase(
        name=settings.folder_ingest_kb_name,
        description="Auto-ingested documents from watched folders",
    )
    db.add(kb)
    await db.commit()
    await db.refresh(kb)
    return kb


async def _resolve_owner_user_id(db: AsyncSession) -> int | None:
    """Resolve ``folder_ingest_target_user`` (username or numeric id) to a user
    id. Empty config → None (the bridge/worker handle an ownerless enqueue the
    same way the upload route does for unauthenticated single-user mode)."""
    target = settings.folder_ingest_target_user.strip()
    if not target:
        return None
    user = (
        await db.execute(select(User).where(User.username == target))
    ).scalar_one_or_none()
    if user is None and target.isdigit():
        user = (
            await db.execute(select(User).where(User.id == int(target)))
        ).scalar_one_or_none()
    if user is None:
        logger.warning(
            f"folder-ingest: target_user {target!r} not found; enqueuing ownerless"
        )
        return None
    return user.id


@router.post("/document", response_model=FolderIngestResponse)
@limiter.limit(settings.api_rate_limit_default)
async def ingest_pushed_document(
    request: Request,
    file: UploadFile = File(...),
    metadata: str = Form(...),
    authorization: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Receive one pushed file from the filesystem MCP and run it through the
    shared folder-ingest bridge. See the module docstring for the contract."""
    # 1. Feature gate — a forgotten flag is a transient "nothing's happening"
    # (retry), distinct from the token-fatal path below (DX-3). The MCP's
    # health probe (T14) is the loud disabled-detector; here we just 503.
    if not settings.folder_ingest_enabled:
        raise HTTPException(
            status_code=503,
            detail={"message": "Folder ingest is disabled", "reason": "feature_disabled"},
        )

    # 2. Bearer token (constant-time). 401 = missing header, 403 = wrong token;
    # both are FATAL config errors in the MCP (do not move the file to failed/).
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    if not await verify_folder_ingest_token(db, token):
        raise HTTPException(status_code=403, detail="Invalid folder-ingest token")

    # 3. Worker-alive gate (reuse knowledge.py:_worker_is_alive). Enqueuing into
    # a stream nobody consumes would orphan the doc → 503/retry instead.
    if not await _worker_is_alive():
        raise HTTPException(
            status_code=503,
            detail={"message": "Document worker unavailable", "reason": "worker_unavailable"},
        )

    # 4. Parse metadata. Malformed → failed (the MCP must move it to failed/, not
    # retry forever — garbage metadata won't self-heal). A client-supplied
    # knowledge_base_id is ignored: IngestMeta does not read it, so the token's
    # single (target_user, kb_name) scope cannot be overridden by the pusher.
    try:
        meta = IngestMeta.from_dict(json.loads(metadata))
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.warning(f"folder-ingest: malformed metadata: {exc}")
        return FolderIngestResponse(status=IngestStatus.FAILED.value, detail="malformed_metadata")

    # 5. Stream the body with an early size abort — never clone an unbounded
    # `await file.read()` into RAM. Oversize → failed (terminal, won't shrink).
    max_bytes = settings.max_file_size_mb * 1024 * 1024
    buf = bytearray()
    while chunk := await file.read(_CHUNK):
        buf.extend(chunk)
        if len(buf) > max_bytes:
            logger.warning(
                f"folder-ingest: {meta.filename!r} exceeds "
                f"{settings.max_file_size_mb} MB ceiling (stream abort)"
            )
            return FolderIngestResponse(status=IngestStatus.FAILED.value, detail="file_too_large")
    file_bytes = bytes(buf)

    # 6. Resolve the server-side target KB + owner, then delegate to the bridge.
    # paperless_leg stays None until T5 wires the real PaperlessMetadataExtractor
    # leg; for now Paperless filing is recorded as settled by the bridge's no-op.
    #
    # Any unexpected failure here (Redis enqueue down, concurrent KB-create
    # IntegrityError, …) must surface as a 503/retry, NOT a raw 500: the MCP
    # leaves the file in the inbox and re-pushes rather than mis-moving it. The
    # bridge already maps its own known errors to FAILED/RETRY; this guards the
    # residual transient ones so the 4-state transport contract never breaks.
    try:
        kb = await _resolve_target_kb(db)
        owner_user_id = await _resolve_owner_user_id(db)
        result = await ingest_document(
            file_bytes,
            meta,
            db=db,
            kb_id=kb.id,
            owner_user_id=owner_user_id,
            paperless_leg=None,
        )
    except Exception as exc:  # noqa: BLE001 - never 500 the push contract
        logger.error(f"folder-ingest: unexpected error processing {meta.filename!r}: {exc}")
        raise HTTPException(
            status_code=503,
            detail={"message": "Transient ingest error", "reason": "internal_error"},
        ) from exc
    return FolderIngestResponse(
        status=result.status.value,
        document_id=result.document_id,
        detail=result.detail,
    )
