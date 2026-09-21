"""Anonymous satellite turns run with a grant list, not with fail-open None (D-4a).

`docs/design/household-auth-on-cutover.md` §6.1 Nr. 2: a satellite turn without a
recognised speaker carries `user_permissions=None`, which every MCP and
internal-tool gate reads as "no permission model in effect" (#690) — deliberate
for a single-trust-domain household, wrong under auth-on, where it hands any
voice in the house every tool. `SATELLITE_ANONYMOUS_PERMISSIONS` replaces that
None with a concrete set. Empty setting or auth off → unchanged.
"""
from __future__ import annotations

import inspect

import pytest

from ha_glue.api.websocket import satellite_handler as sh
from models.permissions import has_mcp_permission, has_permission, Permission

# The set D-4a recommends: house control yes, admin/write tools no.
RECOMMENDED = (
    "mcp.homeassistant,mcp.dlna,mcp.radio,mcp.jellyfin,mcp.weather,"
    "rooms.read,ha.control"
)


@pytest.fixture
def cfg(monkeypatch):
    def _set(*, auth: bool, perms: str):
        monkeypatch.setattr(sh.settings, "auth_enabled", auth)
        monkeypatch.setattr(sh.settings, "satellite_anonymous_permissions", perms)
    return _set


@pytest.mark.unit
class TestAnonymousPermissions:
    def test_auth_off_keeps_the_fail_open_none(self, cfg):
        # The household today: one trust domain, spoken commands must work.
        cfg(auth=False, perms=RECOMMENDED)
        assert sh.anonymous_permissions() is None

    def test_unset_keeps_the_fail_open_none(self, cfg):
        cfg(auth=True, perms="")
        assert sh.anonymous_permissions() is None

    def test_whitespace_only_is_unset_not_a_lockout(self, cfg):
        # An empty LIST would deny every tool — that must need an explicit
        # decision, never a stray comma in the ConfigMap.
        cfg(auth=True, perms="  ,   , ")
        assert sh.anonymous_permissions() is None

    def test_list_is_trimmed(self, cfg):
        cfg(auth=True, perms=" mcp.homeassistant , rooms.read ,, ")
        assert sh.anonymous_permissions() == ["mcp.homeassistant", "rooms.read"]

    def test_recommended_set_allows_house_control(self, cfg):
        cfg(auth=True, perms=RECOMMENDED)
        grants = sh.anonymous_permissions()
        # HA's MCP server exposes Assist intents only (HassTurnOn, HassLightSet,
        # HassSetPosition, media, volume …) — the convention grant covers exactly
        # the actuation D-4a wants; there is no service-call escape hatch.
        assert has_mcp_permission(grants, "mcp.homeassistant")
        for server in ("mcp.dlna", "mcp.radio", "mcp.jellyfin", "mcp.weather"):
            assert has_mcp_permission(grants, server)
        # Announce/broadcast keep working for an unrecognised voice (decided).
        assert has_permission(grants, Permission.HA_CONTROL)

    def test_recommended_set_denies_everything_else(self, cfg):
        cfg(auth=True, perms=RECOMMENDED)
        grants = sh.anonymous_permissions()
        assert not has_mcp_permission(grants, "mcp.*")          # no admin wildcard
        for denied in ("mcp.paperless", "mcp.files", "mcp.email", "mcp.calendar",
                       "mcp.scanner", "mcp.n8n", "mcp.samsung", "mcp.search"):
            assert not has_mcp_permission(grants, denied), denied
        for perm in (Permission.ADMIN, Permission.RAG_MANAGE, Permission.USERS_MANAGE,
                     Permission.SETTINGS_MANAGE, Permission.CAM_FULL, Permission.HA_FULL):
            assert not has_permission(grants, perm), perm


@pytest.mark.unit
class TestWiring:
    def test_handler_substitutes_only_when_no_user_was_resolved(self):
        src = inspect.getsource(sh.satellite_websocket)
        assert "if sat_user_permissions is None:" in src
        assert "sat_user_permissions = anonymous_permissions()" in src
        # A recognised speaker's own permissions must win: the substitution sits
        # AFTER the speaker lookup and only fills a still-None value.
        assert src.index("usr.get_permissions()") < src.index("anonymous_permissions()")
        # And it reaches the executor that gates MCP + internal tools.
        assert src.index("anonymous_permissions()") < src.index("user_permissions=sat_user_permissions")
