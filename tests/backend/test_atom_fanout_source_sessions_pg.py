"""The atom fan-out on a FRESH session -- one session per source, real Postgres.

Why this file exists (2026-11-15): every inbound federated retrieval logged
"2/8 sources delivered, 3 empty, 3 crashed". `federation_query_responder`
opens a fresh `AsyncSession` and `PolymorphicAtomStore.query` fanned eight
sources out over it at once. A session holds one connection; all eight tried
to provision it, one won, the rest ended in `InvalidRequestError: This session
is provisioning a new connection; concurrent operations are not permitted`.

The fix gives every source a session of its own (`SourceSessions`). What that
must NOT change is tested here next to the regression itself:

- the scope (`user_id=asker_id`, `peer`) of every source,
- that one crashing source leaves the others alone,
- that every session is closed -- on a crash and on cancellation,
- the `access_count` contract of the memory source,
- that nothing returned needs its session after it is closed,
- the bound on concurrently open source sessions.

The defect cannot be shown with a mocked session: it lives in how a real
session provisions a real connection. Applies only with
`RENFIELD_TEST_PG_URL` (skipped otherwise).
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from loguru import logger as _loguru
from sqlalchemy import event, inspect, select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from models.database import (
    EMBEDDING_DIMENSION,
    SKILL_SOURCE_USER_CREATED,
    SKILL_STATUS_APPROVED,
    SKILL_STATUS_DRAFT,
    ConversationMemory,
    ProceduralSkill,
    Role,
    SkillWouldHaveInjectedLog,
    User,
)
from services.circle_sql import PeerScope
from services.federation_pending_store import _PendingRequest
from utils.config import settings

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

#: A token no other fixture row carries, so every hit is one of ours.
_TOKEN = "zirkonium"

_VEC = [0.0] * EMBEDDING_DIMENSION
_VEC[3] = 1.0

_SUMMARY = re.compile(
    r"(\d+)/(\d+) sources delivered, (\d+) empty, (\d+) crashed")


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def production_flags(monkeypatch):
    """The flag set BOTH live instances run with (`k8s/configmap.yaml`).

    In the container `skill` and `note` are off; a test on the defaults would
    measure six sources of a configuration that does not exist in production.
    """
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "skills_enabled", True)
    monkeypatch.setattr(settings, "notes_enabled", True)
    monkeypatch.setattr(settings, "graph_expansion_enabled", True)
    monkeypatch.setattr(settings, "memory_kg_bridge_enabled", True)


@pytest.fixture
def no_model_server(monkeypatch):
    """Embeddings without a model server: one fixed vector for every text.

    Each call yields to the event loop once, as the real HTTP call does -- the
    sources reach the database interleaved, not one after the other.
    """
    import services.kg_retrieval as kg_mod
    import services.memory_retrieval as mem_mod
    import services.rag_retrieval as rag_mod
    import services.skill_service as skill_mod
    import utils.llm_client as llm_mod

    class _Embedder:
        async def embeddings(self, **_kwargs):
            await asyncio.sleep(0)
            return SimpleNamespace(embedding=list(_VEC))

    embedder = _Embedder()
    for mod in (kg_mod, mem_mod, rag_mod, skill_mod, llm_mod):
        monkeypatch.setattr(mod, "get_embed_client", lambda: embedder)

    # The KG source asks an LLM for entity names first; without one it falls
    # back to the query text -- stated here instead of left to a timeout.
    async def _no_names(self, query, lang="de"):
        return []

    monkeypatch.setattr(kg_mod.KGRetrieval, "_extract_query_entities", _no_names)


@pytest.fixture
async def two_owners(async_engine, production_flags, no_model_server) -> dict:
    """Two local people, each with memories on several tiers -- COMMITTED,
    because the sources read on connections of their own."""
    maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as seed:
        role = Role(name="fanout-role", description="t", permissions=[])
        seed.add(role)
        await seed.flush()
        first = User(username="fanout-first", password_hash="x", is_active=True, role_id=role.id)
        second = User(username="fanout-second", password_hash="x", is_active=True, role_id=role.id)
        seed.add_all([first, second])
        await seed.flush()

        def memory(owner: User, tier: int, label: str) -> ConversationMemory:
            return ConversationMemory(
                user_id=owner.id, content=f"{_TOKEN} {label}", category="fact",
                importance=0.8, circle_tier=tier, embedding=_VEC, access_count=0,
            )

        rows = {
            "first_self": memory(first, 0, "first self"),
            "first_household": memory(first, 2, "first household"),
            "first_public": memory(first, 4, "first public"),
            "second_self": memory(second, 0, "second self"),
            "second_household": memory(second, 2, "second household"),
            "second_public": memory(second, 4, "second public"),
        }
        seed.add_all(rows.values())
        await seed.commit()
        return {
            "first": first.id,
            "second": second.id,
            "ids": {name: row.id for name, row in rows.items()},
        }


@pytest.fixture
def responder(tmp_path, own_session_to_test_db):
    """A responder whose own sessions point at the TEST database."""
    from services.federation_identity import (
        init_federation_identity,
        reset_federation_identity_for_tests,
    )
    from services.federation_query_responder import FederationQueryResponder

    reset_federation_identity_for_tests()
    init_federation_identity(tmp_path / "responder_key")
    yield FederationQueryResponder(db=MagicMock())
    reset_federation_identity_for_tests()


@pytest.fixture
async def pooled_engine(pg_url, async_engine):
    """An engine with a REAL pool (`.engine`) and a count of the connections
    checked out of it (`.book`).

    The harness engine uses `NullPool`, which keeps no books. `async_engine`
    is requested only for its table reset.
    """
    engine = create_async_engine(pg_url, pool_size=20, max_overflow=0, echo=False)
    book = {"out": 0, "peak": 0}

    @event.listens_for(engine.sync_engine, "checkout")
    def _out(*_a):
        book["out"] += 1
        book["peak"] = max(book["peak"], book["out"])

    @event.listens_for(engine.sync_engine, "checkin")
    def _in(*_a):
        book["out"] -= 1

    try:
        yield SimpleNamespace(engine=engine, book=book)
    finally:
        await engine.dispose()


def _fallback_request(owner_id: int, tier: int) -> _PendingRequest:
    """A peer-scoped request: no linked person, reach = the pairing's tier."""
    return _PendingRequest(
        request_id="fanout-fallback", asker_pubkey="ab" * 32, peer_user_id=1,
        asker_local_user_id=None, max_visible_tier=tier, query=_TOKEN,
        initiated_at=time.time(), peer_owner_id=owner_id, mapped=False,
    )


