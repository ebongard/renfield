"""OAuth redirect login: server-bound state + PKCE (#1344).

Covers
- ``services/oauth_state_store``: single use, browser binding, provider
  binding, expiry, corrupt payloads, Redis failure, collision;
- the providers' authorize URL (given state + S256 challenge) and code
  exchange against a mocked IdP (``httpx.MockTransport``): Google and Apple via
  the ID token (issuer / audience / expiry checked), GitHub via ``/user`` +
  ``/user/emails``; the Apple client secret is a verifiable ES256 JWT;
- ``POST /api/auth/sso/start`` (same-origin JSON, answers the IdP URL — no
  redirect a cross-site page could trigger) + ``GET|POST
  /api/auth/sso/callback/{provider}``: flag-off 404 (also without or with
  malformed parameters, and for a GET), non-JSON start refused, missing / foreign / reused / expired state, wrong verifier at
  the IdP, unresolved identity, inactive account, voice second factor — every
  failure the SAME opaque 400 — and the happy path end to end through
  ``POST /api/auth/sso/exchange``;
- the config validator (enabled provider without the hand-off fails boot).
"""
from __future__ import annotations

import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from jose import jwt as jose_jwt
from pydantic import SecretStr

from tests.backend.dbrows import ensure_user
from utils.config import settings
from utils.hooks import clear_hooks, register_hook

_SPA_VERIFIER = "v" * 64
_SPA_STATE = "spa-state-0123456789"


class _FakeRedis:
    """Async Redis subset with SET ex/nx, GETDEL and a controllable clock."""

    def __init__(self):
        self.store: dict[str, tuple[str, float | None]] = {}
        self.now = 0.0

    def _live(self, key):
        item = self.store.get(key)
        if item is None:
            return None
        value, expires = item
        if expires is not None and self.now >= expires:
            del self.store[key]
            return None
        return value

    async def set(self, key, value, ex=None, nx=False):
        if nx and self._live(key) is not None:
            return None
        self.store[key] = (value, self.now + ex if ex else None)
        return True

    async def getdel(self, key):
        value = self._live(key)
        self.store.pop(key, None)
        return value


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr("services.oauth_state_store.get_redis", lambda: fake)
    monkeypatch.setattr("services.sso_handoff_store.get_redis", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def _clean_hooks():
    clear_hooks()
    yield
    clear_hooks()


@pytest.fixture
def google_on(monkeypatch):
    monkeypatch.setattr(settings, "sso_handoff_enabled", True)
    monkeypatch.setattr(settings, "oauth_google_enabled", True)
    monkeypatch.setattr(settings, "oauth_google_client_id", "gcid")
    monkeypatch.setattr(settings, "oauth_google_client_secret", SecretStr("gsec"))
    monkeypatch.setattr(settings, "oauth_google_redirect_uri", "https://r/cb/google")
    # The test client talks plain http: a Secure cookie would never come back.
    monkeypatch.setattr(settings, "cookie_secure", False)


def _spa_challenge() -> str:
    from services.sso_handoff_store import s256_challenge
    return s256_challenge(_SPA_VERIFIER)


def _id_token(**over) -> str:
    claims = {
        "iss": "https://accounts.google.com", "aud": "gcid", "sub": "g-123",
        "exp": int(time.time()) + 300, "email": "Ada@Example.org",
        "email_verified": True, "name": "Ada L",
    }
    claims.update(over)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jose_jwt.encode(claims, "irrelevant", algorithm="HS256")


def _mock_idp(monkeypatch, handler):
    """Route every httpx.AsyncClient the providers open through *handler*."""
    real = httpx.AsyncClient
    seen: list[httpx.Request] = []

    def _record(request):
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(_record), **kw),
    )
    return seen


def _form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


# ---------------------------------------------------------------------------
# State store
# ---------------------------------------------------------------------------


async def _begin(provider="google"):
    from services.oauth_state_store import begin_login
    return await begin_login(
        provider_id=provider, channel="web",
        spa_code_challenge=_spa_challenge(), spa_state=_SPA_STATE,
    )


