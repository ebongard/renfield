"""
WebSocket Authentication Service for Renfield

Provides token-based authentication for WebSocket connections.
Supports both query parameter and first-message authentication.
"""

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Query, WebSocket
from loguru import logger

from utils.config import settings


class WSTokenStore:
    """
    In-memory token store for WebSocket authentication.

    For production with multiple instances, replace with Redis-based storage.
    """

    def __init__(self):
        self._tokens: dict[str, dict[str, Any]] = {}

    def create_token(
        self,
        device_id: str | None = None,
        device_type: str | None = None,
        user_id: str | None = None,
        expires_minutes: int = None
    ) -> str:
        """
        Create a new WebSocket authentication token.

        Args:
            device_id: Optional device identifier
            device_type: Optional device type (satellite, web_panel, etc.)
            user_id: Optional user identifier
            expires_minutes: Token expiration in minutes (default from settings)

        Returns:
            Token string
        """
        if expires_minutes is None:
            expires_minutes = settings.ws_token_expire_minutes

        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=expires_minutes)

        self._tokens[token] = {
            "device_id": device_id,
            "device_type": device_type,
            "user_id": user_id,
            "created_at": datetime.now(UTC).replace(tzinfo=None),
            "expires_at": expires_at,
        }

        logger.debug(f"Created WS token for device={device_id}, expires={expires_at}")
        return token

    def validate_token(self, token: str) -> dict[str, Any] | None:
        """
        Validate a WebSocket token.

        Args:
            token: Token string to validate

        Returns:
            Token data dict if valid, None otherwise
        """
        if not token:
            return None

        token_data = self._tokens.get(token)
        if not token_data:
            return None

        # Check expiration
        if datetime.now(UTC).replace(tzinfo=None) > token_data["expires_at"]:
            del self._tokens[token]
            return None

        return token_data

    def revoke_token(self, token: str) -> bool:
        """Revoke a token."""
        if token in self._tokens:
            del self._tokens[token]
            return True
        return False

    def cleanup_expired(self):
        """Remove expired tokens."""
        now = datetime.now(UTC).replace(tzinfo=None)
        expired = [t for t, data in self._tokens.items() if now > data["expires_at"]]
        for token in expired:
            del self._tokens[token]
        if expired:
            logger.debug(f"Cleaned up {len(expired)} expired WS tokens")


# Global token store singleton
_token_store: WSTokenStore | None = None


def get_token_store() -> WSTokenStore:
    """Get or create the global token store."""
    global _token_store
    if _token_store is None:
        _token_store = WSTokenStore()
    return _token_store


def _ws_origin_allowed(websocket: WebSocket) -> bool:
    """CSWSH origin allowlist for the WS handshake (#1116).

    - ``cors_origins == "*"`` → allow all (dev / permissive).
    - no ``Origin`` header → allow (non-browser client; browsers always send it).
    - else the ``Origin`` must be one of the comma-separated ``cors_origins``.

    Mirrors the CORS-middleware allowlist in main.py, so any origin the SPA is
    already served from (and passes CORS with) also passes the WS check.
    """
    configured = settings.cors_origins.strip()
    if configured == "*":
        return True
    origin = websocket.headers.get("origin")
    if not origin:
        return True
    allowed = {o.strip() for o in configured.split(",") if o.strip()}
    return origin in allowed