def _mapped_request(person_id: int) -> _PendingRequest:
    """A mapped request: runs AS the linked local person."""
    return _PendingRequest(
        request_id="fanout-mapped", asker_pubkey="cd" * 32, peer_user_id=1,
        asker_local_user_id=person_id, max_visible_tier=0, query=_TOKEN,
        initiated_at=time.time(), peer_owner_id=None, mapped=True,
    )


class _Lines:
    """Collects what the retrieval logs -- level and message, nothing else."""

    def __init__(self):
        self.records: list[tuple[str, str]] = []

    def __enter__(self):
        self._sink = _loguru.add(
            lambda m: self.records.append((m.record["level"].name, m.record["message"])),
            level="INFO", format="{message}")
        return self

    def __exit__(self, *_exc):
        _loguru.remove(self._sink)

    def summary(self) -> tuple[int, int, int, int]:
        """(delivered, asked, empty, crashed) of the ONE fan-out line."""
        found = [m for _, m in self.records if "sources delivered" in m]
        assert len(found) == 1, f"expected one summary line, got: {found}"
        match = _SUMMARY.search(found[0])
        assert match, found[0]
        return tuple(int(g) for g in match.groups())  # type: ignore[return-value]

    def crashed_sources(self) -> list[str]:
        return [m for _, m in self.records if "crashed (" in m]


async def _retrieve_cold(responder, pending):
    """Exactly what `_run_query` does: a fresh session, the fan-out at once."""
    import services.federation_query_responder as fqr

    with _Lines() as lines:
        async with fqr.AsyncSessionLocal() as session:
            matches = await responder._retrieve(session, pending)
    return matches, lines


