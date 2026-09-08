"""Admin CRUD for per-integration ingest credentials (#1218 Phase 2).

Closes the gap that provisioning a machine credential required hand-crafting an
API call. Surfaced on the page that already manages MCP integrations.

Two things this endpoint must get right, both of which the single-token model
could not express:

* **A token value is returned exactly once**, at mint or rotate. The list route
  never returns one, and only a bcrypt hash is stored.
* **The legacy shared token is reported with its SOURCE.** When
  ``FOLDER_INGEST_TOKEN`` is set in the backend env, the boot reconciler
  re-seeds the DB from it, so rotating it in the UI would silently revert at the
  next restart. The UI renders an env-managed credential read-only rather than
  offering an action it cannot make stick.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    SETTING_EMAIL_INGEST_TOKEN,
    SETTING_FOLDER_INGEST_TOKEN,
    SystemSetting,
)
from models.permissions import Permission
from services.api_rate_limiter import limiter
from services.auth_service import require_permission
from services.database import get_db
from services.ingest_credentials import (
    ROUTE_EMAIL,
    ROUTE_FOLDER,
    InvalidClientId,
    list_credentials,
    mint_credential,
    revoke_credential,
    rotate_credential,
    set_client_sphere,
)
from utils.config import settings

router = APIRouter()

_ROUTES = (ROUTE_FOLDER, ROUTE_EMAIL)
# Which env var (if any) is authoritative for each route's legacy shared token.
_LEGACY_ENV = {
    ROUTE_FOLDER: ("folder_ingest_token", SETTING_FOLDER_INGEST_TOKEN),
    ROUTE_EMAIL: ("email_ingest_token", SETTING_EMAIL_INGEST_TOKEN),
}


class CredentialOut(BaseModel):
    client_id: str
    label: str
    route: str
    # Where this client's documents land. null => the global default.
    owner: str | None = None
    tier: int | None = None
    kb_name: str | None = None
    created_at: str | None = None
    rotated_at: str | None = None
    last_authenticated_at: str | None = None
    revoked_at: str | None = None
    is_enabled: bool = True


class LegacyOut(BaseModel):
    """The shared token, if one is still provisioned for this route."""

    route: str
    configured: bool
    # "env" => the boot reconciler re-seeds it; a UI rotation would silently
    # revert at the next restart, so the UI must render it read-only.
    source: str


class CredentialListOut(BaseModel):
    credentials: list[CredentialOut]
    legacy: list[LegacyOut]
    enabled: bool


class MintIn(BaseModel):
    client_id: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=200)
    route: str
    # Optional sphere routing. Set by the ADMIN here, never by the client.
    owner: str | None = None
    tier: int | None = Field(default=None, ge=0, le=4)
    kb_name: str | None = None


class SphereIn(BaseModel):
    owner: str | None = None
    tier: int | None = Field(default=None, ge=0, le=4)
    kb_name: str | None = None


class TokenOut(BaseModel):
    """The plaintext, returned ONCE. Never persisted, never returned again."""

    client_id: str
    token: str


def _iso(value) -> str | None:
    return value.isoformat() if value else None


async def _legacy_state(db: AsyncSession) -> list[LegacyOut]:
    out: list[LegacyOut] = []
    for route, (settings_attr, setting_key) in _LEGACY_ENV.items():
        env_value = (getattr(settings, settings_attr, "") or "").strip()
        row = await db.get(SystemSetting, setting_key)
        configured = bool(env_value) or bool(row and (row.value or "").strip())
        out.append(
            LegacyOut(
                route=route,
                configured=configured,
                source="env" if env_value else "db",
            )
        )
    return out


@router.get("", response_model=CredentialListOut)
@limiter.limit(settings.api_rate_limit_admin)
async def list_all(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_permission(Permission.SETTINGS_MANAGE)),
):
    """List credentials. NEVER returns a token value — only a bcrypt hash is
    stored, and the plaintext existed once, at mint time."""
    rows = await list_credentials(db)
    return CredentialListOut(
        credentials=[
            CredentialOut(
                client_id=r.client_id, label=r.label, route=r.route,
                owner=r.owner, tier=r.tier, kb_name=r.kb_name,
                created_at=_iso(r.created_at), rotated_at=_iso(r.rotated_at),
                last_authenticated_at=_iso(r.last_authenticated_at),
                revoked_at=_iso(r.revoked_at), is_enabled=bool(r.is_enabled),
            )
            for r in rows
        ],
        legacy=await _legacy_state(db),
        enabled=bool(getattr(settings, "ingest_credentials_enabled", False)),
    )


@router.post("", response_model=TokenOut)
@limiter.limit(settings.api_rate_limit_admin)
async def mint(
    request: Request,
    body: MintIn,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_permission(Permission.SETTINGS_MANAGE)),
):
    """Mint a credential. The token is shown ONCE and cannot be recovered."""
    if body.route not in _ROUTES:
        raise HTTPException(status_code=400, detail=f"unknown route: {body.route}")
    try:
        token = await mint_credential(
            db, client_id=body.client_id, label=body.label, route=body.route,
            created_by_user_id=getattr(user, "id", None),
            owner=body.owner, tier=body.tier, kb_name=body.kb_name,
        )
    except InvalidClientId as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    logger.info(f"ingest credential minted via UI: {body.client_id}")
    return TokenOut(client_id=body.client_id.strip().lower(), token=token)


@router.post("/{client_id}/rotate", response_model=TokenOut)
@limiter.limit(settings.api_rate_limit_admin)
async def rotate(
    request: Request,
    client_id: str,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_permission(Permission.SETTINGS_MANAGE)),
):
    """Rotate. The client keeps failing until its copy is updated — the UI warns
    which client that is before the operator confirms."""
    try:
        token = await rotate_credential(db, client_id)
    except InvalidClientId as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return TokenOut(client_id=client_id.strip().lower(), token=token)


@router.delete("/{client_id}")
@limiter.limit(settings.api_rate_limit_admin)
async def revoke(
    request: Request,
    client_id: str,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_permission(Permission.SETTINGS_MANAGE)),
):
    """Revoke. The row is kept (not deleted) so the audit trail survives."""
    try:
        ok = await revoke_credential(db, client_id)
    except InvalidClientId as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if not ok:
        raise HTTPException(status_code=404, detail=f"unknown client_id: {client_id}")
    return {"revoked": True, "client_id": client_id.strip().lower()}


@router.put("/{client_id}/sphere")
@limiter.limit(settings.api_rate_limit_admin)
async def set_sphere(
    request: Request,
    client_id: str,
    body: SphereIn,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_permission(Permission.SETTINGS_MANAGE)),
):
    """Set where this client's documents land (owner / tier / knowledge base).

    Admin-only and server-side by design: the client never sends these, so a
    stolen push token cannot choose an owner, raise a tier, or file elsewhere.
    A null field clears the override back to the global default.
    """
    try:
        ok = await set_client_sphere(
            db, client_id, owner=body.owner, tier=body.tier, kb_name=body.kb_name
        )
    except InvalidClientId as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if not ok:
        raise HTTPException(status_code=404, detail=f"unknown client_id: {client_id}")
    return {"updated": True, "client_id": client_id.strip().lower()}