async def authenticate_websocket(
    websocket: WebSocket,
    token: str | None = None
) -> dict[str, Any] | None:
    """
    Authenticate a WebSocket connection.

    Args:
        websocket: WebSocket connection
        token: Optional token (from query param or first message)

    Returns:
        Token data if authenticated, None otherwise
    """
    # CSWSH protection (#1116): a browser ALWAYS sends an Origin header on the
    # WS handshake, so reject a browser Origin that is not in the CORS allowlist
    # (cross-site WebSocket hijacking). A MISSING Origin = a non-browser client
    # (satellite/device/server-to-server) → allowed (an attacker's browser can't
    # omit Origin). cors_origins="*" (dev/permissive, e.g. the auth-off
    # household) skips the check. Applied before the ws_auth_enabled short-
    # circuit so it protects even ws-auth-off browser sockets.
    if not _ws_origin_allowed(websocket):
        logger.warning(
            "WS handshake rejected: disallowed Origin {!r}",
            websocket.headers.get("origin"),
        )
        return None

    # Skip authentication if disabled
    if not settings.ws_auth_enabled:
        return {"authenticated": True, "auth_skipped": True}

    # Strategy 0: the HttpOnly access cookie (JWT cookie migration). A browser
    # auto-attaches it on the same-origin WS handshake, so browsers need no
    # ?token= in the URL (no long-lived JWT in proxy logs). Safe because the
    # CSWSH Origin allowlist above already ran and only a pinned-origin deploy
    # enables cookies. Only when auth_cookie_enabled and no explicit query /
    # first-message token was supplied.
    if not token and settings.auth_cookie_enabled:
        token = websocket.cookies.get(settings.auth_cookie_name)

    # Fallback: read token from Authorization header if not provided via query
    if not token:
        auth_header = websocket.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]

    if not token:
        return None

    # Strategy 1: Try JWT validation (web chat users authenticated via
    # /api/auth/login). The React frontend reads `renfield_access_token`
    # from localStorage and appends it directly to the WS URL as
    # `?token=<JWT>`. We mirror the User-resolution pattern from
    # `auth_service.get_current_user`: decode → cast sub → look up the
    # User → verify existence + active status → return the INT user.id
    # from the DB column so downstream code never has to defensively
    # cast a string.
    #
    # Why the DB lookup matters:
    # - JWT `sub` claim is a string per the JWT spec (RFC 7519 §4.1.2).
    #   Without the lookup we'd return that string directly and
    #   downstream queries that expect int user_id crash asyncpg with
    #   `DataError: str object cannot be interpreted as an integer`.
    # - A revoked / deleted / disabled user whose JWT hasn't expired yet
    #   must NOT be able to reconnect. The User row is the source of
    #   truth for "can this person still use the system".
    # - If the JWT is structurally valid but the User is gone, we return
    #   None (reject) — we do NOT fall through to the device-token path,
    #   because an attacker presenting a former user's JWT shouldn't get
    #   a second chance at matching a satellite token.
    payload = None
    try:
        from services.auth_service import decode_token

        payload = decode_token(token)
    except Exception as e:  # noqa: BLE001 — decode_token shouldn't raise, but defend
        logger.debug(f"WebSocket JWT decode raised unexpectedly: {e}")

    # A "voice"-scoped faucet token is ONLY for the external voice-server's
    # /ws/voice handshake — reject it on renfield's own /ws/* so a harvested voice
    # token can't open a chat/kiosk socket (scope hygiene; the "ws" scope stays
    # valid here, and a legacy no-scope access token is still accepted).
    if payload and payload.get("scope") == "voice":
        logger.debug("WebSocket JWT rejected: voice-scoped token not valid on this socket")
        return None

    if payload and payload.get("type") == "access":
        sub = payload.get("sub")
        try:
            user_id_int = int(sub)
        except (TypeError, ValueError):
            logger.debug(f"WebSocket JWT 'sub' is not an int-like string: {sub!r}")
            return None

        try:
            from services.auth_service import get_user_by_id
            from services.database import AsyncSessionLocal

            async with AsyncSessionLocal() as db:
                user = await get_user_by_id(db, user_id_int)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"WebSocket JWT user lookup failed: {e}")
            return None

        if user is None:
            logger.debug(f"WebSocket JWT auth: user_id={user_id_int} not found")
            return None
        if not user.is_active:
            logger.debug(f"WebSocket JWT auth: user_id={user_id_int} disabled")
            return None
        # Forced password rotation (#694) also gates the WS surface — otherwise a
        # flagged user could open /ws/chat and drive the full agent without ever
        # rotating, bypassing the HTTP get_current_user gate. Rejecting the WS
        # connection forces them through the HTTP /change-password flow first.
        if user.must_change_password:
            logger.warning(
                f"WebSocket JWT auth: user_id={user_id_int} must change password "
                "— rejecting connection until rotated"
            )
            return None
        # Revoked (logged-out) token must not open a WS either (audit review):
        # logout blacklists the jti, and get_current_user rejects it on REST — the
        # WS surface must match, else a "logged-out" session keeps a live socket.
        # is_blacklisted fails CLOSED (rejects on Redis outage), same as REST.
        from services.token_blacklist import token_blacklist as _blacklist
        _jti = payload.get("jti")
        if _jti and await _blacklist.is_blacklisted(_jti):
            logger.debug(f"WebSocket JWT auth: user_id={user_id_int} token revoked — rejecting")
            return None
        # Session-revocation epoch (security audit H3/H4): a token minted before
        # the user's token_epoch was bumped (password change) can't (re)connect.
        if int(payload.get("epoch", 0) or 0) < int(getattr(user, "token_epoch", 0) or 0):
            logger.debug(f"WebSocket JWT auth: user_id={user_id_int} epoch stale — rejecting")
            return None

        logger.debug(f"WebSocket authenticated via JWT: user_id={user.id}")
        return {
            "authenticated": True,
            "user_id": user.id,  # int from the DB column, not the JWT string
            "auth_method": "jwt",
        }

    # Strategy 2: Device token (satellites, devices) — the original
    # flow. Used by hardware satellites that fetch a short-lived token
    # via POST /api/ws/token at boot time.
    store = get_token_store()
    token_data = store.validate_token(token)

    if token_data:
        logger.debug(f"WebSocket authenticated: device={token_data.get('device_id')}")
        return token_data

    return None


async def require_ws_auth(
    websocket: WebSocket,
    token: str = Query(None, description="WebSocket authentication token")
) -> dict[str, Any] | None:
    """
    FastAPI dependency for WebSocket authentication via query parameter.

    Usage:
        @app.websocket("/ws")
        async def websocket_endpoint(
            websocket: WebSocket,
            auth: dict = Depends(require_ws_auth)
        ):
            if not auth:
                await websocket.close(code=4001, reason="Unauthorized")
                return
            ...
    """
    return await authenticate_websocket(websocket, token)


class WSAuthError:
    """WebSocket authentication error codes."""
    UNAUTHORIZED = 4001
    TOKEN_EXPIRED = 4002
    TOKEN_INVALID = 4003
    AUTH_REQUIRED = 4004


async def close_unauthorized(websocket: WebSocket, code: int = WSAuthError.UNAUTHORIZED, reason: str = "Unauthorized"):
    """Close WebSocket with authentication error."""
    try:
        await websocket.close(code=code, reason=reason)
    except Exception:
        pass