def _memory_ids(matches) -> set[int]:
    return {
        m.atom.payload["memory_id"] for m in matches
        if "memory_id" in m.atom.payload
    }


# --------------------------------------------------------------------------- #
# 1. The regression
# --------------------------------------------------------------------------- #


class TestAColdFederatedRetrievalLosesNoSource:
    async def test_no_source_crashes_on_a_fresh_session(self, responder, two_owners):
        """🛑 The live defect. RED before the fix: the sources that reach the
        database while the shared session is still provisioning its connection
        end in `InvalidRequestError` and are counted as crashed."""
        matches, lines = await _retrieve_cold(
            responder, _fallback_request(two_owners["first"], 2))

        delivered, asked, empty, crashed = lines.summary()
        assert crashed == 0, lines.crashed_sources()
        assert asked == 8, "all eight sources are on in production"
        assert delivered + empty == asked
        # Not merely "nothing crashed": the two sources that have rows to find
        # found them -- semantic and lexical memory.
        assert delivered >= 2, lines.records
        assert matches, "the peer got an empty answer"

    async def test_the_mapped_path_is_cold_too(self, responder, two_owners):
        """Mapped is the path that runs live; it uses the same fresh session."""
        _, lines = await _retrieve_cold(responder, _mapped_request(two_owners["first"]))
        delivered, asked, empty, crashed = lines.summary()
        assert (crashed, delivered + empty) == (0, asked), lines.records
        assert delivered >= 2


# --------------------------------------------------------------------------- #
# 2. The scope survives the move to a session of its own
# --------------------------------------------------------------------------- #


class TestTheScopeIsTheSameOnASessionOfItsOwn:
    async def test_a_peer_reads_its_owner_from_its_tier_and_nothing_else(
        self, responder, two_owners
    ):
        ids = two_owners["ids"]
        matches, _ = await _retrieve_cold(
            responder, _fallback_request(two_owners["first"], 2))

        seen = _memory_ids(matches)
        # Granted tier 2 at the FIRST owner: that owner's rows from tier 2 up,
        # plus whatever is public anywhere.
        assert seen == {
            ids["first_household"], ids["first_public"], ids["second_public"],
        }, seen

    async def test_the_owner_binding_is_not_an_accident_of_the_data(
        self, responder, two_owners
    ):
        """Negative control: the SAME tier at the OTHER owner reads the other
        owner's rows. So the result above comes from the scope, not from the
        query finding only one person's rows."""
        ids = two_owners["ids"]
        matches, _ = await _retrieve_cold(
            responder, _fallback_request(two_owners["second"], 2))
        assert _memory_ids(matches) == {
            ids["second_household"], ids["second_public"], ids["first_public"],
        }

    async def test_the_narrowest_tier_reads_public_only(self, responder, two_owners):
        ids = two_owners["ids"]
        matches, _ = await _retrieve_cold(
            responder, _fallback_request(two_owners["first"], 4))
        assert _memory_ids(matches) == {ids["first_public"], ids["second_public"]}

    async def test_a_mapped_request_reads_as_the_linked_person(
        self, responder, two_owners
    ):
        """`user_id=asker_id` reaches the source: the person's own rows on
        every tier, nobody else's private ones. A source that lost the id
        would fall back to public-only and drop the first two."""
        ids = two_owners["ids"]
        matches, _ = await _retrieve_cold(responder, _mapped_request(two_owners["first"]))
        assert _memory_ids(matches) == {
            ids["first_self"], ids["first_household"], ids["first_public"],
            ids["second_public"],
        }


class _Recorder:
    """Stands in for a retrieval class: records the session it was built on
    and the arguments of every call."""

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


class _FakeSession:
    def __init__(self):
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        self.closed = True
        return False


