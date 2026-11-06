"""GitHub OAuth redirect provider. enabled=False by default.

See google.py for the split between route (state + PKCE verifier) and
provider (authorize URL + code exchange). GitHub is plain OAuth, no ID token:
the identity is ``GET /user`` (numeric ``id`` = the stable subject) and the
primary VERIFIED address from ``GET /user/emails`` (#1344).
"""

from __future__ import annotations

from urllib.parse import urlencode

from auth.provider_contract import ProviderResult
from auth.providers.base import (
    RedirectProvider,
    get_json,
    make_result,
    post_form_json,
    secret_value,
)
from utils.config import settings

_AUTHORIZE = "https://github.com/login/oauth/authorize"
_TOKEN = "https://github.com/login/oauth/access_token"
_USER = "https://api.github.com/user"
_EMAILS = "https://api.github.com/user/emails"


def _primary_verified_email(emails: object) -> str | None:
    if not isinstance(emails, list):
        return None
    for entry in emails:
        if (
            isinstance(entry, dict)
            and entry.get("primary") is True
            and entry.get("verified") is True
            and isinstance(entry.get("email"), str)
        ):
            return entry["email"]
    return None


class GitHubProvider(RedirectProvider):
    provider_id = "github"
    supported_channels = frozenset({"web"})

    @property
    def enabled(self) -> bool:
        return settings.oauth_github_enabled

    def is_configured(self) -> bool:
        return bool(
            settings.oauth_github_enabled
            and settings.oauth_github_client_id
            and secret_value(settings.oauth_github_client_secret)
            and settings.oauth_github_redirect_uri
        )

    def authorize_url(
        self, channel: str, *, state: str, code_challenge: str
    ) -> str | None:
        if not self.is_configured():
            return None
        params = {
            "client_id": settings.oauth_github_client_id,
            "redirect_uri": settings.oauth_github_redirect_uri,
            "scope": "read:user user:email",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "allow_signup": "false",
        }
        return f"{_AUTHORIZE}?{urlencode(params)}"

    async def exchange_code(
        self, *, code: str, code_verifier: str, channel: str
    ) -> ProviderResult | None:
        if not self.is_configured() or not code:
            return None
        body = await post_form_json(_TOKEN, {
            "code": code,
            "code_verifier": code_verifier,
            "client_id": settings.oauth_github_client_id,
            "client_secret": secret_value(settings.oauth_github_client_secret),
            "redirect_uri": settings.oauth_github_redirect_uri,
        })
        # GitHub answers 200 with {"error": ...} on a bad code / verifier.
        access_token = body.get("access_token") if body else None
        if not isinstance(access_token, str) or not access_token:
            return None
        user = await get_json(_USER, bearer=access_token)
        if not isinstance(user, dict):
            return None
        subject = user.get("id")
        if isinstance(subject, bool) or not isinstance(subject, int):
            return None
        email = _primary_verified_email(await get_json(_EMAILS, bearer=access_token))
        login = user.get("login") if isinstance(user.get("login"), str) else None
        name = user.get("name") if isinstance(user.get("name"), str) else None
        return make_result(
            provider_id=self.provider_id,
            subject=str(subject),
            display_name=name or login or str(subject),
            channel=channel,
            email=email,
            email_verified=email is not None,
        )
