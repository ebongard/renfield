"""Tests for api/routes/internal_auth.py (voice-server verify callback).

``POST /api/internal/auth/verify`` is voice-server's callback target in
``callback``/``registry`` auth mode (voice-server PR #987). Voice-server's
behaviour is fixed (``voice_server/auth.py::_verify_via``), so these tests
pin the contract:

- 200 + payload containing ``user_id`` on accept; ``jti`` and ``exp``
  stripped.
- 401 + opaque ``"unauthorized"`` detail on every rejection branch
  (signature, type, blacklist, missing/non-numeric/unknown/inactive user).
- Service-account tokens (``sub="service:whisper"``) accepted without a
  DB lookup.

The route imports its collaborators lazily at call time, so the tests
swap ``services.{auth_service, token_blacklist, database}`` in
``sys.modules`` — no Redis/DB needed, and it works whether or not the
real modules were already imported by other tests.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient


class _SessionStub:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


def _install_service_stubs(
    monkeypatch,
    *,
    decode_return,
    blacklisted=False,
    user="__default__",
):
    """``user``: ``"__default__"`` → active user; ``None`` → row missing;
    any object with ``is_active`` → that state."""
    if user == "__default__":
        user = SimpleNamespace(id=42, is_active=True)

    auth_module = types.ModuleType("services.auth_service")
    auth_module.decode_token = lambda token: decode_return  # noqa: ARG005

    async def _get_user_by_id(_db, _user_id):
        return user

    auth_module.get_user_by_id = _get_user_by_id

    blacklist_module = types.ModuleType("services.token_blacklist")

    class _Blacklist:
        async def is_blacklisted(self, _jti):
            return blacklisted

    blacklist_module.token_blacklist = _Blacklist()

    db_module = types.ModuleType("services.database")
    db_module.AsyncSessionLocal = _SessionStub

    services_pkg = sys.modules.get("services") or types.ModuleType("services")
    monkeypatch.setitem(sys.modules, "services", services_pkg)
    monkeypatch.setitem(sys.modules, "services.auth_service", auth_module)
    monkeypatch.setitem(sys.modules, "services.token_blacklist", blacklist_module)
    monkeypatch.setitem(sys.modules, "services.database", db_module)


def _client() -> TestClient:
    from api.routes.internal_auth import router

    app = FastAPI()
    app.include_router(router, prefix="/api/internal")
    return TestClient(app)


def test_verify_accepts_valid_access_token(monkeypatch):
    _install_service_stubs(
        monkeypatch,
        decode_return={
            "sub": "42",
            "username": "alice",
            "type": "access",
            "jti": "abc",
            "exp": 9999999999,
        },
    )
    resp = _client().post("/api/internal/auth/verify", json={"token": "x"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == "42"
    assert body["username"] == "alice"
    # exp + jti stripped — jti is the revocation handle and must not leak
    # past the boundary.
    assert "exp" not in body
    assert "jti" not in body


def test_verify_rejects_invalid_signature(monkeypatch):
    _install_service_stubs(monkeypatch, decode_return=None)
    resp = _client().post("/api/internal/auth/verify", json={"token": "x"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "unauthorized"


def test_verify_rejects_refresh_token(monkeypatch):
    _install_service_stubs(
        monkeypatch,
        decode_return={"sub": "42", "type": "refresh", "jti": "abc"},
    )
    resp = _client().post("/api/internal/auth/verify", json={"token": "x"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "unauthorized"


def test_verify_rejects_blacklisted_jti(monkeypatch):
    _install_service_stubs(
        monkeypatch,
        decode_return={"sub": "42", "type": "access", "jti": "revoked"},
        blacklisted=True,
    )
    resp = _client().post("/api/internal/auth/verify", json={"token": "x"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "unauthorized"


def test_verify_rejects_missing_sub(monkeypatch):
    _install_service_stubs(
        monkeypatch,
        decode_return={"type": "access", "jti": "abc"},
    )
    resp = _client().post("/api/internal/auth/verify", json={"token": "x"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "unauthorized"


def test_verify_rejects_unknown_user(monkeypatch):
    """JWT structurally valid but the User row is gone (deleted)."""
    _install_service_stubs(
        monkeypatch,
        decode_return={"sub": "42", "type": "access", "jti": "abc"},
        user=None,
    )
    resp = _client().post("/api/internal/auth/verify", json={"token": "x"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "unauthorized"


def test_verify_rejects_inactive_user(monkeypatch):
    """User row exists but admin set is_active=False."""
    _install_service_stubs(
        monkeypatch,
        decode_return={"sub": "42", "type": "access", "jti": "abc"},
        user=SimpleNamespace(id=42, is_active=False),
    )
    resp = _client().post("/api/internal/auth/verify", json={"token": "x"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "unauthorized"


def test_verify_rejects_non_numeric_sub(monkeypatch):
    """Non-numeric, non-service sub can't reach a User row — mirrors
    websocket_auth.py's strictness."""
    _install_service_stubs(
        monkeypatch,
        decode_return={"sub": "not-an-int", "type": "access", "jti": "abc"},
    )
    resp = _client().post("/api/internal/auth/verify", json={"token": "x"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "unauthorized"


def test_verify_accepts_service_token(monkeypatch):
    """service:* tokens (whisper/piper/meeting-worker) verify without a DB
    lookup — they have no User row."""
    _install_service_stubs(
        monkeypatch,
        decode_return={
            "sub": "service:whisper",
            "scope": "voice",
            "type": "access",
            "jti": "svc-abc",
        },
        user=None,  # proves the user-lookup branch is skipped
    )
    resp = _client().post("/api/internal/auth/verify", json={"token": "x"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == "service:whisper"
    assert body["scope"] == "voice"
    assert "jti" not in body


# --- Shared-secret gate (login-audit finding) ----------------------------


def _set_verify_secret(monkeypatch, value):
    """Set (or clear) settings.internal_auth_verify_secret for the route's
    module-level `settings` reference."""
    from pydantic import SecretStr

    import api.routes.internal_auth as mod
    monkeypatch.setattr(
        mod.settings, "internal_auth_verify_secret",
        (SecretStr(value) if value is not None else None),
        raising=False,
    )


def test_verify_secret_unset_is_backward_compatible(monkeypatch):
    """No configured secret → endpoint works without any header (legacy)."""
    _set_verify_secret(monkeypatch, None)
    _install_service_stubs(
        monkeypatch,
        decode_return={"sub": "42", "username": "alice", "type": "access", "jti": "a", "exp": 9999999999},
    )
    resp = _client().post("/api/internal/auth/verify", json={"token": "x"})
    assert resp.status_code == 200


def test_verify_secret_missing_header_rejected(monkeypatch):
    """Configured secret + no header → 401. The decode stub raises if reached,
    proving the gate fires BEFORE any token work (no validity oracle)."""
    _set_verify_secret(monkeypatch, "s3cr3t")

    def _boom(_t):
        raise AssertionError("token decode must not run when the secret gate fails")

    _install_service_stubs(monkeypatch, decode_return={"sub": "42", "type": "access"})
    import api.routes.internal_auth as _  # noqa: F401
    # Override the stubbed decode with one that fails if the gate is skipped.
    sys.modules["services.auth_service"].decode_token = _boom

    resp = _client().post("/api/internal/auth/verify", json={"token": "x"})
    assert resp.status_code == 401


def test_verify_secret_wrong_header_rejected(monkeypatch):
    _set_verify_secret(monkeypatch, "s3cr3t")
    _install_service_stubs(
        monkeypatch,
        decode_return={"sub": "42", "type": "access", "jti": "a", "exp": 9999999999},
    )
    resp = _client().post(
        "/api/internal/auth/verify", json={"token": "x"},
        headers={"X-Verify-Secret": "wrong"},
    )
    assert resp.status_code == 401


def test_verify_secret_correct_header_accepted(monkeypatch):
    _set_verify_secret(monkeypatch, "s3cr3t")
    _install_service_stubs(
        monkeypatch,
        decode_return={"sub": "42", "username": "alice", "type": "access", "jti": "a", "exp": 9999999999},
    )
    resp = _client().post(
        "/api/internal/auth/verify", json={"token": "x"},
        headers={"X-Verify-Secret": "s3cr3t"},
    )
    assert resp.status_code == 200
    assert resp.json()["user_id"] == "42"
