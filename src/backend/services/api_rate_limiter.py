"""
REST API Rate Limiter for Renfield

Provides global rate limiting for REST API endpoints using slowapi.
Configurable per-endpoint limits via settings.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from typing import Optional
import logging

from utils.config import settings

logger = logging.getLogger(__name__)


def get_client_ip(request: Request) -> str:
    """
    Get client IP address from request.
    Handles X-Forwarded-For header for reverse proxy setups.
    """
    # Check for X-Forwarded-For header (nginx, load balancer)
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        # Take the first IP (original client)
        return forwarded_for.split(",")[0].strip()

    # Check for X-Real-IP header
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip

    # Fall back to direct client IP
    return get_remote_address(request)


# Create limiter instance with custom key function
limiter = Limiter(
    key_func=get_client_ip,
    default_limits=[settings.api_rate_limit_default] if settings.api_rate_limit_enabled else [],
    enabled=settings.api_rate_limit_enabled,
    storage_uri="memory://",  # In-memory storage (use Redis for production clusters)
)


def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """
    Custom handler for rate limit exceeded errors.
    Returns a JSON response with retry information.
    """
    # Extract retry-after from the exception
    retry_after = getattr(exc, 'retry_after', 60)

    logger.warning(
        f"Rate limit exceeded for {get_client_ip(request)} on {request.url.path}"
    )

    return JSONResponse(
        status_code=429,
        content={
            "error": "rate_limit_exceeded",
            "message": f"Rate limit exceeded. Please try again in {retry_after} seconds.",
            "retry_after": retry_after,
            "detail": str(exc.detail) if hasattr(exc, 'detail') else None
        },
        headers={
            "Retry-After": str(retry_after),
            "X-RateLimit-Limit": str(exc.detail) if hasattr(exc, 'detail') else "unknown"
        }
    )


def setup_rate_limiter(app: FastAPI) -> None:
    """
    Setup rate limiter for FastAPI application.

    Args:
        app: FastAPI application instance
    """
    if not settings.api_rate_limit_enabled:
        logger.info("API rate limiting is disabled")
        return

    # Add limiter to app state
    app.state.limiter = limiter

    # Add middleware
    app.add_middleware(SlowAPIMiddleware)

    # Add exception handler
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

    logger.info(
        f"API rate limiting enabled with default limit: {settings.api_rate_limit_default}"
    )


# Pre-configured rate limit decorators for common use cases
def limit_default(func):
    """Apply default rate limit"""
    return limiter.limit(settings.api_rate_limit_default)(func)


def limit_auth(func):
    """Apply stricter rate limit for authentication endpoints"""
    return limiter.limit(settings.api_rate_limit_auth)(func)


def limit_voice(func):
    """Apply rate limit for voice endpoints (STT, TTS)"""
    return limiter.limit(settings.api_rate_limit_voice)(func)


def limit_chat(func):
    """Apply rate limit for chat endpoints"""
    return limiter.limit(settings.api_rate_limit_chat)(func)


def limit_admin(func):
    """Apply higher rate limit for admin endpoints"""
    return limiter.limit(settings.api_rate_limit_admin)(func)


def limit_custom(limit_string: str):
    """
    Apply custom rate limit.

    Args:
        limit_string: Rate limit string (e.g., "10/minute", "100/hour")

    Example:
        @limit_custom("5/minute")
        async def sensitive_endpoint():
            ...
    """
    return limiter.limit(limit_string)


# Export limiter instance for direct use
__all__ = [
    'limiter',
    'setup_rate_limiter',
    'limit_default',
    'limit_auth',
    'limit_voice',
    'limit_chat',
    'limit_admin',
    'limit_custom',
    'get_client_ip',
]
