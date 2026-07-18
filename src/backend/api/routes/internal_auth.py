"""Internal token verification endpoint for downstream services.

The shared voice-server is the consumer: in ``callback`` or ``registry``
auth mode (voice-server PR #987) it POSTs every JWT it sees to this
endpoint instead of holding a copy of the backend's ``SECRET_KEY``. The
backend stays the single source of truth for token issuance + revocation —
logout, blacklist, and user deactivation take effect on the next
voice-server connection without rotating keys in two places.

First deployment that needs it: renfield-xidra's row in the shared
voice-server's ``AUTH_CLIENTS`` registry (voice-server extraction plan T6;
the reference implementation shipped in Reva 2026-05-08 and this is its
upstream port — posture mirrors ``services/websocket_auth.py`` so
voice-server's strictness matches the existing webchat path):

- JWT signature + ``type == "access"``
- ``jti`` not in the revocation blacklist
- For user tokens: User row exists and ``is_active`` is True
- For service-account tokens (``sub="service:..."`` minted in-process by
  whisper_service / piper_service / meeting-worker for non-route callers):
  JWT validity alone, no DB lookup

Contract (pinned by voice-server's ``_verify_via``):

- ``POST {"token": "<jwt>"}``
- ``200`` + JSON payload containing a ``user_id`` field on accept.
- ``401`` with a single opaque ``"unauthorized"`` detail on reject —
  rejection-reason strings would be a free oracle for anyone with network
  reach. Voice-server reads only the status code.

The endpoint is unauthenticated by design — voice-server has no other
credential to present. Constrain network exposure (ingress allowlist /
NetworkPolicy) at the deployment layer.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="unauthorized",
    )


class VerifyRequest(BaseModel):
    token: str


@router.post("/auth/verify")
async def verify_token(request: VerifyRequest) -> dict:
    # Imported lazily so this module can be imported during test collection
    # without dragging the auth + Redis + DB stack into scope.
    from services.auth_service import decode_token, get_user_by_id
    from services.database import AsyncSessionLocal
    from services.token_blacklist import token_blacklist

    payload = decode_token(request.token)
    if not payload or payload.get("type") != "access":
        raise _unauthorized()

    jti = payload.get("jti")
    if jti and await token_blacklist.is_blacklisted(jti):
        raise _unauthorized()

    sub = payload.get("sub")
    if not sub:
        raise _unauthorized()

    if not sub.startswith("service:"):
        try:
            user_id_int = int(sub)
        except (TypeError, ValueError):
            raise _unauthorized() from None

        try:
            async with AsyncSessionLocal() as db:
                user = await get_user_by_id(db, user_id_int)
        except Exception as e:  # noqa: BLE001 — DB outage closes voice fail-safe
            logger.warning("verify_token: user lookup failed: %s", e)
            raise _unauthorized() from e

        if user is None or not user.is_active:
            raise _unauthorized()

    # ``jti`` is the revocation handle — voice-server doesn't need it and
    # shouldn't store it. ``exp`` is irrelevant once validated. Everything
    # else (username, scope) flows through.
    response = {k: v for k, v in payload.items() if k not in {"exp", "jti"}}
    response["user_id"] = str(sub)
    return response
