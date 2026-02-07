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
    # Skip authentication if disabled
    if not settings.ws_auth_enabled:
        return {"authenticated": True, "auth_skipped": True}

    # Fallback: read token from Authorization header if not provided via query
    if not token:
        auth_header = websocket.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]

    if not token:
        return None

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