async def test_state_is_single_use_and_carries_the_verifier(fake_redis, monkeypatch):
    from services.oauth_state_store import consume_login
    from services.sso_handoff_store import s256_challenge
    monkeypatch.setattr(settings, "oauth_state_ttl_seconds", 600)
    start = await _begin()
    got = await consume_login(state=start.state, provider_id="google", binding=start.binding)
    assert got is not None
    assert s256_challenge(got.code_verifier) == start.code_challenge  # PKCE pair
    assert got.spa_state == _SPA_STATE and got.spa_code_challenge == _spa_challenge()
    # Only the binding's hash is stored, never the cookie value itself.
    assert start.binding not in json.dumps(list(fake_redis.store.values()))
    assert await consume_login(state=start.state, provider_id="google", binding=start.binding) is None


@pytest.mark.parametrize("state_kind", ["missing", "unknown"])
async def test_state_missing_or_unknown(fake_redis, state_kind):
    from services.oauth_state_store import consume_login
    start = await _begin()
    state = None if state_kind == "missing" else "never-issued"
    assert await consume_login(state=state, provider_id="google", binding=start.binding) is None


async def test_state_foreign_browser_is_refused_and_burned(fake_redis):
    from services.oauth_state_store import consume_login
    start = await _begin()
    assert await consume_login(state=start.state, provider_id="google", binding="other") is None
    assert await consume_login(state=start.state, provider_id="google", binding=None) is None
    # Burned by the failed attempt: the rightful browser cannot use it either.
    assert await consume_login(state=start.state, provider_id="google", binding=start.binding) is None


async def test_state_of_another_provider_is_refused(fake_redis):
    from services.oauth_state_store import consume_login
    start = await _begin("github")
    assert await consume_login(state=start.state, provider_id="google", binding=start.binding) is None


async def test_state_expires(fake_redis, monkeypatch):
    from services.oauth_state_store import consume_login
    monkeypatch.setattr(settings, "oauth_state_ttl_seconds", 600)
    start = await _begin()
    fake_redis.now += 601
    assert await consume_login(state=start.state, provider_id="google", binding=start.binding) is None


async def test_state_corrupt_payload_and_redis_error(fake_redis, monkeypatch):
    from services import oauth_state_store as store
    fake_redis.store["oauth:state:bad"] = ("{not json", None)
    assert await store.consume_login(state="bad", provider_id="google", binding="b") is None
    fake_redis.store["oauth:state:partial"] = (json.dumps({"provider_id": "google"}), None)
    assert await store.consume_login(state="partial", provider_id="google", binding="b") is None

    class _Broken:
        async def getdel(self, key):
            raise ConnectionError("down")

    monkeypatch.setattr(store, "get_redis", lambda: _Broken())
    assert await store.consume_login(state="x", provider_id="google", binding="b") is None


async def test_state_collision_raises(fake_redis, monkeypatch):
    from services import oauth_state_store as store
    monkeypatch.setattr(store.secrets, "token_urlsafe", lambda n=None: "fixed" * 10)
    await _begin()
    with pytest.raises(store.OAuthStateCollision):
        await _begin()


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


def test_unconfigured_providers_build_nothing():
    from auth.providers.apple import AppleProvider
    from auth.providers.github import GitHubProvider
    from auth.providers.google import GoogleProvider
    for prov in (GoogleProvider(), GitHubProvider(), AppleProvider()):
        assert prov.is_configured() is False
        assert prov.authorize_url("web", state="s", code_challenge="c") is None


async def test_unconfigured_providers_exchange_nothing():
    from auth.providers.apple import AppleProvider
    from auth.providers.base import RedirectProvider
    from auth.providers.github import GitHubProvider
    from auth.providers.google import GoogleProvider
    for prov in (GoogleProvider(), GitHubProvider(), AppleProvider(), RedirectProvider()):
        assert await prov.exchange_code(code="c", code_verifier="v", channel="web") is None
    base = RedirectProvider()
    assert base.is_configured() is False
    assert base.authorize_url("web", state="s", code_challenge="c") is None


async def test_google_exchange_sends_verifier_and_reads_id_token(google_on, monkeypatch):
    from auth.providers.google import GoogleProvider
    seen = _mock_idp(monkeypatch, lambda r: httpx.Response(200, json={"id_token": _id_token()}))
    res = await GoogleProvider().exchange_code(code="the-code", code_verifier="VERIFIER", channel="web")
    assert res is not None
    assert (res.provider_id, res.subject, res.display_name) == ("google", "g-123", "Ada L")
    assert res.email == "ada@example.org" and res.email_verified is True
    sent = _form(seen[0])
    assert sent["code_verifier"] == "VERIFIER" and sent["code"] == "the-code"
    assert sent["client_secret"] == "gsec" and sent["grant_type"] == "authorization_code"


