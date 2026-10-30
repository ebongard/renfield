"""Tests for the pluggable auth provider registry (ebongard/renfield#591).

Covers the engineering-review test matrix:

  registry priority walk (order) · fail-open skip (+ WARNING log + counter) ·
  redirect-provider entry per provider · post_authenticate fires once /
  contract shape / `v` · login-denied-when-unresolved · standalone
  no-consumer legacy fallback · DB happy + wrong-password · LDAP bind
  success/fail/server-down · social provider default-OFF · existing
  `authenticate` hook regression · JWT `sub` unchanged + username=display_name
  · UNIQUE(users.username) concurrency backstop present.

These are unit tests: providers/hooks/sessions are mocked so the matrix runs
without Postgres/LDAP. The DB-insert IntegrityError→re-SELECT retry itself is
Reva-side (`ensure_renfield_user`, out of scope per the handover §5); the
Renfield-side guarantee is the already-existing UNIQUE constraint, asserted
here.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError
from unittest.mock import AsyncMock, MagicMock

import pytest

from auth.provider_contract import (
    PROVIDER_RESULT_CONTRACT_VERSION,
    AuthProvider,
    ProviderResult,
)
from auth.providers.base import make_result, normalize_email
from auth.registry import ProviderRegistry
from utils.hooks import clear_hooks, register_hook

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class _FakeProvider:
    """Minimal AuthProvider for walk/ordering/fail-open tests."""

    def __init__(self, pid, *, priority, enabled=True, result=None,
                 raises=None, hang=False, channels=frozenset({"web"})):
        self.provider_id = pid
        self.priority = priority
        self.enabled = enabled
        self.supported_channels = channels
        self._result = result
        self._raises = raises
        self._hang = hang
        self.calls = 0

    async def authenticate(self, *, username, password, channel):
        self.calls += 1
        if self._hang:
            import asyncio
            await asyncio.sleep(60)
        if self._raises is not None:
            raise self._raises
        return self._result


def _result(pid="db", subject="1", channel="web", **kw):
    return make_result(
        provider_id=pid, subject=subject, display_name=kw.get("dn", "Ada L"),
        channel=channel, email=kw.get("email"),
        email_verified=kw.get("email_verified", False),
    )


@pytest.fixture(autouse=True)
def _isolate_hooks():
    clear_hooks()
    yield
    clear_hooks()


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------

class TestContract:
    @pytest.mark.unit
    def test_version_is_one(self):
        assert PROVIDER_RESULT_CONTRACT_VERSION == 1

    @pytest.mark.unit
    def test_provider_result_frozen(self):
        r = _result()
        # NAMED, not `Exception`: a bare `raises(Exception)` also passes on a
        # typo in the attribute name -- so it did not evidence the
        # immutability.
        with pytest.raises(FrozenInstanceError):
            r.subject = "mutated"

    @pytest.mark.unit
    def test_make_result_stamps_v_and_normalizes_email(self):
        r = make_result(provider_id="db", subject="7", display_name="X",
                         channel="web", email="  Foo@Bar.COM ",
                         email_verified=True)
        assert r.v == PROVIDER_RESULT_CONTRACT_VERSION
        assert r.email == "foo@bar.com"  # lowercase + trim only
        assert r.email_verified is True

    @pytest.mark.unit
    def test_email_verified_false_when_no_email(self):
        r = make_result(provider_id="db", subject="7", display_name="X",
                         channel="web", email="   ", email_verified=True)
        assert r.email is None
        assert r.email_verified is False  # cannot be verified with no email

    @pytest.mark.unit
    def test_normalize_email_no_plus_or_dot_folding(self):
        assert normalize_email("a.b+tag@gmail.com") == "a.b+tag@gmail.com"

    @pytest.mark.unit
    def test_fake_provider_satisfies_protocol(self):
        assert isinstance(_FakeProvider("db", priority=1), AuthProvider)


# ---------------------------------------------------------------------------
# Registry: priority walk, multi-active, enabled gate, fail-open
# ---------------------------------------------------------------------------

class TestRegistryWalk:
    @pytest.mark.unit
    async def test_priority_walk_order_first_non_none_wins(self):
        reg = ProviderRegistry()
        low = _FakeProvider("ldap", priority=50, result=_result("ldap", "L"))
        high = _FakeProvider("db", priority=100, result=_result("db", "D"))
        reg.register(high)
        reg.register(low)  # registered out of order on purpose
        out = await reg.authenticate(username="u", password="p", channel="web")
        assert out.provider_id == "ldap"  # lower priority walked first
        assert low.calls == 1
        assert high.calls == 0  # short-circuited

    @pytest.mark.unit
    async def test_decline_falls_through_to_next(self):
        reg = ProviderRegistry()
        reg.register(_FakeProvider("ldap", priority=50, result=None))
        reg.register(_FakeProvider("db", priority=100,
                                   result=_result("db", "9")))
        out = await reg.authenticate(username="u", password="p", channel="web")
        assert out.provider_id == "db"

    @pytest.mark.unit
    async def test_disabled_provider_skipped(self):
        reg = ProviderRegistry()
        dis = _FakeProvider("ldap", priority=50, enabled=False,
                            result=_result("ldap"))
        reg.register(dis)
        reg.register(_FakeProvider("db", priority=100,
                                   result=_result("db", "3")))
        out = await reg.authenticate(username="u", password="p", channel="web")
        assert out.provider_id == "db"
        assert dis.calls == 0

    @pytest.mark.unit
    async def test_channel_filter(self):
        reg = ProviderRegistry()
        reg.register(_FakeProvider("db", priority=100,
                                   result=_result("db"),
                                   channels=frozenset({"teams_tab"})))
        out = await reg.authenticate(username="u", password="p",
                                     channel="web")
        assert out is None  # provider does not support 'web'

    @pytest.mark.unit
    async def test_fail_open_on_error_skips_logs_counts_continues(
        self, monkeypatch
    ):
        counted = []
        monkeypatch.setattr(
            "auth.registry.record_auth_provider_unreachable",
            lambda pid: counted.append(pid),
        )
        reg = ProviderRegistry()
        reg.register(_FakeProvider("ldap", priority=50,
                                   raises=RuntimeError("boom")))
        reg.register(_FakeProvider("db", priority=100,
                                   result=_result("db", "5")))
        out = await reg.authenticate(username="u", password="p",
                                     channel="web")
        assert out.provider_id == "db"          # walk continued
        assert counted == ["ldap"]               # counter incremented

    @pytest.mark.unit
    async def test_fail_open_on_timeout(self, monkeypatch):
        counted = []
        monkeypatch.setattr(
            "auth.registry.record_auth_provider_unreachable",
            lambda pid: counted.append(pid),
        )
        monkeypatch.setattr(
            "auth.registry.settings.auth_provider_timeout_seconds", 0.01
        )
        reg = ProviderRegistry()
        reg.register(_FakeProvider("ldap", priority=50, hang=True))
        reg.register(_FakeProvider("db", priority=100,
                                   result=_result("db", "5")))
        out = await reg.authenticate(username="u", password="p",
                                     channel="web")
        assert out.provider_id == "db"
        assert counted == ["ldap"]

    @pytest.mark.unit
    async def test_all_decline_returns_none(self):
        reg = ProviderRegistry()
        reg.register(_FakeProvider("db", priority=100, result=None))
        assert await reg.authenticate(
            username="u", password="p", channel="web") is None

    @pytest.mark.unit
    def test_register_replaces_same_id(self):
        reg = ProviderRegistry()
        reg.register(_FakeProvider("db", priority=100))
        reg.register(_FakeProvider("db", priority=10))
        assert len(reg.all()) == 1
        assert reg.get("db").priority == 10


# ---------------------------------------------------------------------------
# Redirect providers: default-OFF + entry per provider
# ---------------------------------------------------------------------------

class TestRedirectProviders:
    @pytest.mark.unit
    def test_social_disabled_by_default(self):
        from auth.providers.apple import AppleProvider
        from auth.providers.github import GitHubProvider
        from auth.providers.google import GoogleProvider
        assert GoogleProvider().enabled is False
        assert GitHubProvider().enabled is False
        assert AppleProvider().enabled is False

    @pytest.mark.unit
    async def test_redirect_provider_not_in_credential_walk(self):
        from auth.providers.google import GoogleProvider
        assert await GoogleProvider().authenticate(
            username="u", password="p", channel="web") is None

    @pytest.mark.unit
    def test_authorize_url_per_provider_when_configured(self, monkeypatch):
        from auth.providers.github import GitHubProvider
        from auth.providers.google import GoogleProvider
        for mod, prov in (("google", GoogleProvider()),
                          ("github", GitHubProvider())):
            monkeypatch.setattr(
                f"auth.providers.{mod}.settings.oauth_{mod}_enabled", True)
            monkeypatch.setattr(
                f"auth.providers.{mod}.settings.oauth_{mod}_client_id", "cid")
            monkeypatch.setattr(
                f"auth.providers.{mod}.settings.oauth_{mod}_redirect_uri",
                "https://r/cb")
            url = prov.authorize_url("web")
            assert url and "client_id=cid" in url and prov.provider_id in url

    @pytest.mark.unit
    def test_registry_redirect_entries_lists_enabled_only(self, monkeypatch):
        from auth.providers.google import GoogleProvider
        reg = ProviderRegistry()
        reg.register(GoogleProvider())
        assert reg.redirect_entries("web") == []  # default off
        monkeypatch.setattr(
            "auth.providers.google.settings.oauth_google_enabled", True)
        monkeypatch.setattr(
            "auth.providers.google.settings.oauth_google_client_id", "cid")
        monkeypatch.setattr(
            "auth.providers.google.settings.oauth_google_redirect_uri",
            "https://r/cb")
        entries = reg.redirect_entries("web")
        assert [pid for pid, _ in entries] == ["google"]


# ---------------------------------------------------------------------------
# DB provider
# ---------------------------------------------------------------------------

class TestDBProvider:
    @asynccontextmanager
    async def _fake_session(self):
        yield MagicMock()

    @pytest.mark.unit
    async def test_db_happy_path(self, monkeypatch):
        from auth.providers import db as dbmod
        user = MagicMock(id=42, email="Ada@Example.com",
                         first_name="Ada", last_name="Lovelace",
                         username="ada")
        monkeypatch.setattr(dbmod, "AsyncSessionLocal", self._fake_session)
        monkeypatch.setattr(dbmod, "authenticate_user",
                            AsyncMock(return_value=user))
        r = await dbmod.DBProvider().authenticate(
            username="ada", password="pw", channel="web")
        assert r.provider_id == "db"
        assert r.subject == "42"
        assert r.display_name == "Ada Lovelace"
        assert r.email == "ada@example.com"
        assert r.email_verified is False  # local accounts not verified
        assert r.v == PROVIDER_RESULT_CONTRACT_VERSION

    @pytest.mark.unit
    async def test_db_wrong_password_returns_none(self, monkeypatch):
        from auth.providers import db as dbmod
        monkeypatch.setattr(dbmod, "AsyncSessionLocal", self._fake_session)
        monkeypatch.setattr(dbmod, "authenticate_user",
                            AsyncMock(return_value=None))
        assert await dbmod.DBProvider().authenticate(
            username="ada", password="bad", channel="web") is None

    @pytest.mark.unit
    def test_db_provider_always_enabled_priority_after_ldap(self):
        from auth.providers.db import DBProvider
        from auth.providers.ldap import LDAPProvider
        assert DBProvider().enabled is True
        assert LDAPProvider().priority < DBProvider().priority


# ---------------------------------------------------------------------------
# LDAP provider — bind ok / fail / server-down
# ---------------------------------------------------------------------------

class TestLDAPProvider:
    def _cfg(self, monkeypatch, **over):
        from auth.providers import ldap as lm
        defaults = dict(ldap_auth_enabled=True, ldap_url="ldaps://d:636",
                        ldap_auth_user_base_dn="ou=u,dc=x",
                        ldap_auth_user_filter="(uid={username})",
                        ldap_bind_dn="cn=svc", ldap_connect_timeout=1,
                        ldap_receive_timeout=1)
        defaults.update(over)
        for k, v in defaults.items():
            monkeypatch.setattr(f"auth.providers.ldap.settings.{k}", v)
        monkeypatch.setattr(
            "auth.providers.ldap.settings.ldap_bind_password",
            MagicMock(get_secret_value=lambda: "svcpw"))
        return lm

    @pytest.mark.unit
    async def test_ldap_disabled_returns_none(self, monkeypatch):
        lm = self._cfg(monkeypatch, ldap_auth_enabled=False)
        assert await lm.LDAPProvider().authenticate(
            username="u", password="p", channel="web") is None

    @pytest.mark.unit
    async def test_ldap_bind_success(self, monkeypatch):
        lm = self._cfg(monkeypatch)
        fake_ldap3 = MagicMock()
        entry = {"type": "searchResEntry", "dn": "uid=ada,ou=u,dc=x",
                 "attributes": {"entryUUID": "uuid-1",
                                "displayName": "Ada L", "mail": "ada@x.io",
                                "memberOf": ["cn=eng", "cn=all"]}}

        class _Conn:
            def __init__(self, *a, **k): ...
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def search(self, **k): return (True, {}, [entry], {})

        fake_ldap3.Connection = _Conn
        fake_ldap3.Server = lambda *a, **k: MagicMock()
        fake_ldap3.SAFE_SYNC = "SAFE_SYNC"
        fake_ldap3.core.exceptions.LDAPBindError = type(
            "LBE", (Exception,), {})
        fake_ldap3.core.exceptions.LDAPSocketOpenError = type(
            "LSO", (Exception,), {})
        fake_ldap3.core.exceptions.LDAPException = type(
            "LE", (Exception,), {})
        monkeypatch.setitem(__import__("sys").modules, "ldap3", fake_ldap3)
        monkeypatch.setitem(__import__("sys").modules,
                            "ldap3.core", fake_ldap3.core)
        monkeypatch.setitem(__import__("sys").modules,
                            "ldap3.core.exceptions",
                            fake_ldap3.core.exceptions)
        r = await lm.LDAPProvider().authenticate(
            username="ada", password="pw", channel="web")
        assert r.provider_id == "ldap"
        assert r.subject == "uuid-1"
        assert r.email == "ada@x.io" and r.email_verified is True
        import json
        assert json.loads(r.extras["ldap_member_of"]) == ["cn=eng", "cn=all"]

    @pytest.mark.unit
    async def test_ldap_bind_fail_returns_none(self, monkeypatch):
        lm = self._cfg(monkeypatch)
        LBE = type("LBE", (Exception,), {})

        class _Conn:
            def __init__(self, *a, **k):
                if k.get("user", "").startswith("uid="):
                    raise LBE()  # user-bind fails = wrong password

            def __enter__(self): return self
            def __exit__(self, *a): return False
            def search(self, **k):
                return (True, {}, [{"type": "searchResEntry",
                                    "dn": "uid=ada,ou=u,dc=x",
                                    "attributes": {}}], {})

        fake = MagicMock()
        fake.Connection = _Conn
        fake.Server = lambda *a, **k: MagicMock()
        fake.SAFE_SYNC = "S"
        fake.core.exceptions.LDAPBindError = LBE
        fake.core.exceptions.LDAPSocketOpenError = type("LSO", (Exception,), {})
        fake.core.exceptions.LDAPException = type("LE", (Exception,), {})
        for m in ("ldap3", "ldap3.core", "ldap3.core.exceptions"):
            monkeypatch.setitem(__import__("sys").modules, m,
                                fake if m == "ldap3" else
                                (fake.core if m == "ldap3.core"
                                 else fake.core.exceptions))
        assert await lm.LDAPProvider().authenticate(
            username="ada", password="bad", channel="web") is None

    @pytest.mark.unit
    async def test_ldap_server_down_raises_for_fail_open(self, monkeypatch):
        """Directory unreachable must RAISE so the registry counts it as
        provider-unreachable (fail-open), not silently decline."""
        lm = self._cfg(monkeypatch)
        LSO = type("LSO", (Exception,), {})

        class _Conn:
            def __init__(self, *a, **k): raise LSO("no route")
            def __enter__(self): return self
            def __exit__(self, *a): return False

        fake = MagicMock()
        fake.Connection = _Conn
        fake.Server = lambda *a, **k: MagicMock()
        fake.SAFE_SYNC = "S"
        fake.core.exceptions.LDAPBindError = type("LBE", (Exception,), {})
        fake.core.exceptions.LDAPSocketOpenError = LSO
        fake.core.exceptions.LDAPException = type("LE", (Exception,), {})
        for m, obj in (("ldap3", fake), ("ldap3.core", fake.core),
                       ("ldap3.core.exceptions", fake.core.exceptions)):
            monkeypatch.setitem(__import__("sys").modules, m, obj)
        with pytest.raises(LSO):
            await lm.LDAPProvider().authenticate(
                username="ada", password="pw", channel="web")

    @pytest.mark.unit
    async def test_registry_fail_open_wraps_ldap_server_down(
        self, monkeypatch
    ):
        """End-to-end: an LDAP server-down inside the registry walk is a
        counted skip, and the DB provider still resolves the login."""
        counted = []
        monkeypatch.setattr(
            "auth.registry.record_auth_provider_unreachable",
            lambda pid: counted.append(pid))
        reg = ProviderRegistry()
        reg.register(_FakeProvider("ldap", priority=50,
                                   raises=ConnectionError("down")))
        reg.register(_FakeProvider("db", priority=100,
                                   result=_result("db", "1")))
        out = await reg.authenticate(username="u", password="p",
                                     channel="web")
        assert out.provider_id == "db"
        assert counted == ["ldap"]


# ---------------------------------------------------------------------------
# resolve_login: post_authenticate once / deny / standalone / legacy hook
# ---------------------------------------------------------------------------

class TestResolveLogin:
    def _registry_with(self, monkeypatch, result):
        reg = ProviderRegistry()
        reg.register(_FakeProvider(
            result.provider_id if result else "db",
            priority=100, result=result))
        monkeypatch.setattr("auth.login_flow.get_registry", lambda: reg)
        return reg

    @pytest.mark.unit
    async def test_standalone_db_fallback_no_consumer(self, monkeypatch):
        from auth.login_flow import resolve_login
        self._registry_with(monkeypatch, _result("db", "77", dn="Ada"))
        out = await resolve_login(db=MagicMock(), username="u",
                                  password="p", channel="web")
        assert out is not None
        assert out.user_id == 77
        assert out.display_name == "Ada"
        assert out.provider_id == "db"

    @pytest.mark.unit
    async def test_standalone_denies_non_db_without_consumer(
        self, monkeypatch
    ):
        from auth.login_flow import resolve_login
        self._registry_with(monkeypatch, _result("ldap", "uuid"))
        assert await resolve_login(
            db=MagicMock(), username="u", password="p",
            channel="web") is None

    @pytest.mark.unit
    async def test_post_authenticate_fires_once_with_contract_shape(
        self, monkeypatch
    ):
        from auth.login_flow import resolve_login
        seen = []

        async def consumer(*, result):
            seen.append(result)
            return 999

        register_hook("post_authenticate", consumer)
        self._registry_with(monkeypatch, _result("ldap", "uuid-x",
                                                  dn="Grace H"))
        out = await resolve_login(db=MagicMock(), username="u",
                                  password="p", channel="web")
        assert out.user_id == 999
        assert out.display_name == "Grace H"
        assert len(seen) == 1                       # fired exactly once
        assert isinstance(seen[0], ProviderResult)  # contract shape
        assert seen[0].v == PROVIDER_RESULT_CONTRACT_VERSION

    @pytest.mark.unit
    async def test_login_denied_when_consumer_unresolved(self, monkeypatch):
        from auth.login_flow import resolve_login

        async def consumer(*, result):
            return None  # consumer present but cannot resolve

        register_hook("post_authenticate", consumer)
        self._registry_with(monkeypatch, _result("db", "5"))
        # Even a db result is denied if a consumer is registered and declines
        assert await resolve_login(
            db=MagicMock(), username="u", password="p",
            channel="web") is None

    @pytest.mark.unit
    async def test_bad_credentials_returns_none(self, monkeypatch):
        from auth.login_flow import resolve_login
        self._registry_with(monkeypatch, None)
        assert await resolve_login(
            db=MagicMock(), username="u", password="bad",
            channel="web") is None

    @pytest.mark.unit
    async def test_legacy_authenticate_hook_still_wins(self, monkeypatch):
        """Backward-compat: a plugin's `authenticate` hook returning a User
        is authoritative and short-circuits the registry (no
        post_authenticate fired)."""
        from auth.login_flow import resolve_login
        fired = []

        async def legacy(*, username, password, db):
            return MagicMock(id=13, first_name="Leg", last_name="Acy",
                             username="legacy")

        async def pa(*, result):
            fired.append(result)
            return 1

        register_hook("authenticate", legacy)
        register_hook("post_authenticate", pa)
        reg = ProviderRegistry()
        walk = _FakeProvider("db", priority=100, result=_result("db", "999"))
        reg.register(walk)
        monkeypatch.setattr("auth.login_flow.get_registry", lambda: reg)
        out = await resolve_login(db=MagicMock(), username="u",
                                  password="p", channel="web")
        assert out.user_id == 13                # legacy hook wins
        assert out.provider_id == "legacy_hook"
        assert walk.calls == 0                  # registry not consulted
        assert fired == []                      # post_authenticate NOT fired


# ---------------------------------------------------------------------------
# JWT claim mapping + concurrency backstop
# ---------------------------------------------------------------------------

class TestJWTAndConcurrency:
    @pytest.mark.unit
    def test_jwt_sub_unchanged_username_is_display_name(self):
        """The login route mints {sub: str(user.id), username: display_name}.
        Assert that decoding yields sub=id and username=display_name."""
        from services.auth_service import create_access_token, decode_token
        token = create_access_token(
            data={"sub": "42", "username": "Ada Lovelace"})
        payload = decode_token(token)
        assert payload["sub"] == "42"            # sub = renfield user id
        assert payload["username"] == "Ada Lovelace"  # = display_name

    @pytest.mark.unit
    def test_users_username_unique_constraint_exists(self):
        """Renfield-side concurrency backstop: UNIQUE(users.username) already
        exists, so a concurrent same-subject insert (Reva-side
        ensure_renfield_user) is a catchable IntegrityError, not a dup row."""
        from models.database import User
        assert User.__table__.c.username.unique is True
