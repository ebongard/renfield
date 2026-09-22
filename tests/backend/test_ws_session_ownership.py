"""#657 — WS push-registration ownership boundary.

A client may only register a session for server-push delivery if it owns that
conversation, or no such conversation exists yet. Enforced only when auth is on
and a JWT caller identity exists. Tested against the helper's logic with a faked
DB session so it stays a fast, deterministic unit test.

Changed with the auth-on cutover (P0 Nr. 5): an EXISTING ownerless conversation
used to be registerable by anyone, because "no row" and "row without an owner"
both read as `None`. With adoption gone such a row stays ownerless forever, so
the two cases are now told apart — no row is still fine, an ownerless row is
not the caller's.
"""
import pytest

import api.websocket.chat_handler as ch


# Sentinel telling "there is no conversation row" apart from "there is one and
# it has no owner" — the whole point of the change.
NO_ROW = object()


class _FakeRow:
    def __init__(self, user_id):
        self.id = 1
        self.user_id = user_id


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _FakeSession:
    def __init__(self, row):
        self._row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, *_a, **_k):
        return _FakeResult(self._row)


def _patch(monkeypatch, *, auth_enabled: bool, owner):
    """`owner=NO_ROW` → no conversation exists; else a row owned by `owner`
    (which may be None: an existing but ownerless conversation)."""
    row = None if owner is NO_ROW else _FakeRow(owner)
    monkeypatch.setattr("utils.config.settings.auth_enabled", auth_enabled)
    monkeypatch.setattr(ch, "AsyncSessionLocal", lambda: _FakeSession(row))


class TestSessionRegisterableBy:
    @pytest.mark.unit
    async def test_auth_disabled_always_allows(self, monkeypatch):
        # owner mismatch is irrelevant when auth is off (single-user mode)
        _patch(monkeypatch, auth_enabled=False, owner=999)
        assert await ch._session_registerable_by("s", 1) is True

    @pytest.mark.unit
    async def test_no_caller_identity_allows(self, monkeypatch):
        # device/satellite path (user_id=None) keeps legacy behavior
        _patch(monkeypatch, auth_enabled=True, owner=999)
        assert await ch._session_registerable_by("s", None) is True

    @pytest.mark.unit
    async def test_brand_new_session_allowed(self, monkeypatch):
        # No row at all: this registration CREATES the conversation for the caller.
        _patch(monkeypatch, auth_enabled=True, owner=NO_ROW)
        assert await ch._session_registerable_by("brand-new", 1) is True

    @pytest.mark.unit
    async def test_existing_ownerless_session_denied(self, monkeypatch):
        """P0 Nr. 5: an ownerless conversation belongs to nobody, and with
        adoption gone it stays that way. Anyone holding the client-minted id
        could otherwise register for its pushes."""
        _patch(monkeypatch, auth_enabled=True, owner=None)
        assert await ch._session_registerable_by("ownerless", 1) is False

    @pytest.mark.unit
    async def test_ownerless_session_still_allowed_with_auth_off(self, monkeypatch):
        # Single trust domain — unchanged.
        _patch(monkeypatch, auth_enabled=False, owner=None)
        assert await ch._session_registerable_by("ownerless", 1) is True

    @pytest.mark.unit
    async def test_owner_match_allowed(self, monkeypatch):
        _patch(monkeypatch, auth_enabled=True, owner=1)
        assert await ch._session_registerable_by("s", 1) is True

    @pytest.mark.unit
    async def test_owner_mismatch_denied(self, monkeypatch):
        # the core fix: user 2 cannot register user 1's session for pushes
        _patch(monkeypatch, auth_enabled=True, owner=1)
        assert await ch._session_registerable_by("s", 2) is False


class TestReplacementConversation:
    """A refused session must not cost the user their turn (P0 Nr. 5):
    `_restart_conversation_for_caller` opens a fresh conversation, saves the
    turn there and hands the client the new id."""

    class _WS:
        def __init__(self):
            self.sent: list[dict] = []

        async def send_json(self, msg):
            self.sent.append(msg)

    class _Msg:
        def __init__(self, mid):
            self.id = mid

    class _Ollama:
        def __init__(self):
            self.saves: list[tuple] = []

        async def save_message(self, session_id, role, content, db, **kw):
            self.saves.append((session_id, role, content, kw.get("user_id")))
            return TestReplacementConversation._Msg(len(self.saves))

    @pytest.mark.unit
    async def test_turn_lands_in_a_fresh_conversation(self, monkeypatch):
        monkeypatch.setattr("utils.config.settings.auth_enabled", True)
        ws, ollama = self._WS(), self._Ollama()

        session_id, user_msg_id, asst_msg_id = await ch._restart_conversation_for_caller(
            ws, object(), ollama,
            user_id=42,
            content="wie spät ist es",
            response="14 Uhr",
            user_metadata={"room_context": {"room_id": 1}},
            assistant_metadata={"intent": "time"},
        )

        # A NEW id, not the refused one, and both halves of the turn saved under it.
        assert session_id and session_id not in ("", None)
        assert [(s, r, u) for s, r, _c, u in ollama.saves] == [
            (session_id, "user", 42),
            (session_id, "assistant", 42),
        ]
        assert (user_msg_id, asst_msg_id) == (1, 2)

    @pytest.mark.unit
    async def test_the_frame_is_not_an_oracle(self, monkeypatch):
        """It carries the new id and nothing else. A reason or an owner would
        let a client probe whether a guessed session id exists or is taken."""
        monkeypatch.setattr("utils.config.settings.auth_enabled", True)
        ws, ollama = self._WS(), self._Ollama()

        session_id, _u, _a = await ch._restart_conversation_for_caller(
            ws, object(), ollama,
            user_id=42, content="x", response="y",
            user_metadata=None, assistant_metadata=None,
        )

        assert ws.sent == [{"type": "session_replaced", "session_id": session_id}]

    @pytest.mark.unit
    async def test_the_handler_answers_a_refusal_with_a_replacement(self):
        import inspect

        src = inspect.getsource(ch.websocket_endpoint)
        # The refusal is caught SEPARATELY from the generic save failure …
        assert "except PermissionError:" in src
        assert src.index("except PermissionError:") < src.index(
            "Failed to save messages to DB"
        )
        # … and answered with a replacement conversation.
        assert "_restart_conversation_for_caller(" in src
