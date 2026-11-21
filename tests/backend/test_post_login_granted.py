"""``post_login_granted`` fires once per GRANTED login, never on a refusal.

Covers the three grant sites — password (``/auth/login``), voice second factor
(``/auth/voice``), redirect callback (``/auth/sso/callback/{provider}``) — plus
the provider list and the password-form switch in ``GET /api/auth/status``.
The SSO-side tests reuse the fixtures of ``test_auth_oauth`` (imported as a
module so pytest does not collect its tests twice).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException, Response

import tests.backend.test_auth_oauth as oauth_t
import tests.backend.test_flags_that_break_when_enabled as flags_t
from tests.backend.dbrows import ensure_user
from utils.config import settings
from utils.hooks import clear_hooks, register_hook

fake_redis = oauth_t.fake_redis
google_on = oauth_t.google_on


@pytest.fixture(autouse=True)
def _clean_hooks():
    clear_hooks()
    yield
    clear_hooks()


@pytest.fixture
def granted():
    calls: list[dict] = []

    async def handler(**kw):
        calls.append(kw)

    register_hook("post_login_granted", handler)
    return calls


@pytest.fixture
def no_rate_limit(monkeypatch):
    from services.api_rate_limiter import limiter

    monkeypatch.setattr(limiter, "enabled", False)


# ---------------------------------------------------------------------------
# Password path — the route function called directly (pattern from
# test_auth_login_lockout_wiring.py), credential walk and user load replaced.
# ---------------------------------------------------------------------------

def _store():
    return MagicMock(
        is_locked=AsyncMock(return_value=False),
        record_failure=AsyncMock(return_value=False),
        clear=AsyncMock(),
    )


def _active_user(**over):
    u = MagicMock(id=7, username="ada", is_active=True, token_epoch=0,
                  must_change_password=False)
    for k, v in over.items():
        setattr(u, k, v)
    return u


async def _password_login(*, outcome, user, second_factor=False, ticket="tkt"):
    from api.routes.auth import login

    form = MagicMock(username="ada", password="pw")
    with patch("services.login_lockout.login_lockout", _store()), \
         patch("auth.login_flow.resolve_login", AsyncMock(return_value=outcome)), \
         patch("api.routes.auth.get_user_by_id", AsyncMock(return_value=user)), \
         patch("services.voice_factor_preconditions.second_factor_applies",
               AsyncMock(return_value=second_factor)), \
         patch("services.voice_second_factor_store.issue_ticket",
               AsyncMock(return_value=ticket)) as issue:
        resp = await login(request=_lockout_request(), form_data=form,
                           db=AsyncMock(), response=Response())
    return resp, issue


def _lockout_request():
    from tests.backend.test_auth_login_lockout_wiring import _limiter_request

    return _limiter_request()


@pytest.mark.unit
async def test_password_login_fires_once_before_the_tokens(granted):
    outcome = MagicMock(user_id=7, provider_id="ldap", display_name="Ada")
    order: list[str] = []

    async def handler(**kw):
        order.append("hook")

    register_hook("post_login_granted", handler)
    from services import auth_service

    real = auth_service.create_access_token

    def minted(*a, **kw):
        order.append("token")
        return real(*a, **kw)

    with patch("api.routes.auth.create_access_token", minted):
        resp, _ = await _password_login(outcome=outcome, user=_active_user())

    assert resp.access_token
    assert granted == [{"user_id": 7, "provider_id": "ldap", "channel": "web"}]
    assert order[:2] == ["hook", "token"]


@pytest.mark.unit
async def test_password_bad_credentials_do_not_fire(granted):
    with pytest.raises(HTTPException):
        await _password_login(outcome=None, user=None)
    assert granted == []


@pytest.mark.unit
async def test_password_inactive_account_does_not_fire(granted):
    outcome = MagicMock(user_id=7, provider_id="db", display_name="Ada")
    with pytest.raises(HTTPException):
        await _password_login(outcome=outcome, user=_active_user(is_active=False))
    assert granted == []


@pytest.mark.unit
async def test_password_pending_second_factor_does_not_fire_and_keeps_the_provider(granted):
    outcome = MagicMock(user_id=7, provider_id="ldap", display_name="Ada")
    resp, issue = await _password_login(outcome=outcome, user=_active_user(), second_factor=True)
    assert resp.access_token is None and resp.second_factor == "voice"
    assert granted == []
    # The ticket carries the first-factor provider for the voice step.
    assert issue.await_args.args[2] == "ldap"


@pytest.mark.unit
async def test_a_crashing_consumer_does_not_undo_the_login():
    async def boom(**kw):
        raise RuntimeError("consumer bug")

    register_hook("post_login_granted", boom)
    outcome = MagicMock(user_id=7, provider_id="ldap", display_name="Ada")
    resp, _ = await _password_login(outcome=outcome, user=_active_user())
    assert resp.access_token


# ---------------------------------------------------------------------------
# Voice second factor — the real route, only the ML half and the ticket stubbed
# (helpers of TestVoiceSecondFactor in test_flags_that_break_when_enabled.py).
# ---------------------------------------------------------------------------

_V = flags_t.TestVoiceSecondFactor


def _voice_ticket(monkeypatch, *, user_id, provider_id):
    from services import voice_second_factor_store as store

    async def _consume(_t, _ip):
        return store.PendingVoiceFactor(user_id=user_id, client_ip=None, provider_id=provider_id)

    monkeypatch.setattr(store, "consume_ticket", _consume)


@pytest.mark.database
async def test_voice_second_factor_fires_with_the_first_factor_provider(
    db_session, monkeypatch, granted, no_rate_limit,
):
    from api.routes import auth as auth_routes

    user = await _V._user(_V(), db_session)
    _V._on(monkeypatch)
    _voice_ticket(monkeypatch, user_id=user.id, provider_id="ldap")
    _V._stub_voice(monkeypatch, verified=True, score=0.99)

    out = await auth_routes.voice_authenticate(
        _V._request(), Response(), ticket="t", audio_file=_V._upload(), db=db_session)
    assert out.success is True
    assert granted == [{"user_id": user.id, "provider_id": "ldap", "channel": "web"}]


@pytest.mark.database
async def test_a_refused_voice_does_not_fire(db_session, monkeypatch, granted, no_rate_limit):
    from api.routes import auth as auth_routes

    user = await _V._user(_V(), db_session)
    _V._on(monkeypatch)
    _voice_ticket(monkeypatch, user_id=user.id, provider_id="ldap")
    _V._stub_voice(monkeypatch, verified=False, score=0.1)

    out = await auth_routes.voice_authenticate(
        _V._request(), Response(), ticket="t", audio_file=_V._upload(), db=db_session)
    assert out.success is False
    assert granted == []


# ---------------------------------------------------------------------------
# Redirect (SSO) callback
# ---------------------------------------------------------------------------

async def _sso_callback(async_client, monkeypatch, *, id_token=None):
    oauth_t._mock_idp(
        monkeypatch, lambda r: httpx.Response(200, json={"id_token": id_token or oauth_t._id_token()}))
    state = await oauth_t._started(async_client)
    return await async_client.get(
        "/api/auth/sso/callback/google", params={"code": "c", "state": state})


@pytest.mark.database
async def test_sso_callback_fires_once_after_the_handoff_code(
    async_client, db_session, google_on, fake_redis, monkeypatch, granted,
):
    await ensure_user(db_session, 1, username="ada")
    await db_session.commit()
    await oauth_t._resolving_consumer(1)
    seen_codes: list[bool] = []

    async def handler(**kw):
        seen_codes.append(any(k.startswith("sso:handoff:") for k in fake_redis.store))

    register_hook("post_login_granted", handler)
    r = await _sso_callback(async_client, monkeypatch)
    assert r.status_code == 303
    assert granted == [{"user_id": 1, "provider_id": "google", "channel": "web"}]
    assert seen_codes == [True], "fires after the hand-off code is stored"

    # Redeeming the code at /sso/exchange does NOT fire a second time.
    from urllib.parse import parse_qs, urlparse
    q = {k: v[0] for k, v in parse_qs(urlparse(r.headers["location"]).query).items()}
    ex = await async_client.post("/api/auth/sso/exchange", json={
        "code": q["code"], "code_verifier": oauth_t._SPA_VERIFIER, "state": oauth_t._SPA_STATE})
    assert ex.status_code == 200
    assert len(granted) == 1


@pytest.mark.database
async def test_sso_refusals_do_not_fire(
    async_client, db_session, google_on, fake_redis, monkeypatch, granted,
):
    user = await ensure_user(db_session, 1, username="ada")
    await db_session.commit()
    await oauth_t._resolving_consumer(1)

    # unresolved identity
    r = await _sso_callback(async_client, monkeypatch, id_token=oauth_t._id_token(sub="stranger"))
    assert r.status_code == 400
    # voice second factor required
    with patch("services.voice_factor_preconditions.second_factor_applies",
               AsyncMock(return_value=True)):
        assert (await _sso_callback(async_client, monkeypatch)).status_code == 400
    # inactive account
    user.is_active = False
    await db_session.commit()
    assert (await _sso_callback(async_client, monkeypatch)).status_code == 400
    assert granted == []


@pytest.mark.database
async def test_sso_handoff_not_stored_does_not_fire(
    async_client, db_session, google_on, fake_redis, monkeypatch, granted,
):
    from services import sso_handoff_store

    await ensure_user(db_session, 1, username="ada")
    await db_session.commit()
    await oauth_t._resolving_consumer(1)

    async def collide(session):
        raise sso_handoff_store.HandoffCodeCollision("x")

    monkeypatch.setattr(sso_handoff_store, "issue_handoff_code", collide)
    assert (await _sso_callback(async_client, monkeypatch)).status_code == 400
    assert granted == []


# ---------------------------------------------------------------------------
# Voice ticket store carries the provider
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_ticket_round_trips_the_provider(monkeypatch):
    from services import voice_second_factor_store as store

    fake = oauth_t._FakeRedis()
    monkeypatch.setattr(store, "get_redis", lambda: fake)
    t = await store.issue_ticket(42, None, "ldap")
    pending = await store.consume_ticket(t, None)
    assert pending.user_id == 42 and pending.provider_id == "ldap"
    # A ticket from before the field existed still redeems (provider None).
    old = await store.issue_ticket(43, None)
    assert (await store.consume_ticket(old, None)).provider_id is None


# ---------------------------------------------------------------------------
# GET /api/auth/status — provider buttons + password-form switch
# ---------------------------------------------------------------------------

@pytest.mark.database
async def test_status_lists_nothing_while_sso_is_off(async_client, monkeypatch):
    monkeypatch.setattr(settings, "sso_handoff_enabled", False)
    monkeypatch.setattr(settings, "login_password_form", "collapsed")
    body = (await async_client.get("/api/auth/status")).json()
    assert body["sso_providers"] == []
    # No provider → the form is the only door and stays visible.
    assert body["password_form"] == "visible"


@pytest.mark.database
async def test_status_lists_a_usable_provider_with_its_logout_target(
    async_client, google_on, monkeypatch,
):
    from auth.providers.google import GoogleProvider

    monkeypatch.setattr(GoogleProvider, "display_name", "Google", raising=False)
    body = (await async_client.get("/api/auth/status")).json()
    assert body["sso_providers"] == [
        {"id": "google", "display_name": "Google", "logout_url": None}]
    assert body["password_form"] == "visible"

    monkeypatch.setattr(settings, "login_password_form", "collapsed")
    assert (await async_client.get("/api/auth/status")).json()["password_form"] == "collapsed"

    monkeypatch.setattr(GoogleProvider, "logout_url",
                        lambda self, channel: "https://idp/logout?client_id=x", raising=False)
    body = (await async_client.get("/api/auth/status")).json()
    assert body["sso_providers"][0]["logout_url"] == "https://idp/logout?client_id=x"


@pytest.mark.database
async def test_status_falls_back_to_the_id_and_survives_a_broken_logout_url(
    async_client, google_on, monkeypatch,
):
    from auth.providers.google import GoogleProvider

    body = (await async_client.get("/api/auth/status")).json()
    assert body["sso_providers"][0]["display_name"] == "google"

    def broken(self, channel):
        raise RuntimeError("bad metadata")

    monkeypatch.setattr(GoogleProvider, "logout_url", broken, raising=False)
    r = await async_client.get("/api/auth/status")
    assert r.status_code == 200
    # The button stays (/sso/start still accepts it); only the IdP logout goes.
    assert r.json()["sso_providers"] == [
        {"id": "google", "display_name": "google", "logout_url": None}]