@pytest.mark.parametrize("token_over", [
    {"aud": "someone-else"},
    {"iss": "https://evil.example"},
    {"exp": int(time.time()) - 1},
    {"sub": None},
])
async def test_google_rejects_bad_id_token_claims(google_on, monkeypatch, token_over):
    from auth.providers.google import GoogleProvider
    _mock_idp(monkeypatch, lambda r: httpx.Response(200, json={"id_token": _id_token(**token_over)}))
    assert await GoogleProvider().exchange_code(code="c", code_verifier="v", channel="web") is None


@pytest.mark.parametrize("response", [
    httpx.Response(400, json={"error": "invalid_grant"}),   # e.g. a wrong code_verifier
    httpx.Response(200, json={"access_token": "no-id-token"}),
    httpx.Response(200, json={"id_token": "not.a.jwt"}),
    httpx.Response(200, text="not json"),
    httpx.Response(200, json=["a", "list"]),
])
async def test_google_token_endpoint_failures(google_on, monkeypatch, response):
    from auth.providers.google import GoogleProvider
    _mock_idp(monkeypatch, lambda r: response)
    assert await GoogleProvider().exchange_code(code="c", code_verifier="v", channel="web") is None


async def test_google_transport_error(google_on, monkeypatch):
    from auth.providers.google import GoogleProvider

    def boom(request):
        raise httpx.ConnectError("unreachable")

    _mock_idp(monkeypatch, boom)
    assert await GoogleProvider().exchange_code(code="c", code_verifier="v", channel="web") is None


@pytest.fixture
def github_on(monkeypatch):
    monkeypatch.setattr(settings, "oauth_github_enabled", True)
    monkeypatch.setattr(settings, "oauth_github_client_id", "hcid")
    monkeypatch.setattr(settings, "oauth_github_client_secret", SecretStr("hsec"))
    monkeypatch.setattr(settings, "oauth_github_redirect_uri", "https://r/cb/github")


def _github_handler(*, user=None, emails=None, token_body=None, user_status=200):
    def handler(request):
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json=token_body or {"access_token": "gho_x"})
        if request.url.path == "/user":
            return httpx.Response(user_status, json=user if user is not None else {
                "id": 42, "login": "ada", "name": "Ada L"})
        if request.url.path == "/user/emails":
            return httpx.Response(200, json=emails if emails is not None else [
                {"email": "old@example.org", "primary": False, "verified": True},
                {"email": "Ada@Example.org", "primary": True, "verified": True},
            ])
        return httpx.Response(404)
    return handler


async def test_github_exchange(github_on, monkeypatch):
    from auth.providers.github import GitHubProvider
    seen = _mock_idp(monkeypatch, _github_handler())
    res = await GitHubProvider().exchange_code(code="c", code_verifier="VERIFIER", channel="web")
    assert res is not None and res.subject == "42" and res.display_name == "Ada L"
    assert res.email == "ada@example.org" and res.email_verified is True
    assert _form(seen[0])["code_verifier"] == "VERIFIER"
    assert seen[1].headers["Authorization"] == "Bearer gho_x"


async def test_github_unverified_primary_email_is_dropped(github_on, monkeypatch):
    from auth.providers.github import GitHubProvider
    _mock_idp(monkeypatch, _github_handler(
        user={"id": 42, "login": "ada"},
        emails=[{"email": "ada@example.org", "primary": True, "verified": False}]))
    res = await GitHubProvider().exchange_code(code="c", code_verifier="v", channel="web")
    assert res is not None and res.email is None and res.email_verified is False
    assert res.display_name == "ada"


@pytest.mark.parametrize("kw", [
    {"token_body": {"error": "bad_verification_code"}},   # GitHub: 200 + error
    {"user": ["not", "a", "dict"]},
    {"user": {"id": True, "login": "x"}},
    {"user": {"login": "no-id"}},
    {"user_status": 401},
])
async def test_github_failures(github_on, monkeypatch, kw):
    from auth.providers.github import GitHubProvider
    _mock_idp(monkeypatch, _github_handler(**kw))
    assert await GitHubProvider().exchange_code(code="c", code_verifier="v", channel="web") is None


async def test_github_api_transport_error(github_on, monkeypatch):
    from auth.providers.github import GitHubProvider

    def handler(request):
        if request.url.path == "/user":
            raise httpx.ReadTimeout("slow")
        return _github_handler()(request)

    _mock_idp(monkeypatch, handler)
    assert await GitHubProvider().exchange_code(code="c", code_verifier="v", channel="web") is None


