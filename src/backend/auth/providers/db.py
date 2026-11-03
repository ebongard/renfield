"""DB credential provider — wraps Renfield's existing bcrypt auth.

This is the lowest-precedence credential provider (priority 100): the
registry walks higher-precedence providers (e.g. LDAP at 50) first, then falls
back to local bcrypt — exactly the pre-registry
``authenticate``-hook-then-``authenticate_user`` order.

On by default. ``DB_AUTH_ENABLED=false`` takes it out of the walk, which makes
LDAP the credential authority: a stale local password no longer logs in (#1345).

``subject`` is the Renfield user id as a string. In a standalone Renfield
(no ``post_authenticate`` consumer) ``/auth/login`` uses this subject directly
as the JWT ``sub`` — that is the legacy-fallback path.
"""

from __future__ import annotations

from auth.provider_contract import ProviderResult
from auth.providers.base import make_result
from services.auth_service import authenticate_user
from services.database import AsyncSessionLocal
from utils.config import settings


def _display_name(user) -> str:
    parts = [p for p in (user.first_name, user.last_name) if p]
    return " ".join(parts) if parts else user.username


class DBProvider:
    """Local username/password against the ``users`` table (bcrypt)."""

    provider_id = "db"
    supported_channels = frozenset({"web"})
    priority = 100

    @property
    def enabled(self) -> bool:
        # Config-driven like LDAP; the registry walk skips a disabled provider.
        return settings.db_auth_enabled

    async def authenticate(
        self, *, username: str, password: str, channel: str
    ) -> ProviderResult | None:
        # Second gate for a caller that bypasses the registry's enabled filter.
        if not settings.db_auth_enabled:
            return None
        # Own short-lived session: the contract signature carries no db
        # handle, and the credential walk must not borrow the request session
        # (a failed bcrypt check should not touch request-scoped tx state).
        async with AsyncSessionLocal() as session:
            user = await authenticate_user(session, username, password)
            if user is None:
                return None
            return make_result(
                provider_id=self.provider_id,
                subject=str(user.id),
                display_name=_display_name(user),
                channel=channel,
                email=user.email,
                # Local accounts are not email-verified by Renfield; the
                # cross-provider email join must not link on this.
                email_verified=False,
            )