class TestEverySourceIsHandedItsScope:
    """The forwarding, for all EIGHT sources -- the real-Postgres tests above
    can only see the sources that have rows."""

    _ASKER_KEY = {
        "rag": "user_id", "kg": "user_id", "memory": "user_id",
        "lexical_chunks": "asker_id", "lexical_memories": "asker_id",
        "skill": "asker_id", "document_fact": "asker_id", "note": "asker_id",
    }

    async def _run(self, *, own_sessions: bool, peer):
        from services.polymorphic_atom_store import PolymorphicAtomStore, SourceSessions

        opened: list[_FakeSession] = []

        def factory():
            opened.append(_FakeSession())
            return opened[-1]

        caller_session = MagicMock(name="caller-session")
        store = PolymorphicAtomStore(
            caller_session,
            source_sessions=SourceSessions(factory) if own_sessions else None,
        )
        await store.query("what", asker_id=41, max_visible_tier=2, top_k=5,
                          peer=peer, federated=own_sessions)
        return caller_session, opened

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

    async def test_a_session_of_its_own_changes_nothing_but_the_two_writes(
        self, recorded_sources
    ):
        """Both regimes, argument by argument. The only permitted differences
        are the two write switches: `track_access` of the memory source and
        `record_shadow_log` of the skill source."""
        peer = PeerScope(owner_id=7, tier=2)
        await self._run(own_sessions=False, peer=peer)
        shared = {c["source"]: dict(c["kwargs"]) for c in recorded_sources}
        recorded_sources.clear()
        await self._run(own_sessions=True, peer=peer)
        own = {c["source"]: dict(c["kwargs"]) for c in recorded_sources}

        assert shared["memory"].pop("track_access") is True
        assert own["memory"].pop("track_access") is False
        assert shared["skill"].pop("record_shadow_log") is True
        assert own["skill"].pop("record_shadow_log") is False
        assert own == shared

    async def test_every_source_gets_a_DIFFERENT_session_and_it_is_closed(
        self, recorded_sources
    ):
        caller_session, opened = await self._run(own_sessions=True, peer=None)

        sessions = [call["session"] for call in recorded_sources]
        assert len({id(s) for s in sessions}) == 8, "two sources shared a session"
        assert all(s is not caller_session for s in sessions)
        assert len(opened) == 8
        assert all(s.closed for s in opened)

    async def test_without_source_sessions_everything_stays_on_the_callers_session(
        self, recorded_sources
    ):
        """The local path (`GET /api/atoms`) is untouched by this change."""
        caller_session, opened = await self._run(own_sessions=False, peer=None)
        assert opened == []
        assert all(call["session"] is caller_session for call in recorded_sources)

    async def test_a_source_that_is_switched_off_takes_no_session(
        self, recorded_sources, monkeypatch
    ):
        monkeypatch.setattr(settings, "skills_enabled", False)
        monkeypatch.setattr(settings, "notes_enabled", False)
        _, opened = await self._run(own_sessions=True, peer=None)
        assert len(opened) == 6
        assert {c["source"] for c in recorded_sources} == set(self._ASKER_KEY) - {"skill", "note"}


# --------------------------------------------------------------------------- #
# 3. Crash and cancellation: every session is closed
# --------------------------------------------------------------------------- #


def _store_on(engine, *, limit: int = 8):
    """A store in the federated regime on `engine`, plus its caller session
    factory."""
    from services.polymorphic_atom_store import PolymorphicAtomStore, SourceSessions

    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    sessions = SourceSessions(maker, limit=limit)
    return maker, lambda db: PolymorphicAtomStore(db, source_sessions=sessions)


