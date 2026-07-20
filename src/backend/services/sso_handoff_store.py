"""One-time SSO hand-off code store (token-in-URL replacement).

After a federated login the backend has a minted application session (access +
refresh JWT). Instead of embedding those tokens in the redirect URL (the OAuth
*implicit flow*, deprecated by RFC 9700), the emitter stores the session here
under a **single-use, short-TTL code** and redirects with only that opaque code.
The SPA then POSTs the code (plus its PKCE ``code_verifier``) to
``/api/auth/sso/exchange`` and receives the tokens in the response body.

Security properties:
- **Single-use**: consumption is an atomic Redis ``GETDEL`` — a replayed code
  finds nothing. No read-then-delete race.
- **Short-lived**: TTL ``settings.sso_handoff_ttl_seconds`` (default 60 s).
- **PKCE-bound**: the stored ``code_challenge`` (S256 of the SPA's verifier) means
  a leaked code is useless without the verifier the initiating browser holds.
- **State-bound**: the ``state`` echoes the SPA's CSRF nonce.
- **Ephemeral**: Redis only, self-expiring — no DB table, nothing persisted.

Design: docs/design/sso-token-handoff-hardening.md.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass

from loguru import logger

from services.redis_client import get_redis
from utils.config import settings

_KEY_PREFIX = "sso:handoff:"


def s256_challenge(verifier: str) -> str:
    """RFC 7636 S256 code_challenge = base64url(SHA256(verifier)), no padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verify_pkce_s256(verifier: str, challenge: str) -> bool:
    """Constant-time check that ``verifier`` matches the stored S256 ``challenge``.

    A too-short verifier is rejected outright (RFC 7636 requires 43-128 chars),
    so a caller can't degrade PKCE by sending a trivial verifier."""
    if not verifier or not challenge or not (43 <= len(verifier) <= 128):
        return False
    return hmac.compare_digest(s256_challenge(verifier), challenge)


@dataclass(frozen=True)
class HandoffSession:
    """The minted session stashed under a one-time code (what /exchange returns)."""
    user_id: int
    access_token: str
    refresh_token: str
    expires_in: int
    code_challenge: str  # S256(code_verifier), base64url, no padding
    state: str
    provider: str
    must_change_password: bool = False


def _key(code: str) -> str:
    return f"{_KEY_PREFIX}{code}"


async def issue_handoff_code(session: HandoffSession) -> str:
    """Store ``session`` under a fresh single-use code and return the code.

    The code is a 256-bit URL-safe token — the only thing that rides in the
    redirect URL. Called by the OIDC/redirect-provider callback (the emitter)
    after it has minted the app session. Caller redirects to
    ``…/auth/callback?code=<code>&state=<session.state>``.
    """
    code = secrets.token_urlsafe(32)  # 256 bits
    payload = json.dumps({
        "user_id": session.user_id,
        "access_token": session.access_token,
        "refresh_token": session.refresh_token,
        "expires_in": session.expires_in,
        "code_challenge": session.code_challenge,
        "state": session.state,
        "provider": session.provider,
        "must_change_password": session.must_change_password,
    })
    # NX so a (astronomically unlikely) code collision never clobbers a live one.
    await get_redis().set(_key(code), payload, ex=settings.sso_handoff_ttl_seconds, nx=True)
    return code


async def consume_handoff_code(code: str) -> HandoffSession | None:
    """Atomically fetch-and-delete the session for ``code`` (single-use).

    Returns None if the code is unknown, already used, or expired. PKCE/state
    verification is the caller's job (it holds the verifier from the request)."""
    if not code:
        return None
    try:
        raw = await get_redis().getdel(_key(code))
    except Exception as e:  # noqa: BLE001 — never leak the store's internals to the caller
        logger.warning(f"sso handoff: GETDEL failed: {e}")
        return None
    if not raw:
        return None
    try:
        d = json.loads(raw)
        return HandoffSession(
            user_id=int(d["user_id"]),
            access_token=d["access_token"],
            refresh_token=d["refresh_token"],
            expires_in=int(d["expires_in"]),
            code_challenge=d["code_challenge"],
            state=d["state"],
            provider=d.get("provider", ""),
            must_change_password=bool(d.get("must_change_password", False)),
        )
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.warning(f"sso handoff: corrupt payload discarded: {e}")
        return None
