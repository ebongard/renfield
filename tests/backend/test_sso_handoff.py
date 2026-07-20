"""Tests for the SSO one-time hand-off code exchange (token-in-URL replacement).

Covers the code store (issue + atomic single-use consume), the PKCE S256 check,
and the POST /api/auth/sso/exchange endpoint (flag gate, happy path, replay,
wrong verifier, wrong state). Uses a tiny in-memory async Redis fake so the
single-use GETDEL semantics are exercised for real (no fakeredis dep).

See docs/design/sso-token-handoff-hardening.md.
"""
from __future__ import annotations

import pytest

from models.database import User
from utils.config import settings

pytestmark = [pytest.mark.asyncio]


class _FakeRedis:
    """Minimal async Redis honoring set(ex, nx) + getdel (delete-on-read)."""
    def __init__(self):
        self.store: dict[str, str] = {}

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def getdel(self, key):
        return self.store.pop(key, None)


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr("services.sso_handoff_store.get_redis", lambda: fake)
    return fake


# A valid PKCE pair (verifier 43-128 chars; challenge = S256(verifier)).
_VERIFIER = "a" * 64


def _challenge_for(verifier: str) -> str:
    from services.sso_handoff_store import s256_challenge
    return s256_challenge(verifier)


async def _seed_user(db) -> User:
    user = User(id=1, username="ssouser", password_hash="x", is_active=True, role_id=1)
    db.add(user)
    await db.commit()
    return user


def test_pkce_s256_roundtrip_and_rejections():
    from services.sso_handoff_store import s256_challenge, verify_pkce_s256
    ch = s256_challenge(_VERIFIER)
    assert verify_pkce_s256(_VERIFIER, ch) is True
    assert verify_pkce_s256("b" * 64, ch) is False          # wrong verifier
    assert verify_pkce_s256("short", ch) is False            # < 43 chars → rejected
    assert verify_pkce_s256(_VERIFIER, "") is False          # no challenge
    # Non-ASCII verifier of a valid LENGTH must be rejected as False (not blow up
    # on encode("ascii") → 500). RFC 7636 restricts the charset to unreserved.
    assert verify_pkce_s256("ä" * 64, ch) is False
    assert verify_pkce_s256("a" * 42 + "!", ch) is False     # invalid char


async def test_store_is_single_use(fake_redis, monkeypatch):
    from services.sso_handoff_store import (
        HandoffSession, consume_handoff_code, issue_handoff_code,
    )
    monkeypatch.setattr(settings, "sso_handoff_ttl_seconds", 60)
    sess = HandoffSession(
        user_id=1, code_challenge=_challenge_for(_VERIFIER), state="st", provider="entra",
    )
    code = await issue_handoff_code(sess)
    assert code
    got = await consume_handoff_code(code)
    assert got is not None and got.user_id == 1 and got.state == "st"
    # Second consume finds nothing — single use (atomic GETDEL).
    assert await consume_handoff_code(code) is None
    assert await consume_handoff_code("never-issued") is None


async def _issue(db, *, state="st", challenge=None, user_id=1):
    from services.sso_handoff_store import HandoffSession, issue_handoff_code
    return await issue_handoff_code(HandoffSession(
        user_id=user_id, code_challenge=challenge or _challenge_for(_VERIFIER),
        state=state, provider="entra",
    ))


async def test_exchange_404_when_flag_off(async_client, monkeypatch):
    monkeypatch.setattr(settings, "sso_handoff_enabled", False)
    r = await async_client.post("/api/auth/sso/exchange", json={
        "code": "x", "code_verifier": _VERIFIER, "state": "st"})
    assert r.status_code == 404


async def test_exchange_happy_path(async_client, db_session, fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "sso_handoff_enabled", True)
    await _seed_user(db_session)
    code = await _issue(db_session)
    r = await async_client.post("/api/auth/sso/exchange", json={
        "code": code, "code_verifier": _VERIFIER, "state": "st"})
    assert r.status_code == 200, r.text
    body = r.json()
    # Tokens are minted fresh at exchange time (never stored in Redis) — decode
    # the access token and confirm it's for the resolved user.
    from services.auth_service import decode_token
    payload = decode_token(body["access_token"])
    assert payload and payload["sub"] == "1"
    assert body["refresh_token"] and body["token_type"] == "bearer"
    assert body["expires_in"] == settings.access_token_expire_minutes * 60


async def test_exchange_inactive_user_is_rejected(async_client, db_session, fake_redis, monkeypatch):
    """Re-validation at exchange time: a user deactivated after the code was
    issued cannot exchange it (opaque 400)."""
    monkeypatch.setattr(settings, "sso_handoff_enabled", True)
    db_session.add(User(id=1, username="off", password_hash="x", is_active=False, role_id=1))
    await db_session.commit()
    code = await _issue(db_session)
    assert (await async_client.post("/api/auth/sso/exchange", json={
        "code": code, "code_verifier": _VERIFIER, "state": "st"})).status_code == 400


async def test_exchange_non_ascii_verifier_is_400_not_500(async_client, db_session, fake_redis, monkeypatch):
    """A non-ASCII verifier of valid length is a clean opaque 400, never a 500."""
    monkeypatch.setattr(settings, "sso_handoff_enabled", True)
    await _seed_user(db_session)
    code = await _issue(db_session)
    assert (await async_client.post("/api/auth/sso/exchange", json={
        "code": code, "code_verifier": "ä" * 64, "state": "st"})).status_code == 400


async def test_exchange_replay_is_rejected(async_client, db_session, fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "sso_handoff_enabled", True)
    await _seed_user(db_session)
    code = await _issue(db_session)
    assert (await async_client.post("/api/auth/sso/exchange", json={
        "code": code, "code_verifier": _VERIFIER, "state": "st"})).status_code == 200
    # Same code again → 400 (single use).
    assert (await async_client.post("/api/auth/sso/exchange", json={
        "code": code, "code_verifier": _VERIFIER, "state": "st"})).status_code == 400


async def test_exchange_wrong_verifier_burns_code(async_client, db_session, fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "sso_handoff_enabled", True)
    await _seed_user(db_session)
    code = await _issue(db_session)
    # Wrong verifier → 400 ...
    assert (await async_client.post("/api/auth/sso/exchange", json={
        "code": code, "code_verifier": "z" * 64, "state": "st"})).status_code == 400
    # ... and the code is already consumed, so even the RIGHT verifier now fails.
    assert (await async_client.post("/api/auth/sso/exchange", json={
        "code": code, "code_verifier": _VERIFIER, "state": "st"})).status_code == 400


async def test_exchange_wrong_state_is_rejected(async_client, db_session, fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "sso_handoff_enabled", True)
    await _seed_user(db_session)
    code = await _issue(db_session, state="the-real-state")
    assert (await async_client.post("/api/auth/sso/exchange", json={
        "code": code, "code_verifier": _VERIFIER, "state": "attacker-state"})).status_code == 400
