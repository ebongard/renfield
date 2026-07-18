"""JWT authentication for voice-server (D5, extended with registry mode).

Three modes, switched by `AUTH_MODE` env:

- `local` (default, single-tenant Renfield): voice-server validates HS256
  tokens against the same `SECRET_KEY` as the backend. Same library, same
  algorithm. No backend dependency on connect.

- `callback` (single foreign client, e.g. a standalone Reva deploy):
  voice-server holds NO signing keys; every connection POSTs the token to
  backend `/api/internal/auth/verify` and caches the result for the
  connection lifetime. See VOICE_PIPELINE_DESIGN.md § "Auth model".

- `registry` (shared multi-client instance): `AUTH_CLIENTS` maps a
  client-id to EITHER that client's own verify URL (the callback pattern,
  per client) OR `anonymous: true` (honored only via the dedicated
  `anon_port` listener, which the deployment fences with a NetworkPolicy).
  Callers identify via `X-Voice-Client` (REST) / `?client=` (WS). The
  verify response contract is pinned: it MUST be a JSON object with a
  `user_id` key. Returned payloads always carry `client_id`, and identity
  is namespaced (client_id, user_id) — never bare user_id — because
  user 5 of one product is not user 5 of another.

`local` and `callback` are unchanged for existing deployments.
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


async def authenticate(
    token: str,
    client_id: str | None = None,
    *,
    via_anon_port: bool = False,
) -> dict[str, Any]:
    """Validate a request's credentials and return an identity payload.

    Raises AuthError on any failure. Caller turns this into a 401 (REST)
    or close-with-policy-violation (WS).

    `client_id` and `via_anon_port` only matter in `registry` mode;
    `local`/`callback` ignore them so existing call sites keep working.

    When `auth_required=False` (local/callback modes only) and no token is
    supplied, returns an anonymous payload — matches backend's
    AUTH_ENABLED=false semantics for single-user / cluster-internal
    deployments. A token IS still validated when present, so the same
    image works in both modes.
    """
    if settings.auth_mode == "registry":
        return await _validate_registry(token, client_id, via_anon_port)

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


async def _post_verify(url: str, token: str) -> tuple[int, Any]:
    """POST a token to a verify endpoint. Split out so tests can stub the
    network without an HTTP server. Returns (status_code, parsed_json);
    raises AuthError on transport failure."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            resp = await client.post(url, json={"token": token})
        except httpx.HTTPError as e:
            logger.warning("auth callback HTTP error: %s", e)
            # Deliberately generic: the exception string embeds the verify
            # URL, and AuthError text reaches callers via 401 detail / WS
            # close reason — don't leak internal endpoints.
            raise AuthError("auth callback unreachable") from e
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    return resp.status_code, payload


async def _validate_callback(token: str) -> dict[str, Any]:
    if not settings.auth_callback_url:
        raise AuthError("auth_mode=callback but auth_callback_url is empty")
    return await _verify_via(settings.auth_callback_url, token)


async def _verify_via(url: str, token: str) -> dict[str, Any]:
    status_code, payload = await _post_verify(url, token)
    if status_code != 200:
        raise AuthError(f"auth callback rejected token: {status_code}")
    # Pinned contract: a verify endpoint returns a JSON object with user_id.
    # No sub-or-user_id or-chaining — a client whose endpoint returns a
    # different shape is misconfigured, not "almost right".
    if not isinstance(payload, dict) or "user_id" not in payload:
        raise AuthError("auth callback returned malformed payload")
    return payload


async def _validate_registry(
    token: str, client_id: str | None, via_anon_port: bool
) -> dict[str, Any]:
    if not client_id:
        raise AuthError(
            "registry auth requires a client id "
            "(X-Voice-Client header / ?client= query param)"
        )
    row = settings.auth_clients.get(client_id)
    if row is None:
        # Do not echo the offered id in detail beyond logging — the 401 body
        # stays uniform so the endpoint can't be used to enumerate clients.
        logger.warning("registry auth: unknown client id %r", client_id)
        raise AuthError("unknown client")

    if row.anonymous:
        # Anonymous rows exist for the household deployment (no JWT logins).
        # They are honored ONLY via the dedicated anon listener, which the
        # deployment restricts by NetworkPolicy. On the primary (ingress-
        # reachable) port an anonymous row is as good as absent: an external
        # caller claiming the household's client id gets rejected.
        if not via_anon_port:
            logger.warning(
                "registry auth: anonymous client %r attempted on primary port",
                client_id,
            )
            raise AuthError("unknown client")
        return {
            "user_id": "anonymous",
            "sub": "anonymous",
            "scope": "anonymous",
            "client_id": client_id,
        }

    if not token:
        raise AuthError("missing token")
    payload = await _verify_via(row.verify_url, token)  # type: ignore[arg-type]
    payload["client_id"] = client_id
    return payload