@pytest.fixture
def apple_key():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return pem, pub


@pytest.fixture
def apple_on(monkeypatch, apple_key):
    monkeypatch.setattr(settings, "oauth_apple_enabled", True)
    monkeypatch.setattr(settings, "oauth_apple_client_id", "com.example.web")
    monkeypatch.setattr(settings, "oauth_apple_team_id", "TEAM")
    monkeypatch.setattr(settings, "oauth_apple_key_id", "KID")
    monkeypatch.setattr(settings, "oauth_apple_private_key", SecretStr(apple_key[0]))
    monkeypatch.setattr(settings, "oauth_apple_redirect_uri", "https://r/cb/apple")


async def test_apple_exchange_signs_client_secret_and_reads_id_token(apple_on, apple_key, monkeypatch):
    from auth.providers.apple import AppleProvider
    token = jose_jwt.encode({
        "iss": "https://appleid.apple.com", "aud": "com.example.web", "sub": "a.1",
        "exp": int(time.time()) + 300, "email": "a@privaterelay.example",
        "email_verified": "true"}, "x", algorithm="HS256")
    seen = _mock_idp(monkeypatch, lambda r: httpx.Response(200, json={"id_token": token}))
    prov = AppleProvider()
    url = prov.authorize_url("web", state="S", code_challenge="C")
    assert "response_mode=form_post" in url and "code_challenge_method=S256" in url
    res = await prov.exchange_code(code="c", code_verifier="VERIFIER", channel="web")
    assert res is not None and res.subject == "a.1" and res.email_verified is True
    sent = _form(seen[0])
    assert sent["code_verifier"] == "VERIFIER"
    secret_claims = jose_jwt.decode(
        sent["client_secret"], apple_key[1], algorithms=["ES256"],
        audience="https://appleid.apple.com")
    assert secret_claims["iss"] == "TEAM" and secret_claims["sub"] == "com.example.web"
    assert jose_jwt.get_unverified_header(sent["client_secret"])["kid"] == "KID"


async def test_apple_unusable_private_key(apple_on, monkeypatch):
    from auth.providers.apple import AppleProvider
    monkeypatch.setattr(settings, "oauth_apple_private_key", SecretStr("not a key"))
    seen = _mock_idp(monkeypatch, lambda r: httpx.Response(500))
    assert await AppleProvider().exchange_code(code="c", code_verifier="v", channel="web") is None
    assert seen == []  # never reached the IdP


async def test_apple_token_endpoint_failure(apple_on, monkeypatch):
    from auth.providers.apple import AppleProvider
    _mock_idp(monkeypatch, lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
    assert await AppleProvider().exchange_code(code="c", code_verifier="v", channel="web") is None


def test_id_token_claims_rejects_non_string_and_list_audience():
    from auth.providers.base import id_token_claims
    issuers = frozenset({"https://accounts.google.com"})
    assert id_token_claims(None, issuers=issuers, audience="gcid") is None
    assert id_token_claims(_id_token(exp=True), issuers=issuers, audience="gcid") is None
    assert id_token_claims(_id_token(aud=["x", "gcid"]), issuers=issuers, audience="gcid")
    assert id_token_claims(_id_token(), issuers=issuers, audience="") is None


def test_secret_value_accepts_plain_default():
    from auth.providers.base import secret_value
    assert secret_value("") == ""
    assert secret_value(SecretStr("s")) == "s"
    assert secret_value(None) == ""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def _start(client, **params):
    body = {"provider": "google", "code_challenge": _spa_challenge(), "state": _SPA_STATE}
    body.update(params)
    return await client.post("/api/auth/sso/start", json=body)


def _authorize_query(r) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(r.json()["authorize_url"]).query).items()}


async def test_start_and_callback_404_while_flags_off(async_client, monkeypatch):
    """Flag-off: nothing exists — with the hand-off off, and with it on but
    every provider at its default (off)."""
    monkeypatch.setattr(settings, "sso_handoff_enabled", False)
    assert (await _start(async_client)).status_code == 404
    assert (await async_client.get("/api/auth/sso/callback/google")).status_code == 404
    monkeypatch.setattr(settings, "sso_handoff_enabled", True)
    assert (await _start(async_client)).status_code == 404
    assert (await async_client.post("/api/auth/sso/callback/google")).status_code == 404


