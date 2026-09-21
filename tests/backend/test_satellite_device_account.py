"""An unrecognised satellite voice runs as a DEVICE account, which never remembers (D-4b).

`docs/design/household-auth-on-cutover.md` §6.1 Nr. 3: giving the anonymous turn
an identity is what lets it read household knowledge and act under auth-on — and
that same identity would otherwise collect every voice in the room into one
account's memory and presence history. `users.is_device_account` is the contract;
no code keys on a username.
"""
from __future__ import annotations

import inspect

import pytest

from ha_glue.api.websocket import satellite_handler as sh


@pytest.fixture
def cfg(monkeypatch):
    def _set(*, auth: bool, device: str = "", perms: str = ""):
        monkeypatch.setattr(sh.settings, "auth_enabled", auth)
        monkeypatch.setattr(sh.settings, "satellite_device_account", device)
        monkeypatch.setattr(sh.settings, "satellite_anonymous_permissions", perms)
    return _set


def _account(monkeypatch, value):
    """Stub the DB lookup: value is (user_id, permissions) or None."""
    async def _load():
        return value
    monkeypatch.setattr(sh, "_load_device_account", _load)


@pytest.mark.unit
@pytest.mark.asyncio
class TestResolution:
    async def test_auth_off_changes_nothing(self, cfg, monkeypatch):
        _account(monkeypatch, (42, ["mcp.homeassistant"]))
        cfg(auth=False, device="haushalt", perms="mcp.dlna")
        assert await sh.resolve_anonymous_identity() == (None, None, False)

    async def test_no_device_account_falls_to_the_grant_list(self, cfg):
        cfg(auth=True, device="", perms="mcp.dlna,ha.control")
        assert await sh.resolve_anonymous_identity() == (
            None, ["mcp.dlna", "ha.control"], False,
        )

    async def test_whitespace_device_name_is_unset(self, cfg):
        cfg(auth=True, device="   ", perms="mcp.dlna")
        assert await sh.resolve_anonymous_identity() == (None, ["mcp.dlna"], False)

    async def test_device_account_supplies_identity_and_its_role(self, cfg, monkeypatch):
        # The ROLE is the grant set on this path — the anonymous list is not
        # consulted at all, so the two mechanisms can never half-apply.
        _account(monkeypatch, (42, ["mcp.homeassistant", "ha.control"]))
        cfg(auth=True, device="haushalt", perms="mcp.dlna")
        assert await sh.resolve_anonymous_identity() == (
            42, ["mcp.homeassistant", "ha.control"], True,
        )

    async def test_a_configured_but_unresolvable_account_denies(self, cfg, monkeypatch):
        # Typo in the ConfigMap, user deleted, DB hiccup: naming an account is a
        # statement about who the anonymous turn IS. Falling back to the grant
        # list (or worse, to None) would widen it silently; `[]` refuses out
        # loud — the caller speaks the refusal.
        _account(monkeypatch, None)
        cfg(auth=True, device="tippfehler", perms="mcp.dlna,ha.control")
        assert await sh.resolve_anonymous_identity() == (None, [], False)


@pytest.mark.unit
@pytest.mark.asyncio
class TestTheFlagIsTheContract:
    async def test_a_person_account_is_never_borrowed(self, cfg, monkeypatch):
        """The name in the setting is not enough: a user without the flag is a
        person, and running the room as a person is the exact failure D-4b
        exists to prevent."""
        class _Usr:
            id = 7
            is_device_account = False

            @staticmethod
            def get_permissions():
                return ["mcp.*"]

        monkeypatch.setattr(sh, "_device_account_warned", False)
        monkeypatch.setattr(sh, "AsyncSessionLocal", _session_yielding(_Usr()))
        cfg(auth=True, device="eduard")
        assert await sh._load_device_account() is None

    async def test_the_flagged_account_resolves(self, cfg, monkeypatch):
        class _Usr:
            id = 7
            is_device_account = True

            @staticmethod
            def get_permissions():
                return ["mcp.homeassistant"]

        monkeypatch.setattr(sh, "AsyncSessionLocal", _session_yielding(_Usr()))
        cfg(auth=True, device="haushalt")
        assert await sh._load_device_account() == (7, ["mcp.homeassistant"])

    async def test_an_unknown_name_warns_once(self, cfg, monkeypatch):
        from loguru import logger as loguru_logger

        monkeypatch.setattr(sh, "_device_account_warned", False)
        monkeypatch.setattr(sh, "AsyncSessionLocal", _session_yielding(None))
        cfg(auth=True, device="gibtsnicht")
        warnings: list[str] = []
        sink = loguru_logger.add(lambda m: warnings.append(str(m)), level="WARNING")
        try:
            assert await sh._load_device_account() is None
            assert await sh._load_device_account() is None  # warns ONCE, not per turn
        finally:
            loguru_logger.remove(sink)
        assert len(warnings) == 1, warnings
        assert "gibtsnicht" in warnings[0]


