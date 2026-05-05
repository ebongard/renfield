"""JWT authentication for voice-server (D5).

Two modes, switched by `AUTH_MODE` env:

- `local` (default, for Renfield): voice-server validates HS256 tokens
  against the same `SECRET_KEY` as the backend. Same library, same
  algorithm. No backend dependency on connect.

- `callback` (Reva re-visit hook): voice-server holds NO signing keys;
  every connection POSTs the token to backend `/api/internal/auth/verify`
  and caches the result for the connection lifetime. Adds backend
  dependency to /ws/voice open. See VOICE_PIPELINE_DESIGN.md § "Auth model".

Both modes are implemented from B.1 onwards so Reva flips a config flag
instead of patching code.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from jose import JWTError, jwt

from voice_server.config import settings

logger = logging.getLogger(__name__)


class AuthError(Exception):
    """Token did not validate."""


async def authenticate(token: str) -> dict[str, Any]:
    """Validate a JWT token and return its payload.

    Raises AuthError on any failure. Caller turns this into a 401 (REST)
    or close-with-policy-violation (WS).

    When `auth_required=False` and no token is supplied, returns an
    anonymous payload — matches backend's AUTH_ENABLED=false semantics
    for single-user / cluster-internal deployments. A token IS still
    validated when present, so the same image works in both modes.
    """
    if not token:
        if not settings.auth_required:
            return {"sub": "anonymous", "scope": "anonymous"}
        raise AuthError("missing token")

    if settings.auth_mode == "local":
        return _validate_local(token)

    if settings.auth_mode == "callback":
        return await _validate_callback(token)

    raise AuthError(f"unknown auth_mode: {settings.auth_mode}")


def _validate_local(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(
            token,
            settings.secret_key.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
        )
    except JWTError as e:
        logger.debug("JWT decode failed: %s", e)
        raise AuthError(f"invalid token: {e}") from e


async def _validate_callback(token: str) -> dict[str, Any]:
    if not settings.auth_callback_url:
        raise AuthError("auth_mode=callback but auth_callback_url is empty")

    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            resp = await client.post(
                settings.auth_callback_url,
                json={"token": token},
            )
        except httpx.HTTPError as e:
            logger.warning("auth callback HTTP error: %s", e)
            raise AuthError(f"auth callback unreachable: {e}") from e

    if resp.status_code != 200:
        raise AuthError(f"auth callback rejected token: {resp.status_code}")

    payload = resp.json()
    if not isinstance(payload, dict) or "user_id" not in payload:
        raise AuthError("auth callback returned malformed payload")
    return payload
