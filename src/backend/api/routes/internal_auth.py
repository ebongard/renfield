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

Network exposure: mounted under the ``/api`` ingress prefix, so it is
externally reachable. Set ``INTERNAL_AUTH_VERIFY_SECRET`` to gate the endpoint
— an external caller without the secret then gets a plain 401 before any token
work, closing the validity-oracle/claims-leak (login audit). Unset =
unauthenticated (legacy). A deployment-layer ingress/NetworkPolicy allowlist is
still the belt-and-suspenders option.

TWO-SIDED coupling: the shared voice-server sends ``X-Verify-Secret`` only when
its matching per-client secret is set (``auth_callback_secret`` in callback
mode, the registry row's ``verify_secret`` in registry mode) — it is NOT sent
unconditionally. Configuring this secret on the backend without setting the
same value on the voice-server side makes the backend 401 every verify and
breaks that client's voice auth. ``local``-mode voice-servers validate JWTs
offline and never call this endpoint, so setting it there cannot break voice.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel

from utils.config import settings

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
async def verify_token(
    request: VerifyRequest,
    x_verify_secret: str | None = Header(default=None, alias="X-Verify-Secret"),
) -> dict:
    # Shared-secret gate FIRST (before any token work), when configured: an
    # external caller without the secret gets an opaque 401 and learns nothing
    # about the token. Constant-time compare. Unset = legacy unauthenticated.
    configured = settings.internal_auth_verify_secret
    if configured is not None:
        expected = configured.get_secret_value()
        # Compare as bytes: hmac.compare_digest raises TypeError on non-ASCII
        # str, and the header arrives latin-1-decoded — a non-ASCII header value
        # would 500 the gate (ugly + a weak "secret is configured" oracle)
        # instead of returning the intended opaque 401.
        if not x_verify_secret or not hmac.compare_digest(
            x_verify_secret.encode("utf-8"), expected.encode("utf-8")
        ):
            raise _unauthorized()

    # Imported lazily so this module can be imported during test collection
    # without dragging the auth + Redis + DB stack into scope.
    from services.auth_service import decode_token, get_user_by_id
    from services.database import AsyncSessionLocal
    from services.token_blacklist import token_blacklist

    payload = decode_token(request.token)
    if not payload or payload.get("type") != "access":
        raise _unauthorized()

    # A WS-scoped token (security audit M2) is ONLY valid on the browser
    # WebSocket handshake — reject it here so a ~90s ws token harvested from a
    # proxy log can't be replayed as a full voice session within its window.
    if payload.get("scope") == "ws":
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

        # Session-revocation epoch (security audit H3/H4): a token minted before
        # the user's token_epoch was bumped (password change / admin reset) is
        # revoked on REST + WS — enforce it here too so the voice path can't
        # honor a stolen token for its full lifetime after a revoke.
        if int(payload.get("epoch", 0) or 0) < int(getattr(user, "token_epoch", 0) or 0):
            raise _unauthorized()

    # ``jti`` is the revocation handle — voice-server doesn't need it and
    # shouldn't store it. ``exp`` is irrelevant once validated. Everything
    # else (username, scope) flows through.
    response = {k: v for k, v in payload.items() if k not in {"exp", "jti"}}
    response["user_id"] = str(sub)
    return response