class TestACrashingSourceStaysAlone:
    async def test_the_others_deliver_and_no_connection_is_left_out(
        self, pooled_engine, two_owners, monkeypatch
    ):
        """One source fails INSIDE the database, mid-transaction. On a shared
        session that poisons the transaction for everyone after it; on its own
        session it is that source's problem alone."""
        import services.note_retrieval as note_mod

        async def _broken(self, query, *, asker_id, top_k, peer=None):
            await self.db.execute(text("SELECT 1"))
            await self.db.execute(text("SELECT * FROM no_such_table_fanout"))

        monkeypatch.setattr(note_mod.NoteRetrieval, "search", _broken)

        maker, store_on = _store_on(pooled_engine.engine)
        with _Lines() as lines:
            async with maker() as db:
                matches = await store_on(db).query(
                    _TOKEN, asker_id=two_owners["first"], max_visible_tier=0, top_k=10)

        delivered, asked, empty, crashed = lines.summary()
        assert crashed == 1, lines.records
        assert len(lines.crashed_sources()) == 1
        assert "'note'" in lines.crashed_sources()[0]
        assert delivered >= 2 and delivered + empty == asked - 1
        assert two_owners["ids"]["first_self"] in _memory_ids(matches)
        assert pooled_engine.book["out"] == 0, "a connection was not returned"
        assert pooled_engine.engine.sync_engine.pool.checkedout() == 0

    async def test_a_source_raising_before_any_statement_closes_its_session_too(
        self, pooled_engine, two_owners, monkeypatch
    ):
        import services.rag_retrieval as rag_mod

        async def _broken(self, query, **_kwargs):
            raise RuntimeError("no statement was ever sent")

        monkeypatch.setattr(rag_mod.RAGRetrieval, "search", _broken)

        maker, store_on = _store_on(pooled_engine.engine)
        with _Lines() as lines:
            async with maker() as db:
                await store_on(db).query(
                    _TOKEN, asker_id=two_owners["first"], max_visible_tier=0, top_k=10)

        assert lines.summary()[3] == 1
        assert "'rag'" in lines.crashed_sources()[0]
        assert pooled_engine.engine.sync_engine.pool.checkedout() == 0


class TestCancellationClosesEverySession:
    async def test_cancelling_the_fan_out_returns_every_connection(
        self, pooled_engine, two_owners, monkeypatch
    ):
        """All sources are inside a long statement when the task is cancelled
        -- the state a federated query is in when its asker gives up."""
        import services.document_fact_retrieval as fact_mod
        import services.note_retrieval as note_mod

        in_flight = asyncio.Event()
        started = {"n": 0}

        async def _hangs(self, query, *, asker_id, top_k, peer=None):
            await self.db.execute(text("SELECT 1"))
            started["n"] += 1
            if started["n"] == 2:
                in_flight.set()
            await self.db.execute(text("SELECT pg_sleep(30)"))
            return []

        monkeypatch.setattr(note_mod.NoteRetrieval, "search", _hangs)
        monkeypatch.setattr(fact_mod.DocumentFactRetrieval, "search", _hangs)

        maker, store_on = _store_on(pooled_engine.engine)

        async def _query():
            async with maker() as db:
                return await store_on(db).query(
                    _TOKEN, asker_id=two_owners["first"], max_visible_tier=0, top_k=10)

        task = asyncio.create_task(_query())
        await asyncio.wait_for(in_flight.wait(), timeout=20)
        assert pooled_engine.book["out"] >= 2, "the hanging sources hold no connection?"

        t0 = time.perf_counter()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert time.perf_counter() - t0 < 10, "the cancellation waited for pg_sleep"

        assert pooled_engine.book["out"] == 0, "a connection survived the cancellation"
        assert pooled_engine.engine.sync_engine.pool.checkedout() == 0


# --------------------------------------------------------------------------- #
# 4. The access_count contract
# --------------------------------------------------------------------------- #


async def _access_counts(engine, ids: dict[str, int]) -> dict[int, int]:
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as check:
        rows = (await check.execute(
            select(ConversationMemory.id, ConversationMemory.access_count,
                   ConversationMemory.last_accessed_at)
            .where(ConversationMemory.id.in_(list(ids.values())))
        )).all()
    assert all(r.last_accessed_at is None for r in rows if r.access_count == 0)
    return {r.id: r.access_count for r in rows}


def _statements_on(engine) -> list[str]:
    seen: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _record(_conn, _cursor, statement, *_a):
        seen.append(statement)

    return seen


