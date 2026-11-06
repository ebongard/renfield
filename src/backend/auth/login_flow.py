"""`/auth/login` resolution: registry walk + the single ``post_authenticate``.

This generalizes the pre-registry two-step (``authenticate`` hook → bcrypt
fallback) WITHOUT breaking it:

  1. **Legacy ``authenticate`` hook** (unchanged): if a plugin's hook returns
     a Renfield ``User``, that is authoritative — exactly today's behavior,
     bit-for-bit. (No ``post_authenticate`` is fired here: the legacy hook has
     already resolved identity to a concrete User row. This is the
     backward-compat seam acceptance criterion #6 protects.)
  2. Else the **provider registry credential walk** runs. Its built-in
     ``DBProvider`` replaces the old inline bcrypt fallback; ``LDAPProvider``
     and any plugin-registered providers participate by priority.
  3. On a registry ``ProviderResult``, ``post_authenticate`` is fired **exactly
     once, before any JWT is minted**:
       * a consumer is registered and resolves → its renfield user id is used;
       * a consumer is registered but none resolve → **deny** (return None);
       * **no** consumer registered (standalone Renfield) → legacy fallback:
         the ``db`` provider's subject IS the renfield user id; any other
         provider cannot be resolved without an identity layer → deny.

Returns a :class:`LoginOutcome` (the route loads the ``User`` by id to update
``last_login`` and mint tokens) or ``None`` (→ 401; the route does not
distinguish "bad credentials" from "unresolved" — both are 401).
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from auth.provider_contract import ProviderResult
from auth.registry import get_registry
from utils.hooks import has_hook, run_hooks


@dataclass(frozen=True)
class LoginOutcome:
    """What `/auth/login` needs to mint a token. ``provider_id`` is for audit
    logging only (``legacy_hook`` for the backward-compat path)."""

    user_id: int
    display_name: str
    provider_id: str


def _display_name_from_user(user) -> str:
    parts = [p for p in (getattr(user, "first_name", None),
                          getattr(user, "last_name", None)) if p]
    return " ".join(parts) if parts else user.username


async def resolve_login(
    *,
    db: AsyncSession,
    username: str,
    password: str,
    channel: str = "web",
) -> LoginOutcome | None:
    # --- Step 1: legacy `authenticate` hook (unchanged existing seam) -------
    legacy_results = await run_hooks(
        "authenticate", username=username, password=password, db=db
    )
    legacy_user = next((r for r in legacy_results if r is not None), None)
    if legacy_user is not None:
        return LoginOutcome(
            user_id=int(legacy_user.id),
            display_name=_display_name_from_user(legacy_user),
            provider_id="legacy_hook",
        )

    # --- Step 2: provider registry credential walk -------------------------
    result = await get_registry().authenticate(
        username=username, password=password, channel=channel
    )
    if result is None:
        return None  # every provider declined → 401 (bad credentials)

    # --- Step 3: fire post_authenticate exactly once, before JWT mint ------
    return await resolve_provider_result(result)


async def resolve_provider_result(result: ProviderResult) -> LoginOutcome | None:
    """Turn a provider identity into a Renfield user — the ONE place.

    Shared by the credential walk above and the OAuth redirect callback
    (``api/routes/auth_oauth.py``, #1344), so a federated login can never
    resolve differently from a password login: ``post_authenticate`` fires
    exactly once; without a consumer only a ``db`` subject is a user id.
    """
    if has_hook("post_authenticate"):
        resolutions = await run_hooks("post_authenticate", result=result)
        resolved = next((r for r in resolutions if r is not None), None)
        if resolved is None:
            # Consumer present but unresolved (no signal / version mismatch /
            # consumer crashed → run_hooks swallowed it). Deny — never mint a
            # half-bound token.
            logger.warning(
                f"post_authenticate did not resolve provider "
                f"{result.provider_id!r} result — login denied"
            )
            return None
        return LoginOutcome(
            user_id=int(resolved),
            display_name=result.display_name,
            provider_id=result.provider_id,
        )

    # No consumer registered → standalone legacy fallback.
    if result.provider_id == "db":
        return LoginOutcome(
            user_id=int(result.subject),
            display_name=result.display_name,
            provider_id=result.provider_id,
        )
    logger.warning(
        f"Provider {result.provider_id!r} authenticated but no "
        f"post_authenticate consumer is registered to resolve a non-DB "
        f"identity — login denied (standalone Renfield)"
    )
    return None
