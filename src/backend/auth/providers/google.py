"""Google OAuth2 / OIDC redirect provider. enabled=False by default.

Not in the credential walk. The route (``api/routes/auth_oauth.py``) owns the
server-bound ``state`` and the PKCE verifier; this class builds the consent URL
and redeems the authorization code at Google's token endpoint (with the
verifier). The identity comes from the ID token received over that TLS back
channel — issuer, audience and expiry are checked (#1344).
"""

from __future__ import annotations

from urllib.parse import urlencode

from auth.provider_contract import ProviderResult
from auth.providers.base import (
    RedirectProvider,
    claim_is_true,
    id_token_claims,
    make_result,
    post_form_json,
    secret_value,
)
from utils.config import settings

_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN = "https://oauth2.googleapis.com/token"
_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})


class GoogleProvider(RedirectProvider):
    provider_id = "google"
    supported_channels = frozenset({"web"})

    @property
    def enabled(self) -> bool:
        return settings.oauth_google_enabled

    def is_configured(self) -> bool:
        return bool(
            settings.oauth_google_enabled
            and settings.oauth_google_client_id
            and secret_value(settings.oauth_google_client_secret)
            and settings.oauth_google_redirect_uri
        )

    def authorize_url(
        self, channel: str, *, state: str, code_challenge: str
    ) -> str | None:
        if not self.is_configured():
            return None
        params = {
            "client_id": settings.oauth_google_client_id,
            "redirect_uri": settings.oauth_google_redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "access_type": "online",
            "prompt": "select_account",
        }
        return f"{_AUTHORIZE}?{urlencode(params)}"

    async def exchange_code(
        self, *, code: str, code_verifier: str, channel: str
    ) -> ProviderResult | None:
        if not self.is_configured() or not code:
            return None
        body = await post_form_json(_TOKEN, {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": code_verifier,
            "client_id": settings.oauth_google_client_id,
            "client_secret": secret_value(settings.oauth_google_client_secret),
            "redirect_uri": settings.oauth_google_redirect_uri,
        })
        if body is None:
            return None
        claims = id_token_claims(
            body.get("id_token"),
            issuers=_ISSUERS,
            audience=settings.oauth_google_client_id,
        )
        if claims is None:
            return None
        email = claims.get("email") if isinstance(claims.get("email"), str) else None
        name = claims.get("name") if isinstance(claims.get("name"), str) else None
        return make_result(
            provider_id=self.provider_id,
            subject=str(claims["sub"]),
            display_name=name or email or str(claims["sub"]),
            channel=channel,
            email=email,
            email_verified=claim_is_true(claims.get("email_verified")),
        )