class TestTheAccessCountContract:
    async def test_a_fan_out_on_own_sessions_is_a_pure_read(
        self, pooled_engine, two_owners
    ):
        """No UPDATE is sent at all -- so no row lock is ever held by a source
        session, and a peer's question leaves the recency ranking alone."""
        statements = _statements_on(pooled_engine.engine)
        maker, store_on = _store_on(pooled_engine.engine)
        async with maker() as db:
            matches = await store_on(db).query(
                _TOKEN, asker_id=two_owners["first"], max_visible_tier=0, top_k=10)

        assert _memory_ids(matches), "the memory source found nothing -- no proof"
        updates = [s for s in statements if s.lstrip().upper().startswith("UPDATE")]
        assert updates == [], updates
        counts = await _access_counts(pooled_engine.engine, two_owners["ids"])
        assert set(counts.values()) == {0}, counts

    async def test_on_the_callers_session_the_caller_still_decides(
        self, pooled_engine, two_owners, monkeypatch
    ):
        """The local regime is unchanged: the bump is flushed into the CALLER'S
        transaction and lives or dies with it."""
        from services.polymorphic_atom_store import PolymorphicAtomStore

        # The skill shadow log commits on the shared session; out of the way,
        # so that what is measured is the memory source alone.
        monkeypatch.setattr(settings, "skills_enabled", False)
        maker = async_sessionmaker(pooled_engine.engine, class_=AsyncSession, expire_on_commit=False)
        own_id = two_owners["ids"]["first_self"]

        async def _count(db) -> int:
            return (await db.execute(
                select(ConversationMemory.access_count)
                .where(ConversationMemory.id == own_id))).scalar_one()

        async with maker() as db:
            await db.execute(text("SELECT 1"))          # warm, as `get_db` hands it over
            await PolymorphicAtomStore(db).query(
                _TOKEN, asker_id=two_owners["first"], max_visible_tier=0, top_k=10)
            assert await _count(db) == 1, "the bump is not visible to its own transaction"
            await db.rollback()
        assert (await _access_counts(pooled_engine.engine, two_owners["ids"]))[own_id] == 0

        async with maker() as db:
            await db.execute(text("SELECT 1"))
            await PolymorphicAtomStore(db).query(
                _TOKEN, asker_id=two_owners["first"], max_visible_tier=0, top_k=10)
            await db.commit()
        assert (await _access_counts(pooled_engine.engine, two_owners["ids"]))[own_id] == 1


# --------------------------------------------------------------------------- #
# 4b. The skill shadow log
# --------------------------------------------------------------------------- #


@pytest.fixture
async def skills(async_engine, two_owners) -> dict:
    """Skills of the first owner: one APPROVED (without it `find_similar`
    returns before it reaches the database) and two DRAFTS the draft gate
    holds back -- exactly what the shadow log exists to record. One draft
    sits on tier 2, so a peer granted tier 2 can see it as well."""
    from services.skill_service import SkillService

    maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    def skill(title: str, status: str, tier: int) -> ProceduralSkill:
        return ProceduralSkill(
            user_id=two_owners["first"], title=title, body_md=f"{_TOKEN} {title}",
            source=SKILL_SOURCE_USER_CREATED, status=status, circle_tier=tier,
            embedding=_VEC,
        )

    async with maker() as seed:
        rows = {
            "approved": skill("approved skill", SKILL_STATUS_APPROVED, 2),
            "draft_self": skill("draft self", SKILL_STATUS_DRAFT, 0),
            "draft_household": skill("draft household", SKILL_STATUS_DRAFT, 2),
        }
        seed.add_all(rows.values())
        await seed.commit()
        ids = {name: row.id for name, row in rows.items()}
    SkillService.invalidate_has_skills_cache()
    yield ids
    SkillService.invalidate_has_skills_cache()


async def _shadow_rows(engine) -> list[tuple[int, int | None]]:
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as check:
        return [tuple(r) for r in (await check.execute(
            select(SkillWouldHaveInjectedLog.skill_id, SkillWouldHaveInjectedLog.user_id)
            .order_by(SkillWouldHaveInjectedLog.skill_id)
        )).all()]


def _skill_ids(matches) -> set[int]:
    return {m.atom.payload["skill_id"] for m in matches if "skill_id" in m.atom.payload}


