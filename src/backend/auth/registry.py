"""Priority-ordered, multi-active auth provider registry.

The registry generalizes Renfield's pre-existing ``authenticate`` hook seam
(`api/routes/auth.py`): instead of one hook walk it owns an ordered set of
providers and a single ``post_authenticate`` emission point.

Resolution model
----------------
* **Credential providers** (db, ldap): the registry runs a PRIORITY WALK
  (ascending ``priority``, lower first) over enabled providers whose
  ``supported_channels`` include the request channel. **First non-None wins.**
* A provider that raises, or exceeds ``settings.auth_provider_timeout_seconds``,
  is **skipped (fail-open)**: WARNING log + ``auth_provider_unreachable_total
  {provider_id}`` counter, then the walk continues to the next provider.
* **Redirect providers** (google, github, apple, …) are NOT in the credential
  walk — their ``authenticate`` returns None. They are user-selected entry
  points; ``redirect_entries`` enumerates them for the login UI and their
  OAuth/OIDC callback constructs the ``ProviderResult`` directly.

The registry is identity-agnostic. It produces a ``ProviderResult`` and fires
``post_authenticate`` exactly once; the Reva consumer (or the standalone
legacy fallback in ``/auth/login``) turns that into a Renfield user id.
"""

from __future__ import annotations

import asyncio
import importlib.util

from loguru import logger

from auth.provider_contract import AuthProvider, ProviderResult
from utils.config import settings
from utils.hooks import has_hook
from utils.metrics import record_auth_provider_unreachable


class ProviderRegistry:
    """Ordered, multi-active provider set with a per-provider ``enabled`` gate."""

    def __init__(self) -> None:
        self._providers: list[AuthProvider] = []

    # -- registration --------------------------------------------------------

    def register(self, provider: AuthProvider) -> None:
        """Add a provider. Duplicate ``provider_id`` replaces the prior one
        (idempotent re-bootstrap; last registration wins)."""
        self._providers = [
            p for p in self._providers if p.provider_id != provider.provider_id
        ]
        self._providers.append(provider)
        logger.debug(
            f"Auth provider registered: {provider.provider_id} "
            f"(priority={provider.priority}, enabled={provider.enabled})"
        )

    def clear(self) -> None:
        """Drop all providers (test isolation / re-bootstrap)."""
        self._providers = []

    # -- introspection -------------------------------------------------------

    def all(self) -> list[AuthProvider]:
        """All registered providers, ascending ``priority``."""
        return sorted(self._providers, key=lambda p: p.priority)

    def get(self, provider_id: str) -> AuthProvider | None:
        return next(
            (p for p in self._providers if p.provider_id == provider_id), None
        )

    def credential_providers(self, channel: str) -> list[AuthProvider]:
        """Enabled providers that participate in the credential walk for
        *channel*, in priority order."""
        return [
            p
            for p in self.all()
            if p.enabled and channel in p.supported_channels
        ]

    def redirect_entries(self, channel: str) -> list[tuple[str, str]]:
        """``(provider_id, authorize_url)`` for every enabled redirect provider
        that exposes an ``authorize_url(channel)`` entry point for *channel*.

        Credential-only providers (no ``authorize_url``) are excluded, so this
        is the list the login UI renders as "Sign in with …" buttons.
        """
        entries: list[tuple[str, str]] = []
        for p in self.all():
            if not p.enabled or channel not in p.supported_channels:
                continue
            authorize_url = getattr(p, "authorize_url", None)
            if authorize_url is None:
                continue
            url = authorize_url(channel)
            if url:
                entries.append((p.provider_id, url))
        return entries

    # -- credential walk -----------------------------------------------------

    async def authenticate(
        self, *, username: str, password: str, channel: str
    ) -> ProviderResult | None:
        """Run the priority walk. Return the first provider's non-None
        ``ProviderResult``, or None if every provider declined.

        Fail-open: a provider that raises or times out is skipped (logged +
        counted) and the walk continues. This never raises.
        """
        timeout = settings.auth_provider_timeout_seconds
        for provider in self.credential_providers(channel):
            try:
                result = await asyncio.wait_for(
                    provider.authenticate(
                        username=username, password=password, channel=channel
                    ),
                    timeout=timeout,
                )
            except TimeoutError:
                logger.warning(
                    f"Auth provider {provider.provider_id!r} timed out after "
                    f"{timeout}s — skipped (fail-open), continuing walk"
                )
                record_auth_provider_unreachable(provider.provider_id)
                continue
            except Exception:
                logger.opt(exception=True).warning(
                    f"Auth provider {provider.provider_id!r} errored — "
                    f"skipped (fail-open), continuing walk"
                )
                record_auth_provider_unreachable(provider.provider_id)
                continue

            if result is not None:
                logger.debug(
                    f"Auth provider {provider.provider_id!r} resolved "
                    f"credential walk (channel={channel})"
                )
                return result
        return None