@pytest.mark.parametrize("handoff", [False, True])
@pytest.mark.parametrize("call", [
    lambda c: c.post("/api/auth/sso/start"),
    lambda c: c.post("/api/auth/sso/start", json={}),
    lambda c: c.post("/api/auth/sso/start", json=["google"]),
    lambda c: c.post("/api/auth/sso/start", json={"provider": 7}),
    lambda c: c.post("/api/auth/sso/start", content=b"{not json",
                     headers={"Content-Type": "application/json"}),
    lambda c: c.get("/api/auth/sso/start"),
    lambda c: c.get("/api/auth/sso/start", params={"provider": "google"}),
])
async def test_start_is_dark_before_any_validation(async_client, monkeypatch, handoff, call):
    """The gate runs first: no 422 naming the fields, no 405 naming the route."""
    monkeypatch.setattr(settings, "sso_handoff_enabled", handoff)
    r = await call(async_client)
    assert r.status_code == 404, r.text
    assert r.json() == {"detail": "Not found"}


async def test_start_get_is_404_even_with_a_live_provider(async_client, google_on, fake_redis):
    """The forgeable top-level GET is gone: a cross-site link cannot start a login."""
    q = {"provider": "google", "code_challenge": _spa_challenge(), "state": _SPA_STATE}
    r = await async_client.get("/api/auth/sso/start", params=q)
    assert r.status_code == 404
    assert "set-cookie" not in r.headers and fake_redis.store == {}


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded", None])
async def test_start_refuses_a_body_a_cross_site_page_can_send(
    async_client, google_on, fake_redis, content_type,
):
    """Only ``application/json`` (preflighted cross-site) is read; a simple
    request (form, text/plain, a Blob without type) is not a start."""
    body = json.dumps({"provider": "google", "code_challenge": _spa_challenge(), "state": _SPA_STATE})
    headers = {"Content-Type": content_type} if content_type else {}
    r = await async_client.post("/api/auth/sso/start", content=body.encode(), headers=headers)
    assert r.status_code == 404
    assert "set-cookie" not in r.headers and fake_redis.store == {}


@pytest.mark.parametrize("bad", [
    {"code_challenge": "short"},
    {"code_challenge": "!" * 43},
    {"code_challenge": None},
    {"state": "short"},
    {"state": "ä" * 20},
    {"state": 12345678901234567890},
])
async def test_start_rejects_malformed_spa_binding(async_client, google_on, fake_redis, bad):
    r = await _start(async_client, **bad)
    assert r.status_code == 400
    assert r.json() == {"detail": "Invalid or expired authorization request"}
    assert fake_redis.store == {}


async def test_start_returns_the_idp_url_with_server_state_and_pkce(async_client, google_on, fake_redis):
    r = await _start(async_client)
    assert r.status_code == 200 and "location" not in r.headers
    assert r.headers["cache-control"] == "no-store"
    loc = urlparse(r.json()["authorize_url"])
    assert loc.netloc == "accounts.google.com"
    q = _authorize_query(r)
    assert q["code_challenge_method"] == "S256" and len(q["code_challenge"]) == 43
    assert q["state"] != _SPA_STATE                          # the IdP gets the SERVER state
    assert f"oauth:state:{q['state']}" in fake_redis.store  # stored server-side
    cookie = r.headers["set-cookie"]
    assert "renfield_oauth_bind=" in cookie and "HttpOnly" in cookie
    assert "Path=/api/auth/sso/callback" in cookie and "samesite=lax" in cookie.lower()


async def test_start_accepts_json_with_charset(async_client, google_on, fake_redis):
    body = json.dumps({"provider": "google", "code_challenge": _spa_challenge(), "state": _SPA_STATE})
    r = await async_client.post("/api/auth/sso/start", content=body.encode(),
                                headers={"Content-Type": "Application/JSON; charset=utf-8"})
    assert r.status_code == 200 and "authorize_url" in r.json()


async def test_start_503_when_state_cannot_be_stored(async_client, google_on, monkeypatch):
    class _Down:
        async def set(self, *a, **kw):
            raise ConnectionError("redis down")

    monkeypatch.setattr("services.oauth_state_store.get_redis", lambda: _Down())
    r = await _start(async_client)
    assert r.status_code == 503 and "authorize_url" not in r.text
    assert "set-cookie" not in r.headers