def _session_yielding(scalar_result):
    """Minimal AsyncSessionLocal stub: `select(...)` → scalar_one_or_none()."""
    class _Result:
        @staticmethod
        def scalar_one_or_none():
            return scalar_result

    class _Session:
        async def execute(self, *_a, **_kw):
            return _Result()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    return lambda: _Session()


@pytest.mark.unit
class TestExtractionGate:
    def test_a_device_account_never_extracts(self, monkeypatch):
        called: list[str] = []
        monkeypatch.setattr(
            sh, "spawn_memory_extraction",
            lambda **kw: called.append("memory"),
        )
        monkeypatch.setattr(
            sh, "spawn_post_message_hooks",
            lambda **kw: called.append("hooks"),
        )
        assert sh._spawn_satellite_extraction(
            user_text="wie spät ist es",
            response_text="14 Uhr",
            user_id=42,
            session_id="sess-1",
            lang="de",
            action_success=True,
            is_device_account=True,
        ) is None
        assert called == []

    def test_a_person_still_extracts(self, monkeypatch):
        class _Spawn:
            owns_post_message = True

        called: list[str] = []
        monkeypatch.setattr(
            sh, "spawn_memory_extraction",
            lambda **kw: (called.append("memory"), _Spawn())[1],
        )
        assert sh._spawn_satellite_extraction(
            user_text="ich mag Kaffee",
            response_text="notiert",
            user_id=42,
            session_id="sess-1",
            lang="de",
            action_success=True,
            is_device_account=False,
        ) is not None
        assert called == ["memory"]


@pytest.mark.unit
class TestWiring:
    def test_the_handler_resolves_and_carries_the_flag(self):
        src = inspect.getsource(sh.satellite_websocket)
        # Substitution still only fills a still-unresolved turn …
        assert "if sat_user_permissions is None:" in src
        assert "anon = await resolve_anonymous_identity()" in src
        # … a recognised speaker's own permissions win …
        assert src.index("usr.get_permissions()") < src.index("resolve_anonymous_identity()")
        # … the grants reach the executor …
        assert src.index("resolve_anonymous_identity()") < src.index(
            "user_permissions=sat_user_permissions"
        )
        # … and the flag reaches the extraction gate.
        assert "is_device_account=sat_is_device_account" in src

    def test_presence_is_never_booked_for_a_device(self):
        src = inspect.getsource(sh.satellite_websocket)
        head = src.index("register_voice_presence")
        gate = src.rindex("not sat_is_device_account", 0, head)
        assert "presence_enabled" in src[gate:head]

    def test_a_denied_device_turn_is_spoken_not_swallowed(self):
        # `[]` denies every tool; the refusal path from P0 Nr. 2 is what makes
        # that audible instead of a silently wrong answer.
        src = inspect.getsource(sh.satellite_websocket)
        assert 'action_result.get("permission_denied")' in src


@pytest.mark.unit
class TestMemoryServiceGate:
    def test_both_extraction_write_paths_check_the_flag(self):
        from services import conversation_memory_service as cms

        for fn in (
            cms.ConversationMemoryService._apply_update_v2,
            cms.ConversationMemoryService._apply_delete_v2,
            cms.ConversationMemoryService._extraction_target_owned,
        ):
            assert "_device_account_write_denied" in inspect.getsource(fn), fn.__name__

    def test_the_gate_reads_the_column_not_a_name(self):
        from services import conversation_memory_service as cms

        src = inspect.getsource(
            cms.ConversationMemoryService._device_account_write_denied
        )
        assert "User.is_device_account" in src
        assert "username" not in src
        # Auth off is a single trust domain — unchanged there.
        assert "not settings.auth_enabled" in src
