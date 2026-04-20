"""
Federation audit API — asker-side "what did I ask, when, what came back".

Mounted under `/api/federation/audit`. Authenticated — always scoped
to the caller's own `user_id`. There is no admin escape hatch: even
an admin cannot read another user's audit log, because the federation
query trail is as sensitive as the conversation history itself.

Retention is handled out-of-band by `services.federation_audit.prune_old_audit_rows`
scheduled from `api.lifecycle`.

Design ref: F4d of the v2 federation lanes.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from models.database import User
from services.auth_service import get_current_user
from services.federation_audit import list_audit_for_user


router = APIRouter()


class FederationAuditEntry(BaseModel):
    id: int
    peer_user_id: int | None
    peer_pubkey: str
    peer_display_name: str
    query_text: str
    initiated_at: str
    finalized_at: str | None
    final_status: str
    verified_signature: bool
    answer_excerpt: str | None
    error_message: str | None


class FederationAuditListResponse(BaseModel):
    entries: list[FederationAuditEntry] = Field(default_factory=list)
    # Echo back the filter the caller used, so a paginated UI can
    # reconstruct the "next page" URL without re-parsing params.
    limit: int
    offset: int
    peer_pubkey: str | None


@router.get("/audit", response_model=FederationAuditListResponse)
async def list_federation_audit(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    peer_pubkey: str | None = Query(
        None,
        min_length=64,
        max_length=64,
        pattern="^[0-9a-fA-F]{64}$",
    ),
    current_user: User = Depends(get_current_user),
):
    """
    Return this user's own federation query audit, newest first.

    `peer_pubkey` (optional) filters to queries against a single peer.
    Must be the full 64-char hex — partial prefixes are not supported
    here so the UI must round-trip the exact key from the peers page.
    """
    rows = await list_audit_for_user(
        user_id=current_user.id,
        limit=limit,
        offset=offset,
        peer_pubkey=peer_pubkey,
    )
    entries = [
        FederationAuditEntry(
            id=row.id,
            peer_user_id=row.peer_user_id,
            peer_pubkey=row.peer_pubkey_snapshot,
            peer_display_name=row.peer_display_name_snapshot,
            query_text=row.query_text,
            initiated_at=row.initiated_at.isoformat() if row.initiated_at else "",
            finalized_at=row.finalized_at.isoformat() if row.finalized_at else None,
            final_status=row.final_status,
            verified_signature=row.verified_signature,
            answer_excerpt=row.answer_excerpt,
            error_message=row.error_message,
        )
        for row in rows
    ]
    return FederationAuditListResponse(
        entries=entries,
        limit=limit,
        offset=offset,
        peer_pubkey=peer_pubkey,
    )
