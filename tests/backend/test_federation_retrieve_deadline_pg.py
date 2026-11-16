"""What the retrieval deadline of a federated query does when it is reached --
real Postgres, real statements, through the real entry point.

Why this file exists (2026-11-16): one deadline lay over the fan-out AND graph
expansion. A single source may use most of it (default 20 s of 25 s), so
expansion then had a few seconds left, and running out of them FAILED the
whole query -- the results of the other sources were thrown away for an
enrichment. And the reason stored for it read `TimeoutError: ` with nothing
after the colon.

What is pinned down here:

- a slow expansion after a source ran to its deadline -> ANSWERED, from the
  unexpanded result, and the audit row says so in a code;
- a fan-out that cannot finish in time                -> FAILED, with a reason
  that names the step and the deadline;
- in both cases the statement that was cancelled was a REAL one
  (`pg_sleep`), and afterwards no connection is checked out and every slot
  of the source sessions is free;
- an expansion that finishes in time marks nothing (the negative control).

Applies only with `RENFIELD_TEST_PG_URL` (skipped otherwise).
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from loguru import logger as _loguru
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.federation_audit import outcome_code
from services.federation_pending_store import get_pending_store
from services.federation_query_responder import _UNEXPANDED, _UNEXPANDED_NOTE
from services.federation_query_schemas import STATUS_COMPLETE, STATUS_FAILED
from tests.backend.test_federation_revoked_pairing_gets_no_answer_pg import (  # noqa: F401
    _admit,
    _answered_rows,
    _poll,
    far_side,
    maker,
    pairing,
    responder,
)
from utils.config import settings

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

_ANSWER = "an answer made from what the sources delivered"
_SLOTS = 8


@pytest.fixture
async def pool(pg_url, maker, monkeypatch):  # noqa: F811
    """The responder's own sessions on an engine with a REAL pool, and a
    count of the connections checked out of it. `maker` is requested so this
    replaces the harness factory AFTER it was put in place."""
    import services.federation_query_responder as fqr

    engine = create_async_engine(pg_url, pool_size=20, max_overflow=0, echo=False)
    book = {"out": 0, "peak": 0}

    @event.listens_for(engine.sync_engine, "checkout")
    def _out(*_a):
        book["out"] += 1
        book["peak"] = max(book["peak"], book["out"])

    @event.listens_for(engine.sync_engine, "checkin")
    def _in(*_a):
        book["out"] -= 1

    monkeypatch.setattr(
        fqr, "AsyncSessionLocal",
        async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False))
    try:
        yield SimpleNamespace(engine=engine, book=book)
    finally:
        await engine.dispose()


@pytest.fixture
def a_model_server_that_never_answers(monkeypatch):
    """Every embedding call hangs, so each source that needs one runs to its
    deadline; the lexical sources answer. All eight sources are asked."""
    import services.kg_retrieval as kg_mod
    import services.memory_retrieval as mem_mod
    import services.rag_retrieval as rag_mod
    import services.skill_service as skill_mod
    import utils.llm_client as llm_mod

    class _Embedder:
        async def embeddings(self, **_kwargs):
            await asyncio.sleep(3600)

    embedder = _Embedder()
    for mod in (kg_mod, mem_mod, rag_mod, skill_mod, llm_mod):
        monkeypatch.setattr(mod, "get_embed_client", lambda: embedder)

    async def _no_names(self, query, lang="de"):
        return []

    monkeypatch.setattr(kg_mod.KGRetrieval, "_extract_query_entities", _no_names)
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "skills_enabled", True)
    monkeypatch.setattr(settings, "notes_enabled", True)
    monkeypatch.setattr(settings, "graph_expansion_enabled", True)


def _deadlines(monkeypatch, *, source: float, step: float) -> None:
    """`step` seconds for the retrieval step, derived the way production
    derives it: from the synthesis timeout."""
    monkeypatch.setattr(settings, "atom_fanout_source_timeout", source)
    monkeypatch.setattr(settings, "federation_synthesis_timeout", 55.0 - step)
    assert settings.federation_retrieve_deadline == pytest.approx(step)


def _expansion(monkeypatch, body) -> dict:
    """Replace graph expansion's WORK by `body(db)`, on the session the store
    hands it. Returns what it saw."""
    import services.graph_expansion as ge

    seen = {"calls": 0, "cancelled": 0}

    async def expand_fused(merged, asker_id, db, **_kwargs):
        seen["calls"] += 1
        try:
            await body(db)
        except asyncio.CancelledError:
            seen["cancelled"] += 1
            raise
        return []

    monkeypatch.setattr(ge, "expand_fused", expand_fused)
    return seen


class _Warnings:
    def __enter__(self):
        self.lines: list[str] = []
        self._sink = _loguru.add(
            lambda m: self.lines.append(m.record["message"]), level="WARNING",
            format="{message}")
        return self

    def __exit__(self, *_exc):
        _loguru.remove(self._sink)


def _free_slots() -> int:
    import services.federation_query_responder as fqr

    return fqr._SOURCE_SESSIONS._slots_for_this_loop()._value


def _nothing_left_behind(pool) -> None:
    assert pool.book["out"] == 0, "a connection survived the deadline"
    assert pool.engine.sync_engine.pool.checkedout() == 0
    assert _free_slots() == _SLOTS, "a source-session slot was not released"


async def _no_statement_still_runs(pool, marker: str) -> None:
    """The cancelled statement is gone from the SERVER too, not only from
    this process's books."""
    async with pool.engine.connect() as conn:
        for _ in range(100):
            running = (await conn.execute(text(
                "SELECT count(*) FROM pg_stat_activity "
                # This database only: the view is server-wide, and a run next
                # door on a database of its own runs the same marked statement.
                "WHERE datname = current_database() "
                "AND state = 'active' AND pid <> pg_backend_pid() "
                "AND query LIKE :m"), {"m": f"%{marker}%"})).scalar()
            if running == 0:
                return
            await asyncio.sleep(0.05)
    raise AssertionError(f"{running} cancelled statement(s) still running on the server")


