"""Sign in with Apple redirect provider. enabled=False by default.

See google.py for the split between route (state + PKCE verifier) and
provider (authorize URL + code exchange). Apple specifics:

- ``response_mode=form_post``: the callback arrives as a cross-site POST, so
  the route's browser-binding cookie is ``SameSite=None; Secure`` for Apple.
- The client secret is a short-lived ES256 JWT signed with the team's private
  key (``OAUTH_APPLE_TEAM_ID`` / ``_KEY_ID`` / ``_PRIVATE_KEY``), minted per
  exchange.
- Apple does not document PKCE. The challenge and verifier are sent anyway (an
  authorization server ignores unknown parameters, RFC 6749 §3.1/§3.2); the
  binding that holds regardless is the server-bound ``state`` (#1344).
"""

from __future__ import annotations

import time
from urllib.parse import urlencode

from loguru import logger

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

_AUTHORIZE = "https://appleid.apple.com/auth/authorize"
_TOKEN = "https://appleid.apple.com/auth/token"
_ISSUER = "https://appleid.apple.com"
_CLIENT_SECRET_TTL_SECONDS = 300


def _client_secret() -> str | None:
    """The ES256 client-secret JWT Apple's token endpoint expects."""
    from jose import jwt as jose_jwt

    now = int(time.time())
    try:
        return jose_jwt.encode(
            {
                "iss": settings.oauth_apple_team_id,
                "iat": now,
                "exp": now + _CLIENT_SECRET_TTL_SECONDS,
                "aud": _ISSUER,
                "sub": settings.oauth_apple_client_id,
            },
            secret_value(settings.oauth_apple_private_key),
            algorithm="ES256",
            headers={"kid": settings.oauth_apple_key_id},
        )
    except Exception as e:
        # Never log the key or the exception text (it may quote key material).
        logger.warning(f"Apple client secret could not be signed: {type(e).__name__}")
        return None


class AppleProvider(RedirectProvider):
    provider_id = "apple"
    supported_channels = frozenset({"web"})
    #: The IdP answers with a cross-site POST (``response_mode=form_post``).
    callback_is_form_post = True

    @property
    def enabled(self) -> bool:
        return settings.oauth_apple_enabled

    def is_configured(self) -> bool:
        return bool(
            settings.oauth_apple_enabled
            and settings.oauth_apple_client_id
            and settings.oauth_apple_team_id
            and settings.oauth_apple_key_id
            and secret_value(settings.oauth_apple_private_key)
            and settings.oauth_apple_redirect_uri
        )

    def authorize_url(
        self, channel: str, *, state: str, code_challenge: str
    ) -> str | None:
        if not self.is_configured():
            return None
        params = {
            "client_id": settings.oauth_apple_client_id,
            "redirect_uri": settings.oauth_apple_redirect_uri,
            "response_type": "code",
            "scope": "name email",
            "response_mode": "form_post",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{_AUTHORIZE}?{urlencode(params)}"

    async def exchange_code(
        self, *, code: str, code_verifier: str, channel: str
    ) -> ProviderResult | None:
        if not self.is_configured() or not code:
            return None
        client_secret = _client_secret()
        if client_secret is None:
            return None
        body = await post_form_json(_TOKEN, {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": code_verifier,
            "client_id": settings.oauth_apple_client_id,
            "client_secret": client_secret,
            "redirect_uri": settings.oauth_apple_redirect_uri,
        })
        if body is None:
            return None
        claims = id_token_claims(
            body.get("id_token"),
            issuers=frozenset({_ISSUER}),
            audience=settings.oauth_apple_client_id,
        )
        if claims is None:
            return None
        email = claims.get("email") if isinstance(claims.get("email"), str) else None
        return make_result(
            provider_id=self.provider_id,
            subject=str(claims["sub"]),
            display_name=email or str(claims["sub"]),
            channel=channel,
            email=email,
            email_verified=claim_is_true(claims.get("email_verified")),
        )