class TestAFederatedQuestionWritesNoShadowLog:
    """🛑 "Pure read" has to be true for ALL eight sources. The skill source
    wrote: a mapped question inserted would-have-injected rows under the
    linked local account, and a peer-scoped one ran a shadow query that
    failed on every call (its bindings lacked the peer's)."""

    async def test_a_mapped_question_writes_nothing_under_the_linked_account(
        self, responder, two_owners, skills, async_engine
    ):
        matches, lines = await _retrieve_cold(responder, _mapped_request(two_owners["first"]))

        # The source got past its early return and did its real work ...
        assert skills["approved"] in _skill_ids(matches), lines.records
        # ... and left nothing behind.
        assert await _shadow_rows(async_engine) == []
        assert not [m for _, m in lines.records if "Shadow-log" in m]
        assert lines.summary()[3] == 0

    async def test_a_peer_scoped_question_writes_nothing_and_fails_nothing(
        self, responder, two_owners, skills, async_engine
    ):
        matches, lines = await _retrieve_cold(
            responder, _fallback_request(two_owners["first"], 2))

        assert skills["approved"] in _skill_ids(matches), lines.records
        assert await _shadow_rows(async_engine) == []
        assert not [m for _, m in lines.records if "Shadow-log" in m], lines.records
        assert lines.summary()[3] == 0

    async def test_the_local_path_still_logs_what_the_draft_gate_held_back(
        self, two_owners, skills, async_engine
    ):
        """Negative control, and the unchanged contract of the local path:
        the same data, asked locally by its owner, IS logged -- under the
        asking account."""
        from services.polymorphic_atom_store import PolymorphicAtomStore

        maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
        async with maker() as db:
            await db.execute(text("SELECT 1"))          # warm, as `get_db` hands it over
            matches = await PolymorphicAtomStore(db).query(
                _TOKEN, asker_id=two_owners["first"], max_visible_tier=0, top_k=10)

        assert skills["approved"] in _skill_ids(matches)
        first = two_owners["first"]
        assert await _shadow_rows(async_engine) == sorted(
            [(skills["draft_self"], first), (skills["draft_household"], first)])

    async def test_a_peer_scoped_call_never_logs_even_when_asked_to(
        self, two_owners, skills, async_engine
    ):
        """There is no asking local account on a peer-scoped call -- nobody a
        row could be logged under. Before, this ran a shadow query whose
        bindings lacked the peer's and failed on every call."""
        from services.skill_service import SkillService

        maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
        with _Lines() as lines:
            async with maker() as db:
                found = await SkillService(db).find_similar(
                    _TOKEN, asker_id=0, top_k=5, record_shadow_log=True,
                    peer=PeerScope(owner_id=two_owners["first"], tier=2))

        assert [f["id"] for f in found] == [skills["approved"]]
        assert await _shadow_rows(async_engine) == []
        assert not [m for _, m in lines.records if "Shadow-log" in m], lines.records


# --------------------------------------------------------------------------- #
# 5. Nothing returned needs a session
# --------------------------------------------------------------------------- #


class TestTheResultOutlivesItsSessions:
    async def test_matches_are_plain_data_once_every_session_is_closed(
        self, responder, two_owners
    ):
        from services.atom_types import Provenance

        matches, _ = await _retrieve_cold(
            responder, _fallback_request(two_owners["first"], 2))
        assert matches

        def walk(value):
            assert inspect(value, raiseerr=False) is None, (
                f"an ORM instance left its session: {type(value).__name__}")
            if isinstance(value, dict):
                for v in value.values():
                    walk(v)
            elif isinstance(value, (list, tuple, set)):
                for v in value:
                    walk(v)

        for m in matches:
            walk(m.atom.payload)
            walk(m.atom.policy)
            json.dumps(m.atom.payload, default=str)     # every field is readable
            # What `_run_query` and `_synthesize` read after the session is gone.
            assert isinstance(m.snippet, str)
            Provenance(
                atom_id=m.atom.atom_id, atom_type=m.atom.atom_type,
                display_label=m.snippet[:120], score=m.score,
            ).redacted_for_remote()

    async def test_the_synthesis_consumer_runs_on_the_closed_result(
        self, responder, two_owners, monkeypatch
    ):
        """`_synthesize` runs after `_run_query` has left its `async with`."""
        import utils.llm_client as llm_mod

        def _no_llm():
            raise RuntimeError("no model server in this test")

        monkeypatch.setattr(llm_mod, "get_default_client", _no_llm)
        matches, _ = await _retrieve_cold(
            responder, _fallback_request(two_owners["first"], 2))
        answer = await responder._synthesize(_TOKEN, list(matches))
        assert _TOKEN in answer


