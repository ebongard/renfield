"""Email-ingest push endpoint (Phase 1) — the REST seam the dedicated
``renfield-mcp-email-ingest`` watcher pushes email attachments to.

``POST /api/email-ingest/document`` (multipart: ``file`` + ``metadata`` JSON with
``mailbox_id`` + ``message_id`` + ``filename`` [+ ``sender``/``subject``/``sha256``/
``mime``]). Bearer-auth'd by a revocable SystemSetting token (separate from the
folder-ingest token). The backend owns the SPHERE: it resolves ``mailbox_id`` →
``owner/tier/kb`` server-side; the watcher never sends a tier/owner. An unknown
``mailbox_id`` → ``failed``.

Same 4-state transport contract as folder-ingest: ``status`` ∈
ingested|duplicate|retry|failed (all HTTP 200); ``503`` → retry (disabled / no
worker), ``401/403`` → fatal token error.
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
from models.permissions import Permission
from services.api_rate_limiter import limiter
from services.auth_service import require_permission
from services.database import get_db
from services.email_ingest import (
    EMAIL_INGEST_CONTRACT_VERSION,
    generate_email_ingest_token,
    ingest_email_document,
    resolve_mailbox_target,
    verify_email_ingest_token,
)
from services.folder_ingest import IngestStatus
from utils.config import settings

router = APIRouter()

_CHUNK = 1024 * 1024  # 1 MiB streaming read


class EmailIngestResponse(BaseModel):
    """The 4-state push response. ``status`` ∈ ingested|duplicate|retry|failed."""

    status: str
    document_id: int | None = None
    detail: str | None = None
    contract_version: str = EMAIL_INGEST_CONTRACT_VERSION


class EmailIngestTokenResponse(BaseModel):
    token: str


class EmailIngestHealthResponse(BaseModel):
    """Config-alignment handshake for the watcher: it can confirm its configured
    mailbox_ids are known to the backend routing table (the email analog of
    folder-ingest's kb_resolved), so a two-surface mismatch surfaces loudly."""

    enabled: bool
    mailbox_ids: list[str]
    to_paperless: bool
    token_ok: bool
    max_file_size_mb: int
    allowed_extensions: list[str]
    contract_version: str = EMAIL_INGEST_CONTRACT_VERSION


def _build_paperless_leg(request: Request):
    """Real Paperless leg when ``email_ingest_to_paperless`` is on and the MCP
    manager is available, else None (no-op settled). OCR-correspondent only
    (decision #3) — same ``make_paperless_leg`` as folder-ingest. Kept at the
    route edge so the Paperless/MCP import stays out of the bridge.

    ``user_id`` is left None in Phase 1: the per-mailbox owner isn't threaded to
    the extractor's learned-examples (a per-user refinement); the correspondent
    resolve-or-create does not depend on it."""
    if not settings.email_ingest_to_paperless:
        return None
    mcp_manager = getattr(request.app.state, "mcp_manager", None)
    if mcp_manager is None:
        logger.warning(
            "email-ingest: to_paperless is on but no MCP manager — skipping "
            "Paperless filing for this push"
        )
        return None
    from services.folder_ingest_paperless import make_paperless_leg

    # TODO(email-ingest phase 2): thread the per-mailbox owner (ingest_email_document
    # resolves it) into make_paperless_leg(user_id=...) so the Paperless extractor's
    # learned-examples are owner-scoped, as folder-ingest already does. Phase-1-safe:
    # the correspondent resolve-or-create does not depend on user_id.
    return make_paperless_leg(mcp_manager, user_id=None)


@router.post("/document", response_model=EmailIngestResponse)
@limiter.limit(settings.api_rate_limit_default)
async def ingest_pushed_email(
    request: Request,
    file: UploadFile = File(...),
    metadata: str = Form(...),
    authorization: str | None = Header(None),
    x_email_ingest_contract: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Receive one pushed email attachment and run it through the email-ingest
    bridge (which reuses the folder-ingest pipeline). See the module docstring."""
    if (
        x_email_ingest_contract
        and x_email_ingest_contract != EMAIL_INGEST_CONTRACT_VERSION
    ):
        logger.warning(
            f"email-ingest: contract skew — watcher sent {x_email_ingest_contract!r}, "
            f"backend is {EMAIL_INGEST_CONTRACT_VERSION!r}; processing leniently"
        )

    # 1. Feature gate (transient 503/retry).
    if not settings.email_ingest_enabled:
        raise HTTPException(
            status_code=503,
            detail={"message": "Email ingest is disabled", "reason": "feature_disabled"},
        )

    # 2. Bearer token (constant-time). 401 missing, 403 wrong — both fatal in the watcher.
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    if not await verify_email_ingest_token(db, token):
        raise HTTPException(status_code=403, detail="Invalid email-ingest token")

    # 3. Worker-alive gate (enqueuing into a stream nobody consumes → orphan).
    if not await _worker_is_alive():
        raise HTTPException(
            status_code=503,
            detail={"message": "Document worker unavailable", "reason": "worker_unavailable"},
        )

    # 4. Parse metadata. Missing filename/mailbox_id → failed (won't self-heal).
    try:
        raw = json.loads(metadata)
        if not isinstance(raw, dict):
            raise ValueError("metadata is not an object")
        filename = str(raw.get("filename") or "").strip()
        mailbox_id = str(raw.get("mailbox_id") or "").strip()
        if not filename:
            raise ValueError("metadata is missing a filename")
        if not mailbox_id:
            raise ValueError("metadata is missing a mailbox_id")
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.warning(f"email-ingest: malformed metadata: {exc}")
        return EmailIngestResponse(status=IngestStatus.FAILED.value, detail="malformed_metadata")

    # 5. Stream the body with an early size abort. Oversize → failed (terminal).
    max_bytes = settings.max_file_size_mb * 1024 * 1024
    buf = bytearray()
    while chunk := await file.read(_CHUNK):
        buf.extend(chunk)
        if len(buf) > max_bytes:
            logger.warning(
                f"email-ingest: {filename!r} exceeds {settings.max_file_size_mb} MB "
                f"ceiling (stream abort)"
            )
            return EmailIngestResponse(status=IngestStatus.FAILED.value, detail="file_too_large")
    file_bytes = bytes(buf)

    # 6. Delegate to the bridge (server-authoritative sphere routing inside).
    # Any residual transient failure → 503/retry, never a raw 500 — the watcher
    # leaves the email in the inbox and re-pushes rather than mis-moving it.
    try:
        result = await ingest_email_document(
            file_bytes,
            db=db,
            mailbox_id=mailbox_id,
            message_id=str(raw.get("message_id") or "").strip(),
            filename=filename,
            sender=raw.get("sender"),
            subject=raw.get("subject"),
            mime=raw.get("mime"),
            sha256=raw.get("sha256"),
            paperless_leg=_build_paperless_leg(request),
        )
    except Exception as exc:  # noqa: BLE001 - never 500 the push contract
        logger.error(f"email-ingest: unexpected error processing {filename!r}: {exc}")
        raise HTTPException(
            status_code=503,
            detail={"message": "Transient ingest error", "reason": "internal_error"},
        ) from exc
    return EmailIngestResponse(
        status=result.status.value, document_id=result.document_id, detail=result.detail
    )


@router.get("/health", response_model=EmailIngestHealthResponse)
@limiter.limit(settings.api_rate_limit_default)
async def health(
    request: Request,
    authorization: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Config-alignment handshake (Bearer-auth'd). Reports the known mailbox_ids
    so the watcher can detect a routing-table mismatch; does NOT 503 when
    disabled (the ``enabled`` flag is in the body)."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    if not await verify_email_ingest_token(db, token):
        raise HTTPException(status_code=403, detail="Invalid email-ingest token")

    mailbox_ids = [
        str(m.get("id", "")).strip()
        for m in settings.email_ingest_mailboxes
        if resolve_mailbox_target(str(m.get("id", "")))
    ]
    return EmailIngestHealthResponse(
        enabled=settings.email_ingest_enabled,
        mailbox_ids=mailbox_ids,
        to_paperless=settings.email_ingest_to_paperless,
        token_ok=True,
        max_file_size_mb=settings.max_file_size_mb,
        allowed_extensions=settings.allowed_extensions_list,
    )


@router.post("/token", response_model=EmailIngestTokenResponse)
@limiter.limit(settings.api_rate_limit_admin)
async def mint_token(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_permission(Permission.SETTINGS_MANAGE)),
):
    """Generate or rotate the email-ingest Bearer token (admin). Returns the
    plaintext once — store it in the watcher's secret."""
    token = await generate_email_ingest_token(db)
    return EmailIngestTokenResponse(token=token)