# ---------------------------------------------------------------------------
# Module-level default registry
# ---------------------------------------------------------------------------

_registry: ProviderRegistry | None = None


def build_default_registry() -> ProviderRegistry:
    """Construct the registry from config and install it as the singleton.

    All built-ins are registered; the per-provider ``enabled`` gate (driven by
    config) decides participation. Social providers ship ``enabled=False`` —
    flipping them on is a config-only change, no redeploy.
    """
    # Imported here to avoid a circular import at module load
    # (providers import the contract; the package __init__ imports the
    # registry).
    from auth.providers.apple import AppleProvider
    from auth.providers.db import DBProvider
    from auth.providers.github import GitHubProvider
    from auth.providers.google import GoogleProvider
    from auth.providers.ldap import LDAPProvider

    registry = ProviderRegistry()
    registry.register(DBProvider())
    registry.register(LDAPProvider())
    registry.register(GoogleProvider())
    registry.register(GitHubProvider())
    registry.register(AppleProvider())

    global _registry
    _registry = registry
    logger.info(
        "Auth provider registry built: "
        + ", ".join(
            f"{p.provider_id}({'on' if p.enabled else 'off'})"
            for p in registry.all()
        )
    )
    return registry


def _ldap3_available() -> bool:
    return importlib.util.find_spec("ldap3") is not None


def assert_login_possible() -> None:
    """Raise ``RuntimeError`` when no login can ever succeed (#1345).

    With ``DB_AUTH_ENABLED=false`` the only built-in credential provider left
    is LDAP, and an LDAP result is a directory subject, not a Renfield user:
    standalone ``resolve_login`` denies it unless a ``post_authenticate``
    consumer resolves it (or a legacy ``authenticate`` hook answers first).
    Without either, every login is a 401 — fail the boot instead. Called by the
    lifespan AFTER the plugins have registered their hooks; the LDAP half is
    the config validator's (``Settings.assert_credential_provider_config``).
    """
    if settings.db_auth_enabled:
        return
    # The LDAP provider declines every login when ldap3 is missing (logged,
    # not raised) — with db off that is a total lockout, so fail the boot.
    if not _ldap3_available():
        raise RuntimeError(
            "DB_AUTH_ENABLED=false but the ldap3 package is not installed — the "
            "LDAP provider would decline every login, so nobody could log in. "
            "Install ldap3 in the image or set DB_AUTH_ENABLED=true."
        )
    if has_hook("post_authenticate") or has_hook("authenticate"):
        return
    raise RuntimeError(
        "DB_AUTH_ENABLED=false but no post_authenticate consumer (nor a legacy "
        "authenticate hook) is registered — an LDAP login cannot be resolved to "
        "a Renfield user, so nobody could log in. Load the identity plugin "
        "(PLUGIN_MODULES) or set DB_AUTH_ENABLED=true."
    )


def get_registry() -> ProviderRegistry:
    """Return the process-wide registry, building the default lazily."""
    if _registry is None:
        return build_default_registry()
    return _registry


def set_registry(registry: ProviderRegistry | None) -> None:
    """Override / reset the singleton (tests)."""
    global _registry
    _registry = registry
