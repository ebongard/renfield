"""What the atom fan-out HANDS each of its eight sources -- no database.

Split off from `test_atom_fanout_source_sessions_pg.py`: that file carries the
`postgres` marker and is skipped without `RENFIELD_TEST_PG_URL`, and nothing
here needs a database. The security-relevant part of the move to own sessions
is exactly this forwarding -- the scope (`asker_id`, `peer`) of every source --
so it must not disappear with a missing environment variable.

Also here: what a source is handed INSTEAD of a session (`SourceReader`), the
statement-scoped slot of `SourceSessions`, and the per-source deadline.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from services.circle_sql import PeerScope
from utils.config import settings

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.fixture
def production_flags(monkeypatch):
    """All eight sources on, as on both live instances."""
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "skills_enabled", True)
    monkeypatch.setattr(settings, "notes_enabled", True)


class _Recorder:
    """Stands in for a retrieval class: records what it was built on (a
    session or a reader) and the arguments of every call."""

    def __init__(self, log: list, name_by_method: dict[str, str]):
        self._log = log
        self._names = name_by_method

    def __call__(self, session):
        recorder = self

        class _Instance:
            def __getattr__(self, method):
                source = recorder._names[method]

                async def call(*args, **kwargs):
                    await asyncio.sleep(0)
                    recorder._log.append(
                        {"source": source, "session": session,
                         "args": args, "kwargs": kwargs})
                    return []
                return call

        return _Instance()


@pytest.fixture
def recorded_sources(monkeypatch, production_flags):
    """All eight sources replaced by recorders. No database involved: this
    pins down what the store HANDS each source, for both regimes."""
    import importlib

    log: list = []
    for module, cls, methods in (
        ("rag_retrieval", "RAGRetrieval", {"search": "rag"}),
        ("kg_retrieval", "KGRetrieval", {"get_relevant_atoms": "kg"}),
        ("memory_retrieval", "MemoryRetrieval", {"retrieve": "memory"}),
        ("lexical_retrieval", "LexicalRetrieval", {
            "search_chunks_lexical": "lexical_chunks",
            "search_memories_lexical": "lexical_memories"}),
        ("skill_service", "SkillService", {"find_similar": "skill"}),
        ("document_fact_retrieval", "DocumentFactRetrieval", {"search": "document_fact"}),
        ("note_retrieval", "NoteRetrieval", {"search": "note"}),
    ):
        monkeypatch.setattr(
            importlib.import_module(f"services.{module}"), cls, _Recorder(log, methods))
    # Graph expansion would query the caller's session after the fusion.
    monkeypatch.setattr(settings, "graph_expansion_enabled", False)
    return log


class _FakeResult:
    def __init__(self, value):
        self.value = value

    def freeze(self):
        return lambda: self


class _FakeSession:
    """A session as `SourceSessions` uses one: entered, one statement, left."""

    def __init__(self, book: dict, *, fail: bool = False, hold: asyncio.Event | None = None):
        self._book, self._fail, self._hold = book, fail, hold
        self.closed = False
        self.bind = "the-engine"

    async def __aenter__(self):
        self._book["open"] += 1
        self._book["peak"] = max(self._book["peak"], self._book["open"])
        return self

    async def __aexit__(self, *_exc):
        self._book["open"] -= 1
        self.closed = True
        return False

    async def connection(self, execution_options=None):
        self._book.setdefault("options", []).append(execution_options)

    async def execute(self, statement, params=None, **_kwargs):
        if self._hold is not None:
            await self._hold.wait()
        await asyncio.sleep(0)
        if self._fail:
            raise RuntimeError("statement failed")
        return _FakeResult((statement, params))


class TestEverySourceIsHandedItsScope:
    """The forwarding, for all EIGHT sources -- the real-Postgres tests can
    only see the sources that have rows."""

    _ASKER_KEY = {
        "rag": "user_id", "kg": "user_id", "memory": "user_id",
        "lexical_chunks": "asker_id", "lexical_memories": "asker_id",
        "skill": "asker_id", "document_fact": "asker_id", "note": "asker_id",
    }

    async def _run(self, *, own_sessions: bool, peer, federated: bool | None = None):
        """`federated` defaults to the two combinations production has: the
        responder (own sessions, federated) and `GET /api/atoms` (neither)."""
        from services.polymorphic_atom_store import PolymorphicAtomStore, SourceSessions

        book = {"open": 0, "peak": 0}
        caller_session = MagicMock(name="caller-session")
        store = PolymorphicAtomStore(
            caller_session,
            source_sessions=(
                SourceSessions(lambda: _FakeSession(book)) if own_sessions else None),
        )
        await store.query("what", asker_id=41, max_visible_tier=2, top_k=5,
                          peer=peer,
                          federated=own_sessions if federated is None else federated)
        return caller_session

    @pytest.mark.parametrize("own_sessions", [True, False])
    async def test_asker_and_peer_reach_all_eight(self, recorded_sources, own_sessions):
        peer = PeerScope(owner_id=7, tier=2)
        await self._run(own_sessions=own_sessions, peer=peer)

        by_source = {call["source"]: call for call in recorded_sources}
        assert set(by_source) == set(self._ASKER_KEY), set(by_source)
        for source, call in by_source.items():
            assert call["kwargs"][self._ASKER_KEY[source]] == 41, source
            # The SAME object, not an equal one built on the way.
            assert call["kwargs"]["peer"] is peer, source
            assert call["args"] == ("what",), source

    async def test_the_regime_and_the_statement_change_nothing_but_the_two_writes(
        self, recorded_sources
    ):
        """Every permitted combination, argument by argument. The only
        differences allowed are the two write switches: `track_access` of the
        memory source and `record_shadow_log` of the skill source."""
        peer = PeerScope(owner_id=7, tier=2)
        seen = {}
        for own_sessions, federated in ((False, False), (False, True), (True, True)):
            recorded_sources.clear()
            await self._run(own_sessions=own_sessions, peer=peer, federated=federated)
            calls = {c["source"]: dict(c["kwargs"]) for c in recorded_sources}
            seen[own_sessions, federated] = (
                calls["memory"].pop("track_access"),
                calls["skill"].pop("record_shadow_log"),
                calls,
            )
        assert seen[False, False][2] == seen[False, True][2] == seen[True, True][2]
        assert seen[False, False][:2] == (True, True)

    @pytest.mark.parametrize("own_sessions", [True, False])
    async def test_a_federated_call_writes_nothing_whatever_session_it_runs_on(
        self, recorded_sources, own_sessions
    ):
        """🛑 The write decision follows what the caller STATES. Until
        2026-11-16 it was read off the session regime, so a federated call
        on the caller's session would have bumped memory access counts and
        written skill shadow rows under a local account."""
        await self._run(own_sessions=own_sessions, peer=None, federated=True)
        by_source = {c["source"]: c["kwargs"] for c in recorded_sources}
        assert by_source["memory"]["track_access"] is False
        assert by_source["skill"]["record_shadow_log"] is False

    async def test_a_local_call_on_own_sessions_is_refused_before_any_source_runs(
        self, recorded_sources
    ):
        """🛑 The other half: a reader runs one statement per session and
        cannot flush, so a local call there would lose both writes without a
        word. It is refused, loudly, and no source has been asked."""
        with pytest.raises(ValueError, match="federated=True"):
            await self._run(own_sessions=True, peer=None, federated=False)
        assert recorded_sources == []

    async def test_on_own_sessions_no_source_is_handed_the_callers_session(
        self, recorded_sources
    ):
        from services.polymorphic_atom_store import SourceReader

        caller_session = await self._run(own_sessions=True, peer=None)

        handed = [call["session"] for call in recorded_sources]
        assert len(handed) == 8
        assert all(isinstance(h, SourceReader) for h in handed)
        assert all(h is not caller_session for h in handed)
        assert len({id(h) for h in handed}) == 8, "two sources shared a reader"

    async def test_without_source_sessions_everything_stays_on_the_callers_session(
        self, recorded_sources
    ):
        """The local path (`GET /api/atoms`) is untouched by this change."""
        caller_session = await self._run(own_sessions=False, peer=None)
        assert all(call["session"] is caller_session for call in recorded_sources)

    async def test_a_source_that_is_switched_off_is_not_called(
        self, recorded_sources, monkeypatch
    ):
        monkeypatch.setattr(settings, "skills_enabled", False)
        monkeypatch.setattr(settings, "notes_enabled", False)
        await self._run(own_sessions=True, peer=None)
        assert {c["source"] for c in recorded_sources} == set(self._ASKER_KEY) - {"skill", "note"}


class TestWhatASourceIsHandedInsteadOfASession:
    def _sessions(self, book, *, limit=8, **session_kwargs):
        from services.polymorphic_atom_store import SourceSessions

        made: list[_FakeSession] = []

        def factory():
            made.append(_FakeSession(book, **session_kwargs))
            return made[-1]

        return SourceSessions(factory, limit=limit), made

    async def test_a_statement_opens_a_session_and_closes_it_again(self):
        book = {"open": 0, "peak": 0}
        sessions, made = self._sessions(book)
        reader = sessions.reader()

        first = await reader.execute("SELECT 1", {"a": 1})
        second = await reader.execute("SELECT 2")

        assert (first.value, second.value) == (("SELECT 1", {"a": 1}), ("SELECT 2", None))
        # One session PER STATEMENT, each closed: between the two the source
        # held nothing.
        assert len(made) == 2 and all(m.closed for m in made)
        assert (book["open"], book["peak"]) == (0, 1)
        # ... and each statement's transaction was opened READ ONLY.
        assert book["options"] == [{"postgresql_readonly": True}] * 2

    async def test_a_failing_statement_closes_its_session_and_frees_its_slot(self):
        book = {"open": 0, "peak": 0}
        sessions, made = self._sessions(book, limit=1, fail=True)

        with pytest.raises(RuntimeError):
            await sessions.reader().execute("SELECT broken")
        assert made[0].closed and book["open"] == 0
        # The one slot is free again: a second statement gets through.
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(sessions.reader().execute("SELECT again"), 1)

    async def test_a_cancelled_statement_closes_its_session_and_frees_its_slot(self):
        book = {"open": 0, "peak": 0}
        hold = asyncio.Event()
        sessions, made = self._sessions(book, limit=1, hold=hold)

        stuck = asyncio.create_task(sessions.reader().execute("SELECT slow"))
        while book["open"] == 0:
            await asyncio.sleep(0)
        stuck.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stuck
        assert made[0].closed and book["open"] == 0

        hold.set()
        assert (await asyncio.wait_for(
            sessions.reader().execute("SELECT next"), 1)).value == ("SELECT next", None)

    async def test_never_more_statements_at_once_than_slots(self):
        book = {"open": 0, "peak": 0}
        sessions, _ = self._sessions(book, limit=3)
        await asyncio.gather(*(sessions.reader().execute(f"SELECT {i}") for i in range(16)))
        assert (book["open"], book["peak"]) == (0, 3)

    async def test_the_reader_cannot_write(self):
        """🛑 The fan-out on own sessions is a pure read, and this is what
        holds it to that: a source that tries to write there fails loudly
        instead of writing on a connection nobody decides about."""
        sessions, _ = self._sessions({"open": 0, "peak": 0})
        reader = sessions.reader()
        for write in ("add", "add_all", "flush", "commit", "delete", "merge"):
            assert not hasattr(reader, write), write

    async def test_the_reader_answers_for_the_dialect(self):
        """`lexical`, `document_fact` and `note` ask `db.bind` which database
        they are on."""
        sessions, _ = self._sessions({"open": 0, "peak": 0})
        assert sessions.reader().bind == "the-engine"

    async def test_a_limit_below_one_is_refused(self):
        from services.polymorphic_atom_store import SourceSessions

        with pytest.raises(ValueError):
            SourceSessions(lambda: None, limit=0)


class TestASourceCannotOutliveItsDeadline:
    """A slow model server holds no slot -- but it would hold its own
    fan-out. The kg source alone can wait 300 s for its LLM."""

    async def _query(self, monkeypatch, *, own_sessions: bool, deadline: float):
        import services.kg_retrieval as kg_mod
        from services.polymorphic_atom_store import PolymorphicAtomStore, SourceSessions

        class _Stuck:
            def __init__(self, _session):
                pass

            async def get_relevant_atoms(self, *_a, **_k):
                await asyncio.sleep(3600)      # the model server never answers

        monkeypatch.setattr(kg_mod, "KGRetrieval", _Stuck)
        monkeypatch.setattr(settings, "atom_fanout_source_timeout", deadline)
        book = {"open": 0, "peak": 0}
        store = PolymorphicAtomStore(
            MagicMock(name="caller-session"),
            source_sessions=(
                SourceSessions(lambda: _FakeSession(book)) if own_sessions else None),
        )
        return await store.query("what", asker_id=41, max_visible_tier=2, top_k=5,
                                 federated=own_sessions)

    async def test_the_stuck_source_is_counted_as_crashed_and_the_rest_delivers(
        self, recorded_sources, monkeypatch
    ):
        from loguru import logger as _l

        lines: list[str] = []
        sink = _l.add(lambda m: lines.append(m.record["message"]), level="INFO",
                      format="{message}")
        try:
            await asyncio.wait_for(
                self._query(monkeypatch, own_sessions=True, deadline=0.2), 5)
        finally:
            _l.remove(sink)

        # The other seven were asked and came back.
        assert {c["source"] for c in recorded_sources} == {
            "rag", "memory", "lexical_chunks", "lexical_memories",
            "skill", "document_fact", "note"}
        crashed = [line for line in lines if "crashed (" in line]
        assert len(crashed) == 1 and "'kg'" in crashed[0] and "TimeoutError" in crashed[0]

    async def test_the_shared_session_regime_has_no_deadline(
        self, recorded_sources, monkeypatch
    ):
        """Negative control, and the unchanged local path: there the same
        stuck source is NOT cut off by this setting."""
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                self._query(monkeypatch, own_sessions=False, deadline=0.2), 1)