# --------------------------------------------------------------------------- #
# 6. The bound
# --------------------------------------------------------------------------- #


class TestTheBoundOnSourceSessions:
    async def test_no_more_connections_than_slots_whatever_the_load(self, pooled_engine):
        """Sixteen sources -- two overlapping fan-outs -- on three slots."""
        from services.polymorphic_atom_store import SourceSessions

        maker = async_sessionmaker(pooled_engine.engine, class_=AsyncSession, expire_on_commit=False)
        sessions = SourceSessions(maker, limit=3)

        async def source(i: int) -> int:
            async with sessions.session() as s:
                return (await s.execute(text(f"SELECT {int(i)}, pg_sleep(0.05)"))).first()[0]

        results = await asyncio.gather(*(source(i) for i in range(16)))
        assert results == list(range(16))
        assert pooled_engine.book["peak"] == 3, pooled_engine.book
        assert pooled_engine.book["out"] == 0

    async def test_one_fan_out_runs_in_parallel_within_the_default_limit(
        self, pooled_engine
    ):
        """Regime C of the probe: eight at once, not eight in a row."""
        from services.polymorphic_atom_store import SOURCE_SESSION_LIMIT, SourceSessions

        assert SOURCE_SESSION_LIMIT == 8
        maker = async_sessionmaker(pooled_engine.engine, class_=AsyncSession, expire_on_commit=False)
        sessions = SourceSessions(maker)

        async def source() -> None:
            async with sessions.session() as s:
                await s.execute(text("SELECT pg_sleep(0.2)"))

        t0 = time.perf_counter()
        await asyncio.gather(*(source() for _ in range(8)))
        took = time.perf_counter() - t0
        assert pooled_engine.book["peak"] == 8
        assert took < 1.0, f"{took:.2f} s -- eight times 0.2 s in a row would be 1.6 s"

    async def test_two_real_fan_outs_stay_within_the_slots(
        self, pooled_engine, two_owners
    ):
        maker, store_on = _store_on(pooled_engine.engine, limit=4)

        async def one():
            async with maker() as db:
                return await store_on(db).query(
                    _TOKEN, asker_id=two_owners["first"], max_visible_tier=0, top_k=10)

        with _Lines() as lines:
            first, second = await asyncio.gather(one(), one())
        assert _memory_ids(first) == _memory_ids(second) != set()
        assert not lines.crashed_sources()
        # Four slots for the sources, plus at most one caller session each
        # (graph expansion, after the fan-out).
        assert pooled_engine.book["peak"] <= 4 + 2, pooled_engine.book

    async def test_a_limit_below_one_is_refused(self):
        from services.polymorphic_atom_store import SourceSessions

        with pytest.raises(ValueError):
            SourceSessions(lambda: None, limit=0)

    async def test_the_responder_shares_ONE_instance_across_retrievals(self):
        """The bound holds only across fan-outs that share the object -- a
        `SourceSessions` built per request would bound nothing."""
        import ast
        import inspect as _inspect

        import services.federation_query_responder as fqr
        from services.polymorphic_atom_store import SourceSessions

        assert isinstance(fqr._SOURCE_SESSIONS, SourceSessions)
        body = ast.parse(_inspect.getsource(fqr))
        built = [
            n for n in ast.walk(body)
            if isinstance(n, ast.Call)
            and getattr(n.func, "id", getattr(n.func, "attr", "")) == "SourceSessions"
        ]
        assert len(built) == 1, "SourceSessions is constructed in more than one place"
