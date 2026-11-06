"""Shared helpers + base classes for built-in providers.

These do not extend the cross-repo contract (``provider_contract.py`` stays
byte-faithful to ebongard/renfield#591). They are Renfield-internal
conveniences so each provider is small and consistent.
"""

from __future__ import annotations

from auth.provider_contract import (
    PROVIDER_RESULT_CONTRACT_VERSION,
    ProviderResult,
)


def normalize_email(email: str | None) -> str | None:
    """Contract-locked normalization: lowercase + trim ONLY.

    No Gmail dot/plus folding. Empty/whitespace-only → None. Treat the result
    as opaque (see ProviderResult.email docstring).
    """
    if email is None:
        return None
    cleaned = email.strip().lower()
    return cleaned or None


def make_result(
    *,
    provider_id: str,
    subject: str,
    display_name: str,
    channel: str,
    email: str | None = None,
    email_verified: bool = False,
    system_projections: dict[str, str] | None = None,
    extras: dict[str, str] | None = None,
) -> ProviderResult:
    """Build a ``ProviderResult`` with ``v`` pinned to the contract version and
    the email normalized. Single construction point so every built-in provider
    stamps ``v`` and normalizes identically."""
    return ProviderResult(
        v=PROVIDER_RESULT_CONTRACT_VERSION,
        provider_id=provider_id,
        subject=subject,
        email=normalize_email(email),
        email_verified=email_verified and normalize_email(email) is not None,
        display_name=display_name,
        channel=channel,
        system_projections=dict(system_projections or {}),
        extras=dict(extras or {}),
    )


class RedirectProvider:
    """Base for federated/redirect providers (google, github, apple, …).

    Not in the credential walk: ``authenticate`` always returns ``None``. The
    login starts at ``POST /api/auth/sso/start`` and ends at
    ``/api/auth/sso/callback/{provider}`` (``api/routes/auth_oauth.py``): the
    route owns the server-bound ``state`` and the PKCE verifier
    (``services/oauth_state_store``) and hands both in here. A provider only
    builds the authorize URL and turns an authorization code into a
    ``ProviderResult`` — it never generates or checks ``state`` itself (#1344).
    """

    provider_id: str = ""
    supported_channels: frozenset[str] = frozenset({"web"})
    # priority is ignored for redirect providers; kept high so an accidental
    # appearance in a sorted view never precedes a real credential provider.
    priority: int = 1000
    enabled: bool = False
    #: True when the IdP returns to the callback with a cross-site POST
    #: (``response_mode=form_post``) — the binding cookie must then be
    #: ``SameSite=None`` or the browser drops it.
    callback_is_form_post: bool = False

    async def authenticate(
        self, *, username: str, password: str, channel: str
    ) -> ProviderResult | None:
        return None

    def is_configured(self) -> bool:
        """Enabled AND every setting the code exchange needs is present."""
        return False

    def authorize_url(
        self, channel: str, *, state: str, code_challenge: str
    ) -> str | None:
        """The IdP authorize URL carrying the server-bound ``state`` and the
        S256 ``code_challenge``, or None when the provider is not usable."""
        return None

    async def exchange_code(
        self, *, code: str, code_verifier: str, channel: str
    ) -> ProviderResult | None:
        """Redeem ``code`` (+ the PKCE verifier) at the IdP and return the
        identity, or None on ANY failure (fail closed, never raises)."""
        return None


# ---------------------------------------------------------------------------
# IdP plumbing shared by the redirect providers
# ---------------------------------------------------------------------------


def _timeout() -> float:
    from utils.config import settings

    return float(settings.auth_provider_timeout_seconds)


async def post_form_json(url: str, data: dict[str, str]) -> dict | None:
    """POST a form to an IdP endpoint and return the JSON body, or None on any
    transport error, non-2xx answer or non-object body. Never logs ``data``
    (it holds the client secret, the code and the PKCE verifier)."""
    import httpx
    from loguru import logger

    try:
        async with httpx.AsyncClient(timeout=_timeout()) as client:
            resp = await client.post(
                url, data=data, headers={"Accept": "application/json"}
            )
    except httpx.HTTPError as e:
        logger.warning(f"OAuth token endpoint unreachable ({url}): {type(e).__name__}")
        return None
    if resp.status_code // 100 != 2:
        logger.warning(f"OAuth token endpoint {url} answered {resp.status_code}")
        return None
    try:
        body = resp.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


async def get_json(url: str, *, bearer: str) -> dict | list | None:
    """GET an IdP API resource with a bearer token; None on any failure."""
    import httpx
    from loguru import logger

    try:
        async with httpx.AsyncClient(timeout=_timeout()) as client:
            resp = await client.get(
                url,
                headers={
                    "Authorization": f"Bearer {bearer}",
                    "Accept": "application/json",
                },
            )
    except httpx.HTTPError as e:
        logger.warning(f"OAuth API unreachable ({url}): {type(e).__name__}")
        return None
    if resp.status_code // 100 != 2:
        logger.warning(f"OAuth API {url} answered {resp.status_code}")
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def id_token_claims(
    id_token: object, *, issuers: frozenset[str], audience: str
) -> dict | None:
    """Claims of an ID token received DIRECTLY from the token endpoint.

    OIDC Core 3.1.3.7: a token obtained over the TLS back channel from the
    token endpoint may rely on TLS server validation instead of the signature.
    What must still be checked — issuer, audience, expiry, subject — is checked
    here; any mismatch is None.
    """
    import time

    from jose import jwt as jose_jwt

    if not isinstance(id_token, str) or not id_token:
        return None
    try:
        claims = jose_jwt.get_unverified_claims(id_token)
    except Exception:
        return None
    if not isinstance(claims, dict):
        return None
    if claims.get("iss") not in issuers:
        return None
    aud = claims.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if not audience or audience not in auds:
        return None
    exp = claims.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, int | float) or exp <= time.time():
        return None
    if not claims.get("sub"):
        return None
    return claims


def secret_value(value: object) -> str:
    """Plain value of a ``SecretStr`` setting. The class default ``""`` stays a
    plain ``str`` (pydantic does not validate defaults), so accept both."""
    getter = getattr(value, "get_secret_value", None)
    raw = getter() if callable(getter) else value
    return raw if isinstance(raw, str) else ""


def claim_is_true(value: object) -> bool:
    """``email_verified`` arrives as a bool (Google) or the string "true"
    (Apple)."""
    return value is True or (isinstance(value, str) and value.lower() == "true")