class TestASlowExpansionCostsTheEnrichmentNotTheAnswer:
    async def test_after_a_source_ran_to_its_deadline_the_answer_is_unexpanded(
        self, responder, maker, pairing, far_side, pool,  # noqa: F811
        a_model_server_that_never_answers, monkeypatch,
    ):
        _deadlines(monkeypatch, source=0.6, step=2.0)

        async def a_statement_that_does_not_end(db):
            await db.execute(text("SELECT pg_sleep(30) /* slow-expansion */"))

        seen = _expansion(monkeypatch, a_statement_that_does_not_end)
        handed_to_synthesis = []

        async def synthesize(query, matches):
            handed_to_synthesis.append(len(matches))
            return _ANSWER

        responder._synthesize = synthesize
        rid, finish = await _admit(responder, far_side)

        t0 = time.perf_counter()
        with _Warnings() as warnings:
            await asyncio.wait_for(finish(), 20)
        took = time.perf_counter() - t0

        # The premise: sources DID run to their deadline, and expansion was
        # inside a real statement when its budget ended.
        assert [w for w in warnings.lines if "crashed (" in w and "TimeoutError" in w]
        assert seen == {"calls": 1, "cancelled": 1}

        pending = await get_pending_store().get(rid)
        assert pending.status == STATUS_COMPLETE, pending.error_message
        assert pending.unexpanded is True
        assert handed_to_synthesis == [pending.retrieved_count]
        resp = await responder.handle_retrieve(_poll(far_side, rid))
        assert (resp.status, resp.answer) == (STATUS_COMPLETE, _ANSWER)

        # Logged, and visible where the outcome is recorded.
        assert len([w for w in warnings.lines if "UNEXPANDED" in w]) == 1
        (row,) = await _answered_rows(maker)
        assert (row.final_status, row.answer_excerpt) == ("success", _ANSWER)
        assert outcome_code(row.error_message) == (_UNEXPANDED, None)
        assert row.error_message == _UNEXPANDED_NOTE

        # Bounded by the step, not by the statement.
        assert took < settings.federation_retrieve_deadline + 3, f"{took:.1f} s"
        _nothing_left_behind(pool)
        await _no_statement_still_runs(pool, "slow-expansion")

    async def test_an_expansion_that_finishes_in_time_marks_nothing(
        self, responder, maker, pairing, far_side, pool,  # noqa: F811
        a_model_server_that_never_answers, monkeypatch,
    ):
        """Negative control: the same slow sources, an expansion with a real
        but short statement."""
        _deadlines(monkeypatch, source=0.6, step=4.0)

        async def a_short_statement(db):
            await db.execute(text("SELECT pg_sleep(0.1)"))

        seen = _expansion(monkeypatch, a_short_statement)
        responder._synthesize = lambda query, matches: _answer()
        rid, finish = await _admit(responder, far_side)

        with _Warnings() as warnings:
            await asyncio.wait_for(finish(), 20)

        assert seen == {"calls": 1, "cancelled": 0}
        pending = await get_pending_store().get(rid)
        assert pending.status == STATUS_COMPLETE and pending.unexpanded is False
        assert not [w for w in warnings.lines if "UNEXPANDED" in w]
        (row,) = await _answered_rows(maker)
        assert (row.final_status, row.error_message) == ("success", None)
        _nothing_left_behind(pool)

    async def test_a_fan_out_that_used_the_whole_budget_starts_no_expansion(
        self, responder, maker, pairing, far_side, pool,  # noqa: F811
        a_model_server_that_never_answers, monkeypatch,
    ):
        """The sources' deadline IS the budget -- a tie the startup check
        refuses, staged here: nothing is left, and expansion is not started
        whichever timer wins."""
        _deadlines(monkeypatch, source=0.9, step=1.0)
        assert settings.federation_retrieve_budget == pytest.approx(0.9)

        async def never_reached(db):
            raise AssertionError("expansion was started without a budget")

        seen = _expansion(monkeypatch, never_reached)
        responder._synthesize = lambda query, matches: _answer()
        rid, finish = await _admit(responder, far_side)

        await asyncio.wait_for(finish(), 20)

        pending = await get_pending_store().get(rid)
        # Either the sources' own deadline got there first (answered,
        # unexpanded) or the fan-out's did (failed, and saying why) -- at an
        # exact tie both are right; what must not happen is an expansion.
        assert seen["calls"] == 0
        if pending.status == STATUS_COMPLETE:
            assert pending.unexpanded is True
        else:
            assert pending.status == STATUS_FAILED
            assert "atom fan-out did not finish within 0.9 s" in pending.error_message
        _nothing_left_behind(pool)


