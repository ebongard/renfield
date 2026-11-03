"""One-time download ticket for the satellite OTA package.

WHY IT EXISTS
-------------
`GET /api/satellites/update-package` is ADMIN-gated. A satellite has no user
account, and the fleet in the field (1.4.12) downloads the package with a bare
`urllib` request: no header, `package_url` fetched exactly as received in the
`update_request` (a relative path is prefixed with the server base URL). Since
the household auth-on cutover every OTA therefore died with a 401.

The satellite's credential is its own WebSocket link, which the PSK (H1)
authenticated. `initiate_update` sends the `update_request` over exactly that
link, so it mints a ticket there and puts it into `package_url` -- the only
channel a 1.4.12 device carries back unchanged.

PROPERTIES
----------
* 256-bit (`secrets.token_urlsafe(32)`), short-lived (`TICKET_TTL_SECONDS`).
* Bound to (satellite_id, version, checksum): the route serves the CURRENT
  package and refuses a ticket minted for another one.
* Single-use: redemption is an atomic Redis `GETDEL`, so two concurrent
  downloads cannot both pass, and it works across backend replicas. Any
  presentation consumes the ticket, also one whose binding then does not match --
  a ticket shown with the wrong satellite id has leaked and is worth nothing.
* Only a SHA-256 of the ticket is stored (the Redis key), never the raw value.
  The query parameter name `ticket` is in `utils.log_safety.SECRET_QUERY_PARAMS`,
  so uvicorn's access line never carries it either.

The code-authenticity guarantee stays the signed manifest (H6) the satellite
verifies; the ticket only decides who may fetch the bytes.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass

from loguru import logger

from services.redis_client import get_redis

_KEY_PREFIX = "sat_ota_ticket:"

#: Long enough for a Pi Zero to receive the request and start the download
#: (it downloads right away, once, without retry), short enough that a leaked
#: URL is soon worthless.
TICKET_TTL_SECONDS = 600

#: The query parameter that carries the ticket. Must stay covered by the
#: access-log redaction (`utils.log_safety.SECRET_QUERY_PARAMS`).
TICKET_QUERY_PARAM = "ticket"


@dataclass(frozen=True)
class OtaTicketGrant:
    """What a redeemed ticket was minted for."""
    satellite_id: str
    version: str
    checksum: str


def _key(ticket: str) -> str:
    digest = hashlib.sha256(ticket.encode("utf-8")).hexdigest()
    return f"{_KEY_PREFIX}{digest}"


async def issue_ticket(satellite_id: str, version: str, checksum: str) -> str | None:
    """Store a one-time ticket for this satellite and package; return it.

    None when Redis does not play along. The caller must then not send the
    update request: an OTA without a ticket would only end in a 401 on the
    device.
    """
    ticket = secrets.token_urlsafe(32)
    payload = json.dumps(
        {"satellite_id": satellite_id, "version": version, "checksum": checksum}
    )
    try:
        stored = await get_redis().set(
            _key(ticket), payload, ex=TICKET_TTL_SECONDS, nx=True
        )
    except Exception as e:
        logger.warning(f"satellite OTA: download ticket could not be stored: {e}")
        return None
    if not stored:
        logger.error("satellite OTA: download ticket collision — no ticket issued")
        return None
    return ticket


async def consume_ticket(
    ticket: str, satellite_id: str | None, version: str, checksum: str
) -> OtaTicketGrant | None:
    """Redeem the ticket ONCE and check its binding.

    None on an unknown, used or expired ticket, on a satellite id that does not
    match, and on a package that is no longer the one the ticket was minted for.
    Every refusal logs a WARNING without the ticket value.
    """
    if not ticket:
        return None
    try:
        raw = await get_redis().getdel(_key(ticket))
    except Exception as e:
        logger.warning(f"satellite OTA: ticket lookup failed: {e}")
        return None
    if not raw:
        logger.warning(
            f"satellite OTA: unknown, used or expired download ticket "
            f"(satellite_id={satellite_id!r})"
        )
        return None
    try:
        d = json.loads(raw)
        grant = OtaTicketGrant(
            satellite_id=str(d["satellite_id"]),
            version=str(d["version"]),
            checksum=str(d["checksum"]),
        )
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning(f"satellite OTA: unusable ticket payload discarded: {e}")
        return None

    if satellite_id is None or not hmac.compare_digest(grant.satellite_id, satellite_id):
        logger.warning(
            f"satellite OTA: download ticket for {grant.satellite_id!r} presented "
            f"as {satellite_id!r} — refused"
        )
        return None
    if grant.version != version or grant.checksum != checksum:
        logger.warning(
            f"satellite OTA: download ticket for {grant.satellite_id!r} was minted "
            f"for v{grant.version}, the package is now v{version} — refused"
        )
        return None
    return grant
