"""OAuth redirect login: server-bound ``state`` + PKCE on the IdP leg (#1344).

Two routes under ``/api/auth`` for the redirect providers (google, github,
apple — ``auth/providers/``):

``GET /sso/start?provider=&code_challenge=&state=``
    The SPA (``utils/pkce.ts::beginPkceLogin``) passes the PKCE challenge and
    state of ITS leg (backend -> SPA hand-off). The backend creates the IdP
    leg's own ``state`` and PKCE verifier (``services/oauth_state_store``),
    binds them to this browser with an HttpOnly cookie, and redirects to the
    IdP with ``state`` + S256 ``code_challenge``.

``GET|POST /sso/callback/{provider}``
    Consumes the state (single use), checks the browser binding and the
    provider, redeems the code WITH the verifier, resolves the identity through
    ``resolve_provider_result`` (the same ``post_authenticate`` path a password
    login takes), and redirects to the SPA's ``/auth/callback`` with a one-time
    hand-off code bound to the SPA's challenge + state. No token ever rides in
    a URL; the SPA redeems the code at ``POST /api/auth/sso/exchange``.

Every callback failure answers the SAME opaque 400 — no oracle for which check
failed. Both routes are 404 unless ``SSO_HANDOFF_ENABLED`` is on AND the
provider is enabled and fully configured, so with every provider off (the
default) nothing here exists.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from auth.login_flow import resolve_provider_result
from auth.registry import get_registry
from services.api_rate_limiter import limiter
from services.auth_service import get_user_by_id
from services.database import get_db
from utils.config import settings

router = APIRouter()

#: HttpOnly cookie binding a pending redirect login to the browser that began it.
BINDING_COOKIE = "renfield_oauth_bind"
_CALLBACK_PATH = "/api/auth/sso/callback"
_CHANNEL = "web"
#: The SPA route that redeems the hand-off code (``pages/AuthCallback.tsx``).
_SPA_CALLBACK = "/auth/callback"

# An S256 challenge is base64url(SHA-256) without padding: exactly 43 chars.
_S256_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
# The SPA's state: RFC 7636 unreserved set, long enough to be unguessable.
_SPA_STATE_RE = re.compile(r"^[A-Za-z0-9._~-]{16,128}$")

_OPAQUE_DETAIL = "Invalid or expired authorization request"


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


def _usable_provider(provider_id: str):
    if not settings.sso_handoff_enabled:
        return None
    return get_registry().redirect_provider(provider_id, _CHANNEL)


def _cookie_attrs(provider) -> dict:
    """Apple returns with a cross-site POST: a Lax cookie would not come along,
    so form-post providers get ``SameSite=None`` (which requires ``Secure``)."""
    if getattr(provider, "callback_is_form_post", False):
        return {"secure": True, "samesite": "none"}
    return {"secure": settings.cookie_secure, "samesite": "lax"}


def _opaque_failure(provider) -> JSONResponse:
    resp = JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST, content={"detail": _OPAQUE_DETAIL},
    )
    resp.delete_cookie(
        BINDING_COOKIE, path=_CALLBACK_PATH, httponly=True, **_cookie_attrs(provider),
    )
    return resp


@router.get("/sso/start")
@limiter.limit(settings.api_rate_limit_auth)
async def oauth_start(
    request: Request, provider: str, code_challenge: str, state: str,
):
    """Begin a redirect login: bind state + PKCE server-side, go to the IdP."""
    redirect_provider = _usable_provider(provider)
    if redirect_provider is None:
        raise _not_found()
    if not _S256_CHALLENGE_RE.match(code_challenge) or not _SPA_STATE_RE.match(state):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_OPAQUE_DETAIL)

    from services.oauth_state_store import begin_login

    try:
        start = await begin_login(
            provider_id=redirect_provider.provider_id,
            channel=_CHANNEL,
            spa_code_challenge=code_challenge,
            spa_state=state,
        )
    except Exception as e:
        # Without a stored state the callback can never succeed — do not send
        # the person to the IdP at all.
        logger.warning(f"OAuth start: pending login not stored: {type(e).__name__}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sign-in temporarily unavailable",
        ) from None

    url = redirect_provider.authorize_url(
        _CHANNEL, state=start.state, code_challenge=start.code_challenge,
    )
    if not url:
        raise _not_found()
    resp = RedirectResponse(url, status_code=status.HTTP_302_FOUND)
    resp.set_cookie(
        BINDING_COOKIE,
        start.binding,
        max_age=settings.oauth_state_ttl_seconds,
        path=_CALLBACK_PATH,
        httponly=True,
        **_cookie_attrs(redirect_provider),
    )
    return resp


@router.api_route("/sso/callback/{provider_id}", methods=["GET", "POST"])
@limiter.limit(settings.api_rate_limit_auth)
async def oauth_callback(
    request: Request, provider_id: str, db: AsyncSession = Depends(get_db),
):
    """The IdP's return: verify state + binding, redeem with PKCE, hand off."""
    provider = _usable_provider(provider_id)
    if provider is None:
        raise _not_found()

    if request.method == "POST":
        try:
            params = dict(await request.form())
        except Exception:
            params = {}
    else:
        params = dict(request.query_params)

    from services.oauth_state_store import consume_login

    # Consume FIRST: even a failed callback burns the state, so a raced or
    # replayed one cannot be retried.
    pending = await consume_login(
        state=params.get("state") if isinstance(params.get("state"), str) else None,
        provider_id=provider.provider_id,
        binding=request.cookies.get(BINDING_COOKIE),
    )
    if pending is None:
        return _opaque_failure(provider)
    code = params.get("code")
    if params.get("error") or not isinstance(code, str) or not code:
        logger.info(f"OAuth callback: no authorization code from {provider_id!r}")
        return _opaque_failure(provider)

    try:
        result = await provider.exchange_code(
            code=code, code_verifier=pending.code_verifier, channel=pending.channel,
        )
    except Exception as e:
        logger.warning(f"OAuth callback: {provider_id!r} exchange raised {type(e).__name__}")
        result = None
    if result is None or result.provider_id != provider.provider_id:
        logger.warning(f"OAuth callback: {provider_id!r} code exchange failed")
        return _opaque_failure(provider)

    outcome = await resolve_provider_result(result)
    if outcome is None:
        return _opaque_failure(provider)
    user = await get_user_by_id(db, outcome.user_id)
    if not user or not user.is_active:
        logger.warning(f"OAuth callback: resolved account missing or inactive ({provider_id!r})")
        return _opaque_failure(provider)

    # 🛑 A federated login cannot present the voice. An account that requires it
    # gets no hand-off here — the password path is its only door (fail closed).
    from services.voice_factor_preconditions import second_factor_applies

    if await second_factor_applies(db, user):
        logger.warning(
            f"OAuth callback refused: voice second factor required (user={user.username})"
        )
        return _opaque_failure(provider)

    from services.sso_handoff_store import HandoffSession, issue_handoff_code

    try:
        handoff = await issue_handoff_code(HandoffSession(
            user_id=user.id,
            code_challenge=pending.spa_code_challenge,
            state=pending.spa_state,
            provider=provider.provider_id,
        ))
    except Exception as e:
        logger.warning(f"OAuth callback: hand-off code not stored: {type(e).__name__}")
        return _opaque_failure(provider)

    user.last_login = datetime.now(UTC).replace(tzinfo=None)
    await db.commit()
    logger.info(f"OAuth login: user={user.username} provider={provider.provider_id!r}")

    resp = RedirectResponse(
        f"{_SPA_CALLBACK}?{urlencode({'code': handoff, 'state': pending.spa_state})}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
    resp.delete_cookie(
        BINDING_COOKIE, path=_CALLBACK_PATH, httponly=True, **_cookie_attrs(provider),
    )
    return resp
