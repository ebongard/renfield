"""
Federation audit API — asker-side "what did I ask, when, what came back".

Mounted under `/api/federation/audit`. Authenticated — always scoped
to the caller's own `user_id`. There is no admin escape hatch: even
an admin cannot read another user's audit log, because the federation
query trail is as sensitive as the conversation history itself.

The one exception is `/audit/revocations` (#1558): a record that a
pairing or person link ended carries no content, and an `admin` holder
sees all of them -- an admin may have ended one on someone else's behalf.

Retention is handled out-of-band by `services.federation_audit.prune_old_audit_rows`
scheduled from `api.lifecycle`.

Design ref: F4d of the v2 federation lanes.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from models.database import User
from models.permissions import Permission
from services.auth_service import get_user_or_default
from services.federation_audit import (
    list_audit_for_user,
    list_revocations_for_user,
    outcome_code,
    outcome_sentence,
)

router = APIRouter()


class FederationAuditEntry(BaseModel):
    id: int
    peer_user_id: int | None
    peer_pubkey: str
    peer_display_name: str
    #: The instance part of the peer's label AS SNAPSHOTTED when the row was
    #: written. `None` on rows from before 2026-11-13 and wherever no instance
    #: label existed at the time -- the view then shows `peer_display_name`
    #: alone. 🛑 Never filled in from today's pairing: that would put a name
    #: on an old access that nobody saw when it happened.
    peer_instance_name: str | None = None
    query_text: str
    initiated_at: str
    finalized_at: str | None
    final_status: str
    verified_signature: bool
    answer_excerpt: str | None
    #: The SENTENCE for the reader, without the code that leads it in the
    #: row. On a `success` row it is a note about how the answer came about
    #: (e.g. made without graph expansion), not an error.
    error_message: str | None
    #: What happened, as a code (`pairing_revoked`, `link_revoked`, `gone`,
    #: `cancelled`, `graph_expansion_skipped`) and how far the access had got
    #: (`before_read`, `after_read`, `after_answer`). `None` where the row
    #: carries no code: a plain failure, and every row from before 2026-11-16.
    outcome_code: str | None = None
    outcome_stage: str | None = None
    #: "asked" = this instance asked, "answered" = this instance answered.
    direction: str
    #: WHICH identity the answer rested on -- "mapped" (as the
    #: linked person, with that person's own full reach) or "peer_tier" (as a
    #: foreign instance, only the granted tier).
    #:
    #: 🛑 `None` does NOT mean "peer_tier" but "not recorded": on asker rows this
    #: instance does not know (the far side decides), and rows from before
    #: 2026-10-29 do not know the field at all. The UI has to tell those apart,
    #: or it claims the narrower tier for queries that in truth ran with full
    #: reach.
    identity_basis: str | None


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
    current_user: User = Depends(get_user_or_default),
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
    # 🛑 Only rows THIS instance wrote as the responder carry a code. On an
    # asked row the column holds the far side's message; it is shown as it
    # came, whatever it looks like.
    def _outcome(row) -> tuple[str | None, str | None]:
        if row.direction != "answered":
            return (None, None)
        return outcome_code(row.error_message) or (None, None)

    def _sentence(row) -> str | None:
        if row.direction != "answered":
            return row.error_message
        return outcome_sentence(row.error_message)

    entries = [
        FederationAuditEntry(
            id=row.id,
            direction=row.direction,
            identity_basis=row.identity_basis,
            peer_user_id=row.peer_user_id,
            peer_pubkey=row.peer_pubkey_snapshot,
            peer_display_name=row.peer_display_name_snapshot,
            peer_instance_name=row.peer_instance_name_snapshot,
            query_text=row.query_text,
            initiated_at=row.initiated_at.isoformat() if row.initiated_at else "",
            finalized_at=row.finalized_at.isoformat() if row.finalized_at else None,
            final_status=row.final_status,
            verified_signature=row.verified_signature,
            answer_excerpt=row.answer_excerpt,
            error_message=_sentence(row),
            outcome_code=_outcome(row)[0],
            outcome_stage=_outcome(row)[1],
        )
        for row in rows
    ]
    return FederationAuditListResponse(
        entries=entries,
        limit=limit,
        offset=offset,
        peer_pubkey=peer_pubkey,
    )


class FederationRevocationEntry(BaseModel):
    """One ended pairing or person link (#1558). Names, never ids of other
    accounts; of the far side only its verified instance name."""

    id: int
    at: str
    #: `pairing_revoked` | `link_revoked`
    action: str
    #: For a link: the larger act it was part of (`pairing_revoked`,
    #: `pairing_revived`); `None` when the link itself was the target.
    cause: str | None = None
    #: On whose authority: `owner` (of the pairing), `self` (the link's
    #: account), `admin` (an admin acting on someone else's row).
    basis: str | None = None
    links_removed: int | None = None
    #: `None` = that account no longer exists.
    subject_name: str | None = None
    actor_name: str | None = None
    owner_name: str | None = None
    subject_is_you: bool
    actor_is_you: bool
    #: The far instance's verified name at the time; `None` when none was
    #: verified or the record predates 2026-11-20.
    peer_instance_name: str | None = None


class FederationRevocationListResponse(BaseModel):
    entries: list[FederationRevocationEntry] = Field(default_factory=list)
    limit: int
    offset: int


@router.get("/audit/revocations", response_model=FederationRevocationListResponse)
async def list_federation_revocations(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_user_or_default),
):
    """Revocation records the caller may see: those that concern them (as
    the account whose link or pairing ended, or as the one who ended it),
    and every record for an `admin` holder.

    Fail-closed: with auth on, `get_user_or_default` answers 401 without a
    valid session; the user id comes from that session, never a parameter.
    """
    entries = await list_revocations_for_user(
        user_id=current_user.id,
        is_admin=current_user.has_permission(Permission.ADMIN.value),
        limit=limit,
        offset=offset,
    )
    return FederationRevocationListResponse(
        entries=[FederationRevocationEntry(**e) for e in entries],
        limit=limit,
        offset=offset,
    )
