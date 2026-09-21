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


@pytest.mark.postgres
@pytest.mark.asyncio
class TestAgainstARealSession:
    """The stubs above cannot see the trap this class exists for:
    `get_permissions()` reads the `role` RELATIONSHIP, so touching it after the
    session closed raises MissingGreenlet — which the `except` in
    `_load_device_account` would turn into a silent total denial of every
    anonymous turn. Only a real async session proves the load happens inside."""

    @staticmethod
    async def _seed(engine, *, is_device: bool):
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from models.database import Role, User

        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as db:
            role = Role(name="geraetekonto", permissions=["mcp.homeassistant"], is_system=False)
            db.add(role)
            await db.flush()
            usr = User(
                username="haushalt",
                password_hash="x",
                role_id=role.id,
                is_device_account=is_device,
            )
            db.add(usr)
            await db.commit()
            return maker, usr.id

    async def test_permissions_are_loaded_before_the_session_closes(
        self, pg_async_engine, monkeypatch, cfg
    ):
        maker, uid = await self._seed(pg_async_engine, is_device=True)
        monkeypatch.setattr(sh, "AsyncSessionLocal", maker)
        cfg(auth=True, device="haushalt", perms="mcp.dlna")
        assert await sh._load_device_account() == (uid, ["mcp.homeassistant"])
        # …and through the whole resolution, so the role — not the anonymous
        # list — is what the turn runs with.
        assert await sh.resolve_anonymous_identity() == (uid, ["mcp.homeassistant"], True)

    async def test_an_unflagged_person_is_refused_against_the_real_row(
        self, pg_async_engine, monkeypatch, cfg
    ):
        maker, _ = await self._seed(pg_async_engine, is_device=False)
        monkeypatch.setattr(sh, "_device_account_warned", False)
        monkeypatch.setattr(sh, "AsyncSessionLocal", maker)
        cfg(auth=True, device="haushalt", perms="mcp.dlna")
        assert await sh.resolve_anonymous_identity() == (None, [], False)

    async def test_why_the_role_must_be_eager_everywhere(self, pg_async_engine):
        """`get_permissions()` reads `User.role`. An async session cannot lazy-
        load, so a plain `select(User)` makes it raise — inside the session too.
        Both permission lookups in this handler (device account AND recognised
        speaker) swallow exceptions, so the failure mode is not a stack trace:
        it is a speaker who silently gets no id and no permissions — no
        presence, no extraction, and under auth-on no rights at all."""
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from models.database import User

        maker, _ = await self._seed(pg_async_engine, is_device=True)
        async with maker() as db:
            usr = (
                await db.execute(select(User).where(User.username == "haushalt"))
            ).scalar_one_or_none()
            with pytest.raises(Exception, match="greenlet"):
                usr.get_permissions()
            usr = (
                await db.execute(
                    select(User)
                    .options(selectinload(User.role))
                    .where(User.username == "haushalt")
                )
            ).scalar_one_or_none()
            assert usr.get_permissions() == ["mcp.homeassistant"]


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

    def test_both_permission_lookups_load_the_role_eagerly(self):
        # The lazy load raises inside the session too (see the Postgres test
        # above) and both call sites swallow it — a recognised speaker would
        # silently lose id and permissions. Pin the eager load in both.
        assert "selectinload(User.role)" in inspect.getsource(sh._load_device_account)
        assert "selectinload(User.role)" in inspect.getsource(sh.satellite_websocket)

    def test_presence_is_never_booked_for_a_device(self):
        src = inspect.getsource(sh.satellite_websocket)
        head = src.index("register_voice_presence")
        gate = src.rindex("not sat_is_device_account", 0, head)
        assert "presence_enabled" in src[gate:head]

    def test_a_recognised_speaker_on_a_device_account_stays_a_device(self):
        # The flag is the contract wherever the identity came from: linking a
        # speaker to the device account must not smuggle extraction and
        # presence back in through the speaker branch.
        src = inspect.getsource(sh.satellite_websocket)
        assert "sat_is_device_account = bool(" in src
        assert src.index("sat_is_device_account = bool(") < src.index(
            "anon = await resolve_anonymous_identity()"
        )


def _service_with_device_flag(flag):
    """ConversationMemoryService whose only DB answer is `flag` (the
    is_device_account scalar, or None for "no such user")."""
    from services.conversation_memory_service import ConversationMemoryService

    class _Result:
        @staticmethod
        def scalar_one_or_none():
            return flag

    class _DB:
        async def execute(self, *_a, **_kw):
            return _Result()

    svc = ConversationMemoryService.__new__(ConversationMemoryService)
    svc.db = _DB()
    return svc


@pytest.mark.unit
@pytest.mark.asyncio
class TestMemoryServiceGate:
    async def test_a_device_identity_may_not_rewrite_an_existing_memory(self, monkeypatch):
        from services import conversation_memory_service as cms

        monkeypatch.setattr(cms.settings, "auth_enabled", True)
        svc = _service_with_device_flag(True)
        assert await svc._device_account_write_denied("UPDATE", 7, 42) is True
        # …and the v1 path therefore stops seeing it as an owner, which makes
        # the contradiction resolver fall back to ADD instead of mutating.
        assert await svc._extraction_target_owned(7, 42) is False

    async def test_a_person_is_untouched(self, monkeypatch):
        from services import conversation_memory_service as cms

        monkeypatch.setattr(cms.settings, "auth_enabled", True)
        svc = _service_with_device_flag(False)
        assert await svc._device_account_write_denied("DELETE", 7, 42) is False

    async def test_a_user_id_pointing_at_no_row_is_not_a_device(self, monkeypatch):
        # Deleted account, stale id: "unknown" must not read as "device".
        from services import conversation_memory_service as cms

        monkeypatch.setattr(cms.settings, "auth_enabled", True)
        svc = _service_with_device_flag(None)
        assert await svc._device_account_write_denied("UPDATE", 7, 999) is False

    async def test_auth_off_asks_the_database_nothing(self, monkeypatch):
        # Single trust domain: no gate, and no extra query per write either.
        from services import conversation_memory_service as cms

        monkeypatch.setattr(cms.settings, "auth_enabled", False)

        class _Boom:
            async def execute(self, *_a, **_kw):
                raise AssertionError("must not query while auth is off")

        svc = _service_with_device_flag(True)
        svc.db = _Boom()
        assert await svc._device_account_write_denied("UPDATE", 7, 42) is False

    async def test_an_unidentified_turn_is_left_to_the_other_gate(self, monkeypatch):
        from services import conversation_memory_service as cms

        monkeypatch.setattr(cms.settings, "auth_enabled", True)
        svc = _service_with_device_flag(True)
        assert await svc._device_account_write_denied("UPDATE", 7, None) is False

    async def test_all_three_write_paths_consult_the_gate(self):
        from services import conversation_memory_service as cms

        for fn in (
            cms.ConversationMemoryService._apply_update_v2,
            cms.ConversationMemoryService._apply_delete_v2,
            cms.ConversationMemoryService._extraction_target_owned,
        ):
            assert "_device_account_write_denied" in inspect.getsource(fn), fn.__name__
