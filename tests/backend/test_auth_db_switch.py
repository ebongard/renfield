"""DB_AUTH_ENABLED — the local password provider can be switched off (#1345).

Covers: default unchanged (db on, walk unchanged) · db off + LDAP on → a valid
local password is rejected · the config validator refuses db off without a
fully configured LDAP provider · ``ldap://`` is refused (config + provider
guard) · the startup check ``assert_login_possible`` (no identity consumer →
abort). The default-admin behaviour with db off is in
``test_ensure_admin_bootstrap_pg.py`` (real Postgres).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from auth.providers.base import make_result
from auth.registry import ProviderRegistry, assert_login_possible, set_registry
from utils.config import Settings, settings
from utils.hooks import clear_hooks, register_hook

_LDAP_OK = dict(
    ldap_auth_enabled=True,
    ldap_url="ldaps://dir.example:636",
    ldap_auth_user_base_dn="ou=people,dc=example",
)


@pytest.fixture(autouse=True)
def _isolate():
    clear_hooks()
    yield
    clear_hooks()
    set_registry(None)


class _DecliningLDAP:
    """LDAP stand-in that knows nobody (directory says: wrong password)."""

    provider_id = "ldap"
    supported_channels = frozenset({"web"})
    priority = 50
    enabled = True

    def __init__(self):
        self.calls = 0

    async def authenticate(self, *, username, password, channel):
        self.calls += 1
        return None


@asynccontextmanager
async def _fake_session():
    yield MagicMock()


def _local_user():
    return MagicMock(id=7, email=None, first_name=None, last_name=None, username="ada")


# ---------------------------------------------------------------------------
# Settings validator
# ---------------------------------------------------------------------------

class TestCredentialProviderConfig:
    @pytest.mark.unit
    def test_default_is_db_on(self):
        s = Settings(_env_file=None)
        assert s.db_auth_enabled is True
        assert s.ldap_auth_enabled is False

    @pytest.mark.unit
    def test_db_off_without_ldap_refuses_boot(self):
        with pytest.raises(ValueError, match=r"DB_AUTH_ENABLED=false.*LDAP_AUTH_ENABLED=true"):
            Settings(_env_file=None, db_auth_enabled=False)

    @pytest.mark.unit
    @pytest.mark.parametrize("missing", ["ldap_url", "ldap_auth_user_base_dn"])
    def test_db_off_with_incomplete_ldap_refuses_boot(self, missing):
        cfg = {**_LDAP_OK, missing: "  "}
        with pytest.raises(ValueError, match=missing.upper()):
            Settings(_env_file=None, db_auth_enabled=False, **cfg)

    @pytest.mark.unit
    def test_db_off_with_complete_ldap_constructs(self):
        s = Settings(_env_file=None, db_auth_enabled=False, **_LDAP_OK)
        assert s.db_auth_enabled is False

    @pytest.mark.unit
    @pytest.mark.parametrize("url", ["ldap://dir.example:389", "LDAP://dir.example", "ldapi:///run/sock"])
    def test_ldap_enabled_with_non_tls_url_refuses_boot(self, url):
        with pytest.raises(ValueError, match="only ldaps://"):
            Settings(_env_file=None, **{**_LDAP_OK, "ldap_url": url})

    @pytest.mark.unit
    def test_ldaps_url_any_case_constructs(self):
        s = Settings(_env_file=None, **{**_LDAP_OK, "ldap_url": "LDAPS://dir.example:636"})
        assert s.ldap_auth_enabled is True

    @pytest.mark.unit
    def test_ldap_url_ignored_while_ldap_off(self):
        # A leftover ldap:// value carries no credentials while LDAP is off.
        s = Settings(_env_file=None, ldap_url="ldap://dir.example:389")
        assert s.ldap_auth_enabled is False


# ---------------------------------------------------------------------------
# DBProvider gate + the login walk
# ---------------------------------------------------------------------------

class TestDBProviderSwitch:
    @pytest.mark.unit
    def test_enabled_follows_setting(self, monkeypatch):
        from auth.providers.db import DBProvider

        assert DBProvider().enabled is True
        monkeypatch.setattr(settings, "db_auth_enabled", False)
        assert DBProvider().enabled is False

    @pytest.mark.unit
    async def test_authenticate_declines_without_checking_password_when_off(self, monkeypatch):
        from auth.providers import db as dbmod

        check = AsyncMock(return_value=_local_user())
        monkeypatch.setattr(dbmod, "AsyncSessionLocal", _fake_session)
        monkeypatch.setattr(dbmod, "authenticate_user", check)
        monkeypatch.setattr(settings, "db_auth_enabled", False)

        assert await dbmod.DBProvider().authenticate(
            username="ada", password="right", channel="web") is None
        check.assert_not_called()

    @pytest.mark.unit
    async def test_db_off_ldap_on_valid_local_password_is_rejected(self, monkeypatch):
        """The authority property: LDAP declines, the (valid) local password
        must NOT log in — resolve_login answers None (→ 401)."""
        from auth.login_flow import resolve_login
        from auth.providers import db as dbmod

        check = AsyncMock(return_value=_local_user())
        monkeypatch.setattr(dbmod, "AsyncSessionLocal", _fake_session)
        monkeypatch.setattr(dbmod, "authenticate_user", check)
        monkeypatch.setattr(settings, "db_auth_enabled", False)
        ldap = _DecliningLDAP()
        reg = ProviderRegistry()
        reg.register(dbmod.DBProvider())
        reg.register(ldap)
        set_registry(reg)

        assert await resolve_login(db=MagicMock(), username="ada", password="right") is None
        assert ldap.calls == 1
        check.assert_not_called()
        assert [p.provider_id for p in reg.credential_providers("web")] == ["ldap"]

    @pytest.mark.unit
    async def test_db_on_default_walk_unchanged(self, monkeypatch):
        """Flag default: LDAP declines, the local password logs in as before."""
        from auth.login_flow import resolve_login
        from auth.providers import db as dbmod

        monkeypatch.setattr(dbmod, "AsyncSessionLocal", _fake_session)
        monkeypatch.setattr(dbmod, "authenticate_user", AsyncMock(return_value=_local_user()))
        reg = ProviderRegistry()
        reg.register(dbmod.DBProvider())
        reg.register(_DecliningLDAP())
        set_registry(reg)

        out = await resolve_login(db=MagicMock(), username="ada", password="right")
        assert out is not None
        assert (out.user_id, out.provider_id) == (7, "db")
        assert [p.provider_id for p in reg.credential_providers("web")] == ["ldap", "db"]

    @pytest.mark.unit
    async def test_db_off_ldap_success_resolved_by_consumer(self, monkeypatch):
        """With db off, an LDAP login works through the identity consumer."""
        from auth.login_flow import resolve_login

        monkeypatch.setattr(settings, "db_auth_enabled", False)

        class _OkLDAP(_DecliningLDAP):
            async def authenticate(self, *, username, password, channel):
                return make_result(provider_id="ldap", subject="uuid-1",
                                   display_name="Ada", channel=channel)

        reg = ProviderRegistry()
        reg.register(_OkLDAP())
        set_registry(reg)

        async def _consumer(*, result):
            return 99 if result.provider_id == "ldap" else None

        register_hook("post_authenticate", _consumer)
        out = await resolve_login(db=MagicMock(), username="ada", password="dir-pw")
        assert out is not None
        assert (out.user_id, out.provider_id) == (99, "ldap")


# ---------------------------------------------------------------------------
# LDAP provider transport guard
# ---------------------------------------------------------------------------

class TestLDAPTransportGuard:
    @pytest.mark.unit
    @pytest.mark.parametrize("url", ["ldap://dir.example:389", "ldapi:///sock"])
    async def test_plain_url_declines_before_any_connection(self, monkeypatch, url):
        """A settings object mutated past the validator still never binds in
        clear text: no Server/Connection is constructed."""
        import sys

        from auth.providers import ldap as lm

        for k, v in {**_LDAP_OK, "ldap_url": url}.items():
            monkeypatch.setattr(settings, k, v)
        monkeypatch.setattr(settings, "ldap_bind_password", SecretStr("svc"))
        fake = MagicMock()
        for m, obj in (("ldap3", fake), ("ldap3.core", fake.core),
                       ("ldap3.core.exceptions", fake.core.exceptions)):
            monkeypatch.setitem(sys.modules, m, obj)

        assert await lm.LDAPProvider().authenticate(
            username="ada", password="pw", channel="web") is None
        fake.Server.assert_not_called()
        fake.Connection.assert_not_called()


# ---------------------------------------------------------------------------
# Startup check after the plugins are loaded
# ---------------------------------------------------------------------------

class TestAssertLoginPossible:
    @pytest.mark.unit
    def test_db_on_never_raises(self):
        assert_login_possible()

    @pytest.mark.unit
    def test_db_off_without_identity_consumer_raises(self, monkeypatch):
        monkeypatch.setattr(settings, "db_auth_enabled", False)
        with pytest.raises(RuntimeError, match="nobody could log in"):
            assert_login_possible()

    @pytest.mark.unit
    @pytest.mark.parametrize("event", ["post_authenticate", "authenticate"])
    def test_db_off_with_consumer_passes(self, monkeypatch, event):
        monkeypatch.setattr(settings, "db_auth_enabled", False)

        async def _handler(**_kw):
            return None

        register_hook(event, _handler)
        assert_login_possible()
