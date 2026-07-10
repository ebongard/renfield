"""
REST API Rate Limiter for Renfield

Provides global rate limiting for REST API endpoints using slowapi.
Configurable per-endpoint limits via settings.
"""

import ipaddress
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

from utils.config import settings

logger = logging.getLogger(__name__)

_trusted_networks: list | None = None


def _get_trusted_networks():
    global _trusted_networks
    if _trusted_networks is None:
        _trusted_networks = []
        for entry in settings.trusted_proxies.split(","):
            entry = entry.strip()
            if entry:
                try:
                    _trusted_networks.append(ipaddress.ip_network(entry, strict=False))
                except ValueError:
                    pass
    return _trusted_networks


def _is_trusted_proxy(ip: str) -> bool:
    networks = _get_trusted_networks()
    if not networks:
        return True  # No trusted_proxies configured = trust all (backwards compatible)
    try:
        addr = ipaddress.ip_address(ip)
        return any(addr in net for net in networks)
    except ValueError:
        return False


def get_client_ip(request: Request) -> str:
    """
    Resolve the client IP used as the rate-limit key.

    When ``TRUSTED_PROXIES`` is CONFIGURED, resolution is spoof-resistant:
    ``X-Forwarded-For`` is client-controllable, so we only trust hops we put
    there ourselves. The direct peer must itself be a trusted proxy, and we walk
    the XFF chain RIGHT-to-LEFT returning the first address that is NOT a trusted
    proxy — the right-most-untrusted address, the genuine client as seen from our
    trust boundary. An attacker can prepend arbitrary left-most entries but
    cannot inject an untrusted address to the right of our own proxy hop.

    When ``TRUSTED_PROXIES`` is EMPTY (the default) we keep the legacy,
    backwards-compatible behavior: read ``X-Forwarded-For[0]`` / ``X-Real-IP``,
    trusting all proxies. This preserves per-client keying behind a reverse
    proxy (Traefik/nginx) out of the box — flipping to "use the direct socket
    IP" would collapse every client into the proxy's single IP bucket and turn
    the shared per-IP limit into a cluster-wide DoS. The trade-off is that the
    empty default is spoofable (a client can forge ``X-Forwarded-For[0]``); set
    ``TRUSTED_PROXIES`` to the proxy's network to get the spoof-resistant
    right-most-untrusted walk. See docs/SECURITY.md.
    """
    direct_ip = get_remote_address(request)

    networks = _get_trusted_networks()
    if not networks:
        # Legacy path: no trusted proxies configured → trust all, take the
        # left-most forwarded entry (backwards-compatible per-client keying).
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            first = forwarded_for.split(",")[0].strip()
            if first:
                return first
        real_ip = request.headers.get("X-Real-IP")
        if real_ip and real_ip.strip():
            return real_ip.strip()
        return direct_ip

    # Configured path: the direct peer must itself be a trusted proxy; otherwise
    # the request did not come through our proxy and its forwarded headers are
    # untrustworthy → key on the direct socket IP.
    if not _is_trusted_proxy(direct_ip):
        return direct_ip

    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        # Right-to-left: skip our own trusted proxy hops, return the first
        # untrusted address (the real client from our boundary's perspective).
        for hop in reversed([h.strip() for h in forwarded_for.split(",") if h.strip()]):
            if not _is_trusted_proxy(hop):
                return hop
        # Whole chain is trusted proxies (unusual) → fall through to direct_ip.

    real_ip = request.headers.get("X-Real-IP")
    if real_ip and not _is_trusted_proxy(real_ip.strip()):
        return real_ip.strip()

    return direct_ip


# Create limiter instance with custom key function. storage_uri is env-driven
# (#693): "memory://" per-pod by default, Redis for shared per-cluster limiting.
limiter = Limiter(
    key_func=get_client_ip,
    default_limits=[settings.api_rate_limit_default] if settings.api_rate_limit_enabled else [],
    enabled=settings.api_rate_limit_enabled,
    storage_uri=settings.api_rate_limit_storage_uri,
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
    'get_client_ip',
    'limit_admin',
    'limit_auth',
    'limit_chat',
    'limit_custom',
    'limit_default',
    'limit_voice',
    'limiter',
    'setup_rate_limiter',
]