async def _resolving_consumer(user_id):
    async def consumer(*, result):
        return user_id if result.provider_id == "google" and result.subject == "g-123" else None
    register_hook("post_authenticate", consumer)


async def _started(client):
    r = await _start(client)
    return _authorize_query(r)["state"]


async def test_callback_happy_path_end_to_end(async_client, db_session, google_on, fake_redis, monkeypatch):
    await ensure_user(db_session, 1, username="ada")
    await db_session.commit()
    await _resolving_consumer(1)
    seen = _mock_idp(monkeypatch, lambda r: httpx.Response(200, json={"id_token": _id_token()}))

    state = await _started(async_client)
    r = await async_client.get("/api/auth/sso/callback/google", params={"code": "idp-code", "state": state})
    assert r.status_code == 303, r.text
    loc = urlparse(r.headers["location"])
    assert loc.path == "/auth/callback"
    q = {k: v[0] for k, v in parse_qs(loc.query).items()}
    assert q["state"] == _SPA_STATE and "access_token" not in r.headers["location"]
    # The IdP got the server-held verifier matching the challenge it saw.
    assert _form(seen[0])["code"] == "idp-code" and len(_form(seen[0])["code_verifier"]) >= 43
    # Binding cookie is cleared.
    assert 'renfield_oauth_bind=""' in r.headers["set-cookie"] or "Max-Age=0" in r.headers["set-cookie"]

    # The SPA redeems the hand-off with ITS verifier.
    ex = await async_client.post("/api/auth/sso/exchange", json={
        "code": q["code"], "code_verifier": _SPA_VERIFIER, "state": _SPA_STATE})
    assert ex.status_code == 200, ex.text
    from services.auth_service import decode_token
    assert decode_token(ex.json()["access_token"])["sub"] == "1"

    # Reusing the server state: refused.
    again = await async_client.get("/api/auth/sso/callback/google", params={"code": "idp-code", "state": state})
    assert again.status_code == 400


async def _assert_opaque(resp):
    assert resp.status_code == 400
    assert resp.json() == {"detail": "Invalid or expired authorization request"}


async def test_callback_failures_are_one_opaque_400(async_client, db_session, google_on, fake_redis, monkeypatch):
    await ensure_user(db_session, 1, username="ada")
    await db_session.commit()
    await _resolving_consumer(1)
    idp = {"resp": httpx.Response(200, json={"id_token": _id_token()})}
    _mock_idp(monkeypatch, lambda r: idp["resp"])
    cb = "/api/auth/sso/callback/google"

    # missing state
    await _assert_opaque(await async_client.get(cb, params={"code": "c"}))
    # foreign state: another browser's (no binding cookie on this request)
    state = await _started(async_client)
    async_client.cookies.clear()
    await _assert_opaque(await async_client.get(cb, params={"code": "c", "state": state}))
    # expired state
    state = await _started(async_client)
    fake_redis.now += settings.oauth_state_ttl_seconds + 1
    await _assert_opaque(await async_client.get(cb, params={"code": "c", "state": state}))
    # IdP error / no code
    state = await _started(async_client)
    await _assert_opaque(await async_client.get(cb, params={"error": "access_denied", "state": state}))
    # wrong code_verifier: the IdP refuses the exchange
    idp["resp"] = httpx.Response(400, json={"error": "invalid_grant"})
    state = await _started(async_client)
    await _assert_opaque(await async_client.get(cb, params={"code": "c", "state": state}))
    # identity no consumer resolves
    idp["resp"] = httpx.Response(200, json={"id_token": _id_token(sub="stranger")})
    state = await _started(async_client)
    await _assert_opaque(await async_client.get(cb, params={"code": "c", "state": state}))


async def test_callback_refuses_inactive_account(async_client, db_session, google_on, fake_redis, monkeypatch):
    user = await ensure_user(db_session, 1, username="ada")
    user.is_active = False
    await db_session.commit()
    await _resolving_consumer(1)
    _mock_idp(monkeypatch, lambda r: httpx.Response(200, json={"id_token": _id_token()}))
    state = await _started(async_client)
    await _assert_opaque(await async_client.get(
        "/api/auth/sso/callback/google", params={"code": "c", "state": state}))