async def _answer() -> str:
    return _ANSWER


class TestAFanOutOverTheDeadlineFailsAndSaysSo:
    async def test_the_reason_names_the_step_and_the_deadline(
        self, responder, maker, pairing, far_side, pool,  # noqa: F811
        a_model_server_that_never_answers, monkeypatch,
    ):
        """A source allowed to run longer than the fan-out may -- the
        configuration the startup check refuses, staged here because it is
        the only way the fan-out itself can run over. Two sources sit in a
        REAL statement, each on a session of its own, when the deadline
        comes."""
        import services.document_fact_retrieval as fact_mod
        import services.note_retrieval as note_mod

        _deadlines(monkeypatch, source=20.0, step=1.0)
        in_statement = {"n": 0}

        async def _hangs(self, query, *, asker_id, top_k, peer=None):
            in_statement["n"] += 1
            await self.db.execute(text("SELECT pg_sleep(30) /* slow-source */"))
            return []

        monkeypatch.setattr(note_mod.NoteRetrieval, "search", _hangs)
        monkeypatch.setattr(fact_mod.DocumentFactRetrieval, "search", _hangs)
        seen = _expansion(monkeypatch, lambda db: asyncio.sleep(0))
        synthesized = []
        responder._synthesize = lambda query, matches: synthesized.append(1) or _answer()
        rid, finish = await _admit(responder, far_side)

        t0 = time.perf_counter()
        await asyncio.wait_for(finish(), 20)
        took = time.perf_counter() - t0

        assert in_statement["n"] == 2 and pool.book["peak"] >= 2
        pending = await get_pending_store().get(rid)
        assert pending.status == STATUS_FAILED
        assert pending.error_message == (
            "RetrievalDeadlineExceeded: atom fan-out did not finish within 0.9 s")
        assert pending.retrieved_count is None and pending.answer is None
        assert synthesized == [] and seen["calls"] == 0

        (row,) = await _answered_rows(maker)
        assert (row.final_status, row.answer_excerpt) == ("failed", None)
        assert row.error_message == pending.error_message

        assert took < 5, f"the failure waited for the statement: {took:.1f} s"
        _nothing_left_behind(pool)
        await _no_statement_still_runs(pool, "slow-source")


class TestOnlyTheStoresOwnDeadlineIsABudgetRunningOut:
    async def test_a_timeout_raised_inside_expansion_is_a_failure_of_its_own(
        self, responder, maker, pairing, far_side, pool,  # noqa: F811
        a_model_server_that_never_answers, monkeypatch,
    ):
        """A TimeoutError from further down (a driver, a pool) is not "the
        budget ran out": the budget had seconds left. Passing it off as an
        unexpanded answer would hide a defect behind a degraded result."""
        _deadlines(monkeypatch, source=0.6, step=6.0)

        async def raises_a_timeout_of_its_own(db):
            await db.execute(text("SELECT 1"))
            raise TimeoutError("pool exhausted")

        seen = _expansion(monkeypatch, raises_a_timeout_of_its_own)
        responder._synthesize = lambda query, matches: _answer()
        rid, finish = await _admit(responder, far_side)

        t0 = time.perf_counter()
        with _Warnings() as warnings:
            await asyncio.wait_for(finish(), 20)

        assert seen["calls"] == 1
        assert time.perf_counter() - t0 < 4, "this waited for a deadline"
        pending = await get_pending_store().get(rid)
        assert pending.status == STATUS_FAILED
        assert pending.error_message == "TimeoutError: pool exhausted"
        assert pending.unexpanded is False
        assert not [w for w in warnings.lines if "UNEXPANDED" in w]
        _nothing_left_behind(pool)
