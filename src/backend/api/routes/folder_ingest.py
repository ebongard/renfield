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
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.knowledge import _worker_is_alive
from models.database import FOLDER_INGEST_SOURCE
from models.permissions import Permission
from services.api_rate_limiter import limiter
from services.auth_service import require_permission
from services.database import get_db
from services.folder_ingest import (
    FOLDER_INGEST_CONTRACT_VERSION,
    IngestMeta,
    IngestStatus,
    generate_folder_ingest_token,
    ingest_document,
    resolve_owner_user_id,
    resolve_target_kb,
    target_kb_exists,
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


class FolderIngestTokenResponse(BaseModel):
    token: str


class FolderIngestHealthResponse(BaseModel):
    """Config snapshot the filesystem MCP pings on startup + periodically to
    detect two-surface mismatches (DX-1) — a wrong token surfaces as 401/403
    (fatal in the MCP), everything else is reported here so the MCP can log one
    loud error instead of silently misrouting every file."""

    enabled: bool
    kb_name: str
    kb_resolved: bool
    token_ok: bool
    max_file_size_mb: int
    allowed_extensions: list[str]
    contract_version: str = FOLDER_INGEST_CONTRACT_VERSION


def _should_file_paperless() -> bool:
    """Whether this push should be filed into Paperless (Design Z). The push no
    longer performs the Paperless round-trip itself — it only stamps the
    document ``paperless_state='pending'`` so the out-of-band
    ``paperless_reconciler`` files it later (own session, bounded concurrency).
    So the decision is just the feature flag; the MCP manager is needed at
    reconcile time, not here."""
    return bool(settings.folder_ingest_to_paperless)


@router.post("/document", response_model=FolderIngestResponse)
@limiter.limit(settings.api_rate_limit_ingest)
async def ingest_pushed_document(
    request: Request,
    file: UploadFile = File(...),
    metadata: str = Form(...),
    authorization: str | None = Header(None),
    x_folder_ingest_contract: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Receive one pushed file from the filesystem MCP and run it through the
    shared folder-ingest bridge. See the module docstring for the contract."""
    # Contract-version skew check (DX-7). The MCP sends its contract version in
    # the X-Folder-Ingest-Contract header; a mismatch is logged loudly (and the
    # response always carries OUR version so the MCP can detect skew too) but is
    # NOT fatal here — the request shape has been backward-compatible so far, so
    # we process leniently rather than reject a file over a version bump.
    if (
        x_folder_ingest_contract
        and x_folder_ingest_contract != FOLDER_INGEST_CONTRACT_VERSION
    ):
        logger.warning(
            f"folder-ingest: contract skew — MCP sent "
            f"{x_folder_ingest_contract!r}, backend is "
            f"{FOLDER_INGEST_CONTRACT_VERSION!r}; processing leniently"
        )

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

    # 6. Resolve the server-side target KB + owner, build the Paperless leg
    # (when filing is enabled), then delegate to the bridge.
    #
    # Any unexpected failure here (Redis enqueue down, concurrent KB-create
    # IntegrityError, …) must surface as a 503/retry, NOT a raw 500: the MCP
    # leaves the file in the inbox and re-pushes rather than mis-moving it. The
    # bridge already maps its own known errors to FAILED/RETRY; this guards the
    # residual transient ones so the 4-state transport contract never breaks.
    try:
        kb = await resolve_target_kb(db)
        owner_user_id = await resolve_owner_user_id(db)
        result = await ingest_document(
            file_bytes,
            meta,
            db=db,
            kb_id=kb.id,
            owner_user_id=owner_user_id,
            default_tier=settings.folder_ingest_default_tier,
            file_to_paperless=_should_file_paperless(),
            source=FOLDER_INGEST_SOURCE,  # provenance for the Simba review flow
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


@router.get("/health", response_model=FolderIngestHealthResponse)
@limiter.limit(settings.api_rate_limit_default)
async def health(
    request: Request,
    authorization: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Config-alignment handshake for the filesystem MCP (DX-1). Bearer-auth'd
    with the same token as the push: a wrong/missing token is 401/403 (fatal in
    the MCP). Unlike the push route this does NOT 503 when disabled — the
    ``enabled`` flag is reported in the body so the MCP can tell "backend up,
    feature off" (definitive, stop) from the push route's transient 503."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    if not await verify_folder_ingest_token(db, token):
        raise HTTPException(status_code=403, detail="Invalid folder-ingest token")

    return FolderIngestHealthResponse(
        enabled=settings.folder_ingest_enabled,
        kb_name=settings.folder_ingest_kb_name,
        kb_resolved=await target_kb_exists(db),
        token_ok=True,  # reaching here means the presented token matched
        max_file_size_mb=settings.max_file_size_mb,
        allowed_extensions=settings.allowed_extensions_list,
    )


@router.post("/token", response_model=FolderIngestTokenResponse)
@limiter.limit(settings.api_rate_limit_admin)
async def mint_token(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_permission(Permission.SETTINGS_MANAGE)),
):
    """Generate or rotate the folder-ingest Bearer token (admin). Without this
    there is no way to produce the token the whole feature depends on (DX-2).
    Returns the plaintext token once — store it in the MCP's secret."""
    token = await generate_folder_ingest_token(db)
    return FolderIngestTokenResponse(token=token)