async def test_callback_never_bypasses_the_voice_second_factor(
    async_client, db_session, google_on, fake_redis, monkeypatch,
):
    from unittest.mock import AsyncMock
    await ensure_user(db_session, 1, username="ada")
    await db_session.commit()
    await _resolving_consumer(1)
    monkeypatch.setattr(
        "services.voice_factor_preconditions.second_factor_applies", AsyncMock(return_value=True))
    _mock_idp(monkeypatch, lambda r: httpx.Response(200, json={"id_token": _id_token()}))
    state = await _started(async_client)
    r = await async_client.get("/api/auth/sso/callback/google", params={"code": "c", "state": state})
    await _assert_opaque(r)
    # No hand-off code was issued.
    assert not any(k.startswith("sso:handoff:") for k in fake_redis.store)


async def test_callback_exchange_raising(
    async_client, db_session, google_on, fake_redis, monkeypatch,
):
    from auth.providers.google import GoogleProvider
    await ensure_user(db_session, 1, username="ada")
    await db_session.commit()
    await _resolving_consumer(1)

    async def raising(self, **kw):
        raise RuntimeError("provider bug")

    monkeypatch.setattr(GoogleProvider, "exchange_code", raising)
    state = await _started(async_client)
    await _assert_opaque(await async_client.get(
        "/api/auth/sso/callback/google", params={"code": "c", "state": state}))


async def test_callback_handoff_not_stored(
    async_client, db_session, google_on, fake_redis, monkeypatch,
):
    from services import sso_handoff_store
    await ensure_user(db_session, 1, username="ada")
    await db_session.commit()
    await _resolving_consumer(1)
    _mock_idp(monkeypatch, lambda r: httpx.Response(200, json={"id_token": _id_token()}))

    async def collide(session):
        raise sso_handoff_store.HandoffCodeCollision("x")

    monkeypatch.setattr(sso_handoff_store, "issue_handoff_code", collide)
    state = await _started(async_client)
    await _assert_opaque(await async_client.get(
        "/api/auth/sso/callback/google", params={"code": "c", "state": state}))


async def test_apple_form_post_callback(async_client, db_session, apple_on, fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "sso_handoff_enabled", True)
    await ensure_user(db_session, 1, username="ada")
    await db_session.commit()

    async def consumer(*, result):
        return 1 if result.provider_id == "apple" else None

    register_hook("post_authenticate", consumer)
    token = jose_jwt.encode({
        "iss": "https://appleid.apple.com", "aud": "com.example.web", "sub": "a.1",
        "exp": int(time.time()) + 300}, "x", algorithm="HS256")
    _mock_idp(monkeypatch, lambda r: httpx.Response(200, json={"id_token": token}))

    r = await _start(async_client, provider="apple")
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "samesite=none" in cookie and "secure" in cookie  # survives the cross-site POST
    state = _authorize_query(r)["state"]
    # httpx' test client does not send Secure cookies over http — attach it by hand.
    bind = async_client.cookies.get("renfield_oauth_bind") or r.cookies.get("renfield_oauth_bind")
    assert bind
    cb = await async_client.post(
        "/api/auth/sso/callback/apple", data={"code": "c", "state": state},
        headers={"Cookie": f"renfield_oauth_bind={bind}"})
    assert cb.status_code == 303, cb.text
    assert urlparse(cb.headers["location"]).path == "/auth/callback"


async def test_callback_is_csrf_exempt_under_cookie_auth(monkeypatch):
    """The IdP's cross-site POST cannot carry X-CSRF-Token; the route binds
    itself through state + binding cookie."""
    from main import CSRFMiddleware
    assert any(p == "/api/auth/sso/callback/" for p in CSRFMiddleware._EXEMPT_PREFIXES)


def test_authorization_code_is_redacted_from_access_logs():
    from utils.log_safety import SECRET_QUERY_PARAMS
    assert "code" in SECRET_QUERY_PARAMS


# ---------------------------------------------------------------------------
# Config validator
# ---------------------------------------------------------------------------


def _settings(**kw):
    from utils.config import Settings
    return Settings(_env_file=None, **kw)


def test_enabled_provider_requires_the_handoff():
    with pytest.raises(ValueError, match="SSO_HANDOFF_ENABLED"):
        _settings(oauth_github_enabled=True)
    with pytest.raises(ValueError, match="OAUTH_STATE_TTL_SECONDS"):
        _settings(oauth_github_enabled=True, sso_handoff_enabled=True, oauth_state_ttl_seconds=0)
    assert _settings(oauth_google_enabled=True, sso_handoff_enabled=True).oauth_google_enabled
    assert _settings().oauth_state_ttl_seconds == 600
