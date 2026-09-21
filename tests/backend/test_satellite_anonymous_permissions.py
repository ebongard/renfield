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
from models.permissions import Permission, has_mcp_permission, has_permission

# The set D-4a recommends (widened by review): a POSITIVE list over every MCP
# server — house control and the harmless reads yes, household content and
# write paths no. Anything not named is denied, read-only servers included.
RECOMMENDED = (
    "mcp.homeassistant,mcp.dlna,mcp.radio,mcp.jellyfin,mcp.weather,"
    "mcp.search,mcp.news,rooms.read,ha.control"
)
GRANTED_SERVERS = ("homeassistant", "dlna", "radio", "jellyfin", "weather", "search", "news")
DENIED_SERVERS = ("paperless", "files", "email", "calendar", "scanner", "n8n", "samsung")


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
        for server in GRANTED_SERVERS:
            assert has_mcp_permission(grants, f"mcp.{server}"), server
        # Announce/broadcast keep working for an unrecognised voice (decided).
        assert has_permission(grants, Permission.HA_CONTROL)

    def test_recommended_set_denies_everything_else(self, cfg):
        cfg(auth=True, perms=RECOMMENDED)
        grants = sh.anonymous_permissions()
        assert not has_mcp_permission(grants, "mcp.*")          # no admin wildcard
        for server in DENIED_SERVERS:
            assert not has_mcp_permission(grants, f"mcp.{server}"), server
        for perm in (Permission.ADMIN, Permission.RAG_MANAGE, Permission.USERS_MANAGE,
                     Permission.SETTINGS_MANAGE, Permission.CAM_FULL, Permission.HA_FULL):
            assert not has_permission(grants, perm), perm

    def test_unknown_grant_is_warned_about_once(self, cfg, monkeypatch):
        # A typo is otherwise a silent house-wide denial. (loguru sink — stdlib
        # caplog never sees these.)
        from loguru import logger as loguru_logger

        monkeypatch.setattr(sh, "_anon_perms_warned", False)
        cfg(auth=True, perms="mcp.homeasistant,ha.controll,mcp.dlna")
        warnings: list[str] = []
        sink = loguru_logger.add(lambda m: warnings.append(str(m)), level="WARNING")
        try:
            assert sh.anonymous_permissions() == [
                "mcp.homeasistant", "ha.controll", "mcp.dlna",
            ]
            second = sh.anonymous_permissions()  # warns ONCE, not per turn
        finally:
            loguru_logger.remove(sink)
        assert second is not None
        assert len(warnings) == 1, warnings
        assert "ha.controll" in warnings[0]
        # `mcp.<anything>` is a convention grant — shape-checked only.
        assert "mcp.homeasistant" not in warnings[0]


@pytest.mark.unit
class TestTheGateItself:
    """Through `_check_tool_permission`, not just the helper: the resolution
    order (tool_permissions → server permissions → convention) decides whether
    the convention grant in the list is even reached."""

    @staticmethod
    def _mgr_with(server: str, **cfg_kwargs):
        from services.mcp_client import MCPManager, MCPServerConfig, MCPServerState, MCPToolInfo

        mgr = MCPManager.__new__(MCPManager)
        state = MCPServerState.__new__(MCPServerState)
        state.config = MCPServerConfig(name=server, **cfg_kwargs)
        mgr._servers = {server: state}
        return mgr, MCPToolInfo(
            server_name=server, original_name="Tool",
            namespaced_name=f"mcp.{server}.Tool", description="",
        )

    def test_granted_server_is_allowed_denied_server_is_not(self, cfg):
        cfg(auth=True, perms=RECOMMENDED)
        grants = sh.anonymous_permissions()
        for server in ("homeassistant", "dlna"):
            mgr, tool = self._mgr_with(server)
            assert mgr._check_tool_permission(tool, grants) is None, server
        for server in ("paperless", "files"):
            mgr, tool = self._mgr_with(server)
            assert mgr._check_tool_permission(tool, grants) is not None, server

    def test_a_non_mcp_permission_in_the_yaml_can_never_be_satisfied(self, cfg):
        """Trap for anyone hardening `config/mcp_servers.yaml`: the gate resolves
        `permissions:` / `tool_permissions:` through `has_mcp_permission`, which
        SKIPS every grant that does not start with `mcp.`. So a stanza naming
        `ha.full` (or any role permission) denies everyone except an `mcp.*`
        wildcard holder — the restriction must be expressed as an `mcp.<server>.<scope>`
        grant instead."""
        cfg(auth=True, perms=RECOMMENDED + ",ha.full")
        grants = sh.anonymous_permissions()
        mgr, tool = self._mgr_with("homeassistant", permissions=["ha.full"])
        assert mgr._check_tool_permission(tool, grants) is not None

    def test_narrowing_a_server_works_through_an_mcp_scoped_grant(self, cfg):
        """The shape that DOES work if HA ever exposes a dangerous tool: map it
        to `mcp.homeassistant.admin`, and give the anonymous voice the narrow
        `mcp.homeassistant.control` instead of the server-wide grant."""
        cfg(auth=True, perms="mcp.homeassistant.control")
        grants = sh.anonymous_permissions()
        mgr, tool = self._mgr_with(
            "homeassistant", tool_permissions={"Tool": "mcp.homeassistant.admin"},
        )
        assert mgr._check_tool_permission(tool, grants) is not None
        mgr, tool = self._mgr_with(
            "homeassistant", tool_permissions={"Tool": "mcp.homeassistant.control"},
        )
        assert mgr._check_tool_permission(tool, grants) is None
        # …whereas the server-wide grant would keep covering both.
        cfg(auth=True, perms="mcp.homeassistant")
        wide = sh.anonymous_permissions()
        mgr, tool = self._mgr_with(
            "homeassistant", tool_permissions={"Tool": "mcp.homeassistant.admin"},
        )
        assert mgr._check_tool_permission(tool, wide) is None

    def test_none_still_means_no_permission_model(self):
        mgr, tool = self._mgr_with("paperless")
        assert mgr._check_tool_permission(tool, None) is None


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

    def test_permission_load_failure_denies_instead_of_falling_back(self):
        # A recognised speaker whose permissions cannot be loaded must not be
        # downgraded to the anonymous set — deny (`[]`), like api/routes/chat.py.
        src = inspect.getsource(sh.satellite_websocket)
        assert "sat_user_permissions = []" in src
        assert src.index("Failed to load satellite user permissions") < src.index("sat_user_permissions = []")

    def test_a_refusal_is_spoken_not_swallowed(self):
        src = inspect.getsource(sh.satellite_websocket)
        # The intent loop stops on a denial instead of trying the next intent
        # (which would end in plain chat answering the refused question) …
        assert 'candidate_result.get("permission_denied")' in src
        # … and the response branch says so.
        assert 'action_result.get("permission_denied")' in src
        assert src.index('candidate_result.get("permission_denied")') < src.index(
            'action_result.get("permission_denied")'
        )

    def test_the_mcp_gate_marks_its_denials(self):
        from services import mcp_client

        src = inspect.getsource(mcp_client.MCPManager.execute_tool)
        assert '"permission_denied": True' in src
