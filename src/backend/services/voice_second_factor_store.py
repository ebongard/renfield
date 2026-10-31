"""The intermediate ticket between password and voice.

WHY IT EXISTS
-------------
Until 2026-09-27 `POST /auth/voice` was the FIRST factor: audio recording in,
access and refresh token out. One recording of the voice was therefore enough for
a complete login, with no liveness check and no second factor. The decision of
2026-09-27 was to turn it into an ADDITIONAL factor.

An additional factor only is one if it comes after a first one and is worth
nothing without it. So the voice check needs a proof that "the password was
shown". This module holds it: for an account carrying the flag, `/auth/login`
stores a one-time, short-lived ticket and issues NO tokens; `/auth/voice` redeems
the ticket and only then issues tokens.

🛑 WHAT THIS SETTLES ALONG THE WAY
---------------------------------
Because the user is KNOWN after the first factor, the route turns from "who is
this?" into "is this the claimed person?" -- from 1:N to 1:1. Two findings of the
2026-09-27 review thereby disappear structurally instead of behind a guard:

* The **name oracle**: there is nothing left to identify, hence no name to return
  either. The caller knows anyway who they claim to be.
* The **coin toss**: `verify_speaker` checks against the embeddings of ONE
  profile. The runner-up margin that 1:N needed (and whose absence separated the
  login from the recognition) has nothing left to apply to here.

FORM
----
Deliberately built like `sso_handoff_store`: a 256-bit token as the key, the
payload in Redis, redemption as an atomic `GETDEL` (so one-time, even on two
concurrent attempts), short expiry. **NO token lies in Redis** -- only the
resolved identity. `/auth/voice` mints the JWTs fresh and re-checks the user
while doing so, so an account deactivated in the meantime does not get through
and `must_change_password` is current.

🛑 The ticket is bound to the REQUEST ADDRESS when that address is resistant to
forgery. Without the binding an attacker who intercepts a ticket could redeem it
from anywhere; with it they also have to hold the address. If the address is not
resistant to forgery (no `TRUSTED_PROXIES`), NO binding happens rather than a
wrong one -- binding to a forgeable value would be a gain for the attacker, not
for us.
"""
from __future__ import annotations

import hmac
import json
import secrets
from dataclasses import dataclass

from loguru import logger

from services.redis_client import get_redis
from utils.config import settings

_KEY_PREFIX = "voice2fa:"

#: How long the ticket is valid. Short: it only bridges the seconds between
#: entering the password and recording the voice. Too long would be a window in
#: which an intercepted ticket is still worth something.
DEFAULT_TTL_SECONDS = 180


@dataclass(frozen=True)
class PendingVoiceFactor:
    """The proof that "the first factor passed" -- NOT the tokens.

    ``client_ip`` is None when the address is not resistant to forgery; no
    address binding takes place then (see the module header).
    """
    user_id: int
    client_ip: str | None


def _key(ticket: str) -> str:
    return f"{_KEY_PREFIX}{ticket}"


def _ttl() -> int:
    return int(getattr(settings, "voice_second_factor_ttl_seconds", DEFAULT_TTL_SECONDS))


async def issue_ticket(user_id: int, client_ip: str | None) -> str | None:
    """Store a one-time ticket and return it. None when Redis does not play along.

    🛑 Fail-closed: without a ticket `/auth/login` issues no tokens and the user
    does not get in. That is the right direction -- a Redis outage must not lead
    to the second factor quietly falling away.
    """
    ticket = secrets.token_urlsafe(32)  # 256 Bit
    payload = json.dumps({"user_id": int(user_id), "client_ip": client_ip})
    try:
        stored = await get_redis().set(_key(ticket), payload, ex=_ttl(), nx=True)
    except Exception as e:
        logger.warning(f"voice 2fa: ticket could not be stored: {e}")
        return None
    if not stored:
        # A collision on a 256-bit token means something is fundamentally
        # wrong. No retry, no quiet continuation.
        logger.error("voice 2fa: ticket collision — no ticket issued")
        return None
    return ticket


async def consume_ticket(ticket: str, client_ip: str | None) -> PendingVoiceFactor | None:
    """Redeem the ticket ONCE (atomic `GETDEL`).

    None on an unknown, already used or expired ticket -- and also when the
    address binding does not match. The caller must not report these cases
    distinguishably to the outside.
    """
    if not ticket:
        return None
    try:
        raw = await get_redis().getdel(_key(ticket))
    except Exception as e:
        logger.warning(f"voice 2fa: GETDEL failed: {e}")
        return None
    if not raw:
        return None
    try:
        d = json.loads(raw)
        pending = PendingVoiceFactor(
            user_id=int(d["user_id"]),
            client_ip=d.get("client_ip"),
        )
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
        logger.warning(f"voice 2fa: unusable payload discarded: {e}")
        return None

    # Address binding, but only if one was bound when issuing. Comparison in
    # constant time -- the address is not a secret, but the comparison is free
    # and the habit is the right one.
    if pending.client_ip is not None:
        if client_ip is None or not hmac.compare_digest(pending.client_ip, client_ip):
            logger.warning(
                "voice 2fa: ticket redeemed from a different address — discarded"
            )
            return None
    return pending
