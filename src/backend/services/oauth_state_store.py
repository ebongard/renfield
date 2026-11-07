"""Server-bound ``state`` + PKCE for the OAuth redirect providers (#1344).

The IdP leg of a federated login (Renfield -> Google/GitHub/Apple -> Renfield)
carries two values a forged or replayed callback must not be able to supply:

- ``state`` — a 256-bit random value generated HERE, stored in Redis and bound
  to the initiating browser through an HttpOnly binding cookie (only its
  SHA-256 is stored). A callback whose ``state`` is unknown, already used,
  expired, issued for another provider, or presented by a browser without the
  matching cookie is refused. This closes login CSRF / session fixation: an
  attacker's own ``state`` + ``code`` replayed into the victim's browser fails
  the cookie binding.
- the PKCE ``code_verifier`` — generated HERE, never leaves the server; only
  its S256 challenge rides to the IdP. A stolen authorization ``code`` is
  useless without it.

Alongside, the record carries the SPA's own PKCE challenge + state for the
backend->SPA leg (the one-time hand-off code of ``sso_handoff_store``), so the
callback can bind the hand-off to the tab that started the login.

Consumption is an atomic ``GETDEL`` (single use, no read-then-delete race);
every failure is ``None`` — the route answers ONE opaque 400 for all of them.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass

from loguru import logger

from services.redis_client import get_redis
from services.sso_handoff_store import s256_challenge
from utils.config import settings

_KEY_PREFIX = "oauth:state:"


class OAuthStateCollision(RuntimeError):
    """A fresh state already existed (astronomically unlikely at 256 bits).
    Raised so the caller never redirects with a state that was not stored."""


@dataclass(frozen=True)
class OAuthLoginStart:
    """What the start route needs: the IdP ``state``, the IdP PKCE challenge,
    and the browser binding (cookie value) — the verifier stays in Redis."""

    state: str
    code_challenge: str
    binding: str


@dataclass(frozen=True)
class OAuthPendingLogin:
    """The consumed record: everything the callback needs after the IdP."""

    provider_id: str
    channel: str
    code_verifier: str
    spa_code_challenge: str
    spa_state: str


def _key(state: str) -> str:
    return f"{_KEY_PREFIX}{state}"


def _binding_hash(binding: str) -> str:
    return hashlib.sha256(binding.encode("utf-8")).hexdigest()


async def begin_login(
    *, provider_id: str, channel: str, spa_code_challenge: str, spa_state: str
) -> OAuthLoginStart:
    """Store a pending login and return the values the start route sends out.

    Raises :class:`OAuthStateCollision` if the NX write finds the state present.
    Redis errors propagate: without a stored state the callback can never
    succeed, so the start route must not redirect to the IdP at all.
    """
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)  # 86 chars, RFC 7636 unreserved set
    binding = secrets.token_urlsafe(32)
    payload = json.dumps({
        "provider_id": provider_id,
        "channel": channel,
        "code_verifier": verifier,
        "spa_code_challenge": spa_code_challenge,
        "spa_state": spa_state,
        "binding_hash": _binding_hash(binding),
    })
    stored = await get_redis().set(
        _key(state), payload, ex=settings.oauth_state_ttl_seconds, nx=True,
    )
    if not stored:
        raise OAuthStateCollision(state)
    return OAuthLoginStart(
        state=state, code_challenge=s256_challenge(verifier), binding=binding,
    )


async def consume_login(
    *, state: str | None, provider_id: str, binding: str | None
) -> OAuthPendingLogin | None:
    """Atomically fetch-and-delete the pending login for ``state``.

    Returns None when the state is missing, unknown, already used, expired,
    corrupt, issued for another provider, or the browser binding does not
    match. The record is burned even on a mismatch, so a raced state cannot be
    retried.
    """
    if not state:
        return None
    try:
        raw = await get_redis().getdel(_key(state))
    except Exception as e:
        logger.warning(f"oauth state: GETDEL failed: {type(e).__name__}")
        return None
    if not raw:
        return None
    try:
        d = json.loads(raw)
        pending = OAuthPendingLogin(
            provider_id=str(d["provider_id"]),
            channel=str(d["channel"]),
            code_verifier=str(d["code_verifier"]),
            spa_code_challenge=str(d["spa_code_challenge"]),
            spa_state=str(d["spa_state"]),
        )
        stored_binding_hash = str(d["binding_hash"])
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning(f"oauth state: corrupt payload discarded: {type(e).__name__}")
        return None
    if not binding or not hmac.compare_digest(
        _binding_hash(binding), stored_binding_hash
    ):
        logger.warning("oauth state: browser binding mismatch — callback refused")
        return None
    if not hmac.compare_digest(pending.provider_id, provider_id):
        logger.warning("oauth state: issued for another provider — callback refused")
        return None
    return pending
