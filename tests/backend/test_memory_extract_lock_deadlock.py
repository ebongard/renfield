"""Regression tests for the v2-extract advisory-lock deadlock (prod: 655
deadlocks in 5 days on reva-prod, 5-7/h).

The bug
-------
``ConversationMemoryService.extract_and_save_v2`` ran three phases:

  Phase 1  session-level ``pg_advisory_lock`` -> MemoryRetrieval.retrieve()
           -> that UPDATEs access_count/last_accessed_at and only flush()es
           -> ``pg_advisory_unlock``
  Phase 2  the LLM call (seconds), transaction still open
  Phase 3  ``pg_advisory_lock`` again -> drift check + apply

The advisory lock was released at the end of Phase 1, but the ROW locks
taken by the access-tracking UPDATE live until the CALLER commits. So
worker B could take the free advisory lock and then block on worker A's
row locks, while worker A blocked on B for the advisory lock -> cycle ->
``DeadlockDetected``.

The fix (three parts, all asserted below)
-----------------------------------------
1. ``MemoryRetrieval.retrieve`` / ``retrieve_essential`` gained
   ``track_access`` (default True = every existing caller unchanged).
2. The extract pipeline retrieves with ``track_access=False``, so Phase 1
   takes no row locks at all and needs no lock.
3. Phase 3 takes a TRANSACTION-scoped ``pg_advisory_xact_lock``, which
   cannot be released while its own row locks still stand -> a cycle is
   structurally impossible.

Access-count semantics
----------------------
Phase 1's access write was also REDUNDANT: Phase 3 bumped the retrieved-
but-untouched candidates again, and the Phase-3 drift re-retrieve a third
time, so ``access_count`` was inflated 2-3x per extraction. Since
``last_accessed_at`` feeds both the recency-aware ranker AND the
context/confidence decay in ``ConversationMemoryService.cleanup``, a
retrieved candidate must be bumped EXACTLY ONCE per extraction on every
terminal path -- never 0, never 3. One test per exit path below.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.conversation_memory_service import ConversationMemoryService
from services.memory_ops import MemoryOpsList
from services.memory_retrieval import MemoryRetrieval

# A turn that passes should_extract_memories (no injection / transactional
# pattern) so every test below reaches the phases under test.
_USER_MSG = "Ich mag Jazz-Musik sehr gerne"
_ASSISTANT_MSG = "Notiert."

_CANDIDATE_ID = 101


def _row(mem_id: int = _CANDIDATE_ID, similarity: float = 0.95) -> SimpleNamespace:
    """A conversation_memories row as the raw-SQL retrieval reads it."""
    return SimpleNamespace(
        id=mem_id,
        content="mag Jazz",
        category="fact",
        importance=0.8,
        confidence=1.0,
        access_count=0,
        created_at=None,
        last_accessed_at=None,
        subject_name=None,
        subject_entity_id=None,
        similarity=similarity,
    )


class _FakeSavepoint:
    def __init__(self):
        self.rolled_back = False
        self.committed = False

    async def rollback(self):
        self.rolled_back = True

    async def commit(self):
        self.committed = True


class _RecordingSession:
    """Minimal AsyncSession stand-in that records every executed statement.

    Returns ``rows`` for the retrieval SELECT and nothing for anything else,
    so the assertions can count access-tracking UPDATEs and advisory-lock
    statements precisely.
    """

    def __init__(self, rows: list | None = None):
        self.rows = rows if rows is not None else []
        self.statements: list[str] = []
        self.flushes = 0
        self.commits = 0
        self.savepoints: list[_FakeSavepoint] = []

    async def execute(self, sql, params=None):
        rendered = getattr(sql, "text", None) or str(sql)
        self.statements.append(rendered)
        result = MagicMock()
        if "FROM conversation_memories" in rendered:
            result.fetchall = MagicMock(return_value=list(self.rows))
        else:
            result.fetchall = MagicMock(return_value=[])
        result.rowcount = 1
        return result

    async def flush(self):
        self.flushes += 1

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass

    def add(self, _obj):
        pass

    async def begin_nested(self):
        sp = _FakeSavepoint()
        self.savepoints.append(sp)
        return sp

    # --- assertion helpers -------------------------------------------------

    @property
    def access_bumps(self) -> list[str]:
        """Every statement that increments access_count."""
        return [
            s for s in self.statements
            if s.lstrip().upper().startswith("UPDATE CONVERSATION_MEMORIES")
            and "access_count" in s
        ]

    @property
    def advisory_statements(self) -> list[str]:
        return [s for s in self.statements if "pg_advisory" in s]


def _service(session: _RecordingSession) -> ConversationMemoryService:
    svc = ConversationMemoryService(session)
    return svc


def _noop_ops() -> MemoryOpsList:
    return MemoryOpsList(root=[{"op": "NOOP"}])


@pytest.fixture
def patched_embedding(monkeypatch):
    monkeypatch.setattr(
        MemoryRetrieval, "_get_embedding", AsyncMock(return_value=[0.1] * 8)
    )


# ===========================================================================
# 1. track_access on the retrieval surface
# ===========================================================================

class TestTrackAccessParameter:
    pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

    async def test_retrieve_tracks_access_by_default(self, patched_embedding):
        """Default True — chat/agent recall MUST keep tracking access."""
        session = _RecordingSession(rows=[_row()])
        hits = await MemoryRetrieval(session).retrieve("was mag ich", threshold=0.0)

        assert [h["id"] for h in hits] == [_CANDIDATE_ID]
        assert len(session.access_bumps) == 1

    async def test_retrieve_with_track_access_false_issues_no_update(
        self, patched_embedding
    ):
        """track_access=False takes NO row locks — the whole point of the fix."""
        session = _RecordingSession(rows=[_row()])
        hits = await MemoryRetrieval(session).retrieve(
            "was mag ich", threshold=0.0, track_access=False
        )

        # Results are unchanged; only the write side is suppressed.
        assert [h["id"] for h in hits] == [_CANDIDATE_ID]
        assert session.access_bumps == []
        assert session.flushes == 0

    async def test_retrieve_essential_tracks_access_by_default(self):
        session = _RecordingSession(rows=[_row()])
        hits = await MemoryRetrieval(session).retrieve_essential()

        assert [h["id"] for h in hits] == [_CANDIDATE_ID]
        assert len(session.access_bumps) == 1

    async def test_retrieve_essential_with_track_access_false_issues_no_update(self):
        session = _RecordingSession(rows=[_row()])
        hits = await MemoryRetrieval(session).retrieve_essential(track_access=False)

        assert [h["id"] for h in hits] == [_CANDIDATE_ID]
        assert session.access_bumps == []


# ===========================================================================
# 2. Lock semantics — the deadlock fix proper
# ===========================================================================

class TestExtractV2LockSemantics:
    pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

    async def _run_success(self, session, monkeypatch):
        svc = _service(session)
        svc._call_extract_v2_llm = AsyncMock(return_value=_noop_ops())
        return await svc.extract_and_save_v2(
            user_message=_USER_MSG,
            assistant_response=_ASSISTANT_MSG,
            user_id=7,
            session_id="s1",
            lang="de",
        )

    async def test_phase1_takes_no_session_level_lock(
        self, patched_embedding, monkeypatch
    ):
        """No pg_advisory_lock / pg_advisory_unlock anywhere in the run.

        A session-level lock is what made the cycle possible: it can be
        released while the row locks it was meant to guard still stand.
        """
        session = _RecordingSession(rows=[_row()])
        await self._run_success(session, monkeypatch)

        assert not [s for s in session.advisory_statements if "pg_advisory_lock(" in s]
        assert not [s for s in session.advisory_statements if "pg_advisory_unlock" in s]

    async def test_phase3_uses_transaction_scoped_lock(
        self, patched_embedding, monkeypatch
    ):
        """Exactly one pg_advisory_xact_lock, released by the transaction."""
        session = _RecordingSession(rows=[_row()])
        await self._run_success(session, monkeypatch)

        xact = [s for s in session.advisory_statements if "pg_advisory_xact_lock" in s]
        assert len(xact) == 1, session.advisory_statements

    async def test_lock_is_acquired_before_any_row_lock(
        self, patched_embedding, monkeypatch
    ):
        """Ordering invariant: the xact lock precedes every access UPDATE.

        A transaction that takes row locks BEFORE requesting the advisory
        lock can be the second party of a cycle. Phase 1 must therefore be
        lock-free AND write-free.
        """
        session = _RecordingSession(rows=[_row()])
        await self._run_success(session, monkeypatch)

        lock_at = next(
            i for i, s in enumerate(session.statements)
            if "pg_advisory_xact_lock" in s
        )
        bump_at = [
            i for i, s in enumerate(session.statements)
            if s in session.access_bumps
        ]
        assert all(i > lock_at for i in bump_at), session.statements

    async def test_no_lock_taken_for_anonymous_user(
        self, patched_embedding, monkeypatch
    ):
        """user_id=None has no per-user lock key — unchanged behaviour."""
        session = _RecordingSession(rows=[_row()])
        svc = _service(session)
        svc._call_extract_v2_llm = AsyncMock(return_value=_noop_ops())
        await svc.extract_and_save_v2(
            user_message=_USER_MSG,
            assistant_response=_ASSISTANT_MSG,
            user_id=None,
            lang="de",
        )
        assert session.advisory_statements == []


# ===========================================================================
# 3. access_count is bumped EXACTLY ONCE per extraction — one test per path
# ===========================================================================

class TestAccessBumpExactlyOncePerPath:
    """``last_accessed_at`` feeds the recency ranker AND cleanup's decay, so
    a retrieved-but-untouched candidate must be counted as used exactly once
    per extraction on every terminal path — never 0, never 2-3."""

    pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

    async def test_path_a_success(self, patched_embedding):
        """(a) ops applied cleanly — was 3x (phase 1 + drift re-retrieve +
        explicit bump), must be 1x."""
        session = _RecordingSession(rows=[_row()])
        svc = _service(session)
        svc._call_extract_v2_llm = AsyncMock(return_value=_noop_ops())

        await svc.extract_and_save_v2(
            user_message=_USER_MSG, assistant_response=_ASSISTANT_MSG,
            user_id=7, session_id="s1", lang="de",
        )

        assert len(session.access_bumps) == 1, session.statements

    async def test_path_b_drift_reject_falls_back_to_v1(self, patched_embedding):
        """(b) drift-reject -> v1 fallback. Was 2x (phase 1 + re-retrieve);
        must still be 1x, NOT 0 — the candidates were read this turn."""
        session = _RecordingSession(rows=[_row()])
        svc = _service(session)
        # target_id 999 is not in the candidate set -> id_reject
        svc._call_extract_v2_llm = AsyncMock(return_value=MemoryOpsList(
            root=[{"op": "UPDATE", "target_id": 999, "content": "neu"}]
        ))
        svc._extract_and_save_v1_impl = AsyncMock(return_value=["v1"])

        result = await svc.extract_and_save_v2(
            user_message=_USER_MSG, assistant_response=_ASSISTANT_MSG,
            user_id=7, session_id="s1", lang="de",
        )

        assert result == ["v1"]
        svc._extract_and_save_v1_impl.assert_called_once()
        assert len(session.access_bumps) == 1, session.statements

    async def test_path_c_llm_schema_reject_falls_back_to_v1(self, patched_embedding):
        """(c) LLM/schema reject -> v1 fallback. Was 1x via the phase-1
        write; with phase 1 write-free it must be restored explicitly."""
        session = _RecordingSession(rows=[_row()])
        svc = _service(session)
        svc._call_extract_v2_llm = AsyncMock(return_value=None)
        svc._extract_and_save_v1_impl = AsyncMock(return_value=["v1"])

        result = await svc.extract_and_save_v2(
            user_message=_USER_MSG, assistant_response=_ASSISTANT_MSG,
            user_id=7, session_id="s1", lang="de",
        )

        assert result == ["v1"]
        assert len(session.access_bumps) == 1, session.statements

    async def test_path_c_bump_precedes_the_v1_fallback(self, patched_embedding):
        """The fallback bump must be issued BEFORE v1 runs: v1 commits
        internally, and that commit is what persists the bump (the caller —
        chat_handler's ``async with AsyncSessionLocal()`` — does not commit)."""
        session = _RecordingSession(rows=[_row()])
        svc = _service(session)
        svc._call_extract_v2_llm = AsyncMock(return_value=None)

        bumps_when_v1_ran: list[int] = []

        async def _v1(**_kwargs):
            bumps_when_v1_ran.append(len(session.access_bumps))
            return ["v1"]

        svc._extract_and_save_v1_impl = _v1

        await svc.extract_and_save_v2(
            user_message=_USER_MSG, assistant_response=_ASSISTANT_MSG,
            user_id=7, session_id="s1", lang="de",
        )

        assert bumps_when_v1_ran == [1]

    async def test_path_d_shadow_bump_is_rolled_back(self, patched_embedding):
        """(d) shadow mode: exactly one bump is issued, but inside the
        savepoint that gets rolled back — shadow must never mutate prod
        state. 0 persisted is correct here, by design."""
        session = _RecordingSession(rows=[_row()])
        svc = _service(session)
        svc._call_extract_v2_llm = AsyncMock(return_value=_noop_ops())

        await svc._extract_v2_shadow_only(
            user_message=_USER_MSG, assistant_response=_ASSISTANT_MSG,
            user_id=7, session_id="s1", lang="de",
            v1_outcome="noop", v1_extracted_count=0, v1_latency_seconds=0.1,
        )

        assert len(session.access_bumps) == 1, session.statements
        # The extraction savepoint (the first one opened) was rolled back.
        assert session.savepoints[0].rolled_back is True

    async def test_path_e_exception_between_phases_bumps_nothing(
        self, patched_embedding
    ):
        """(e) an exception in the LLM phase aborts the extraction. The turn
        is not 'used', and the caller's transaction is abandoned — 0 bumps,
        and crucially no half-applied double bump."""
        session = _RecordingSession(rows=[_row()])
        svc = _service(session)
        svc._call_extract_v2_llm = AsyncMock(side_effect=RuntimeError("llm down"))

        with pytest.raises(RuntimeError):
            await svc.extract_and_save_v2(
                user_message=_USER_MSG, assistant_response=_ASSISTANT_MSG,
                user_id=7, session_id="s1", lang="de",
            )

        assert session.access_bumps == []

    async def test_touched_row_is_not_double_bumped(self, patched_embedding):
        """A row the ops UPDATE already carries its own access_count+1 in
        _apply_update_v2, so it must be excluded from the untouched bump."""
        session = _RecordingSession(rows=[_row()])
        svc = _service(session)
        svc._call_extract_v2_llm = AsyncMock(return_value=MemoryOpsList(
            root=[{"op": "UPDATE", "target_id": _CANDIDATE_ID, "content": "neu"}]
        ))
        svc._get_embedding = AsyncMock(return_value=[0.1] * 8)
        svc._record_history = AsyncMock(return_value=None)

        await svc.extract_and_save_v2(
            user_message=_USER_MSG, assistant_response=_ASSISTANT_MSG,
            user_id=7, session_id="s1", lang="de",
        )

        # Exactly one: the UPDATE op itself. No second, untouched-path bump.
        assert len(session.access_bumps) == 1, session.statements


# ===========================================================================
# 4. The real thing: two concurrent extractions for the SAME user
# ===========================================================================

class TestConcurrentExtractionOnRealPostgres:
    """Reproduces the production deadlock against a real Postgres.

    Pre-fix this raises ``DeadlockDetected`` (or hangs into the timeout);
    post-fix both extractions complete and each candidate is counted once
    per extraction.
    """

    pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

    async def test_two_concurrent_extractions_same_user_do_not_deadlock(
        self, pg_async_engine, monkeypatch
    ):
        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from models.database import (
            EMBEDDING_DIMENSION,
            ConversationMemory,
            Role,
            User,
        )

        maker = async_sessionmaker(
            pg_async_engine, class_=AsyncSession, expire_on_commit=False
        )
        vec = [0.0] * EMBEDDING_DIMENSION
        vec[3] = 1.0

        async with maker() as seed:
            role = Role(name="memlock_role")
            seed.add(role)
            await seed.flush()
            user = User(
                username="memlock_user", email="memlock@ex.test",
                password_hash="x", role_id=role.id, is_active=True,
            )
            seed.add(user)
            await seed.flush()
            mems = [
                ConversationMemory(
                    user_id=user.id, content=f"Fakt {i}", category="fact",
                    importance=0.8, circle_tier=0, embedding=vec, access_count=0,
                )
                for i in range(3)
            ]
            for m in mems:
                seed.add(m)
            await seed.commit()
            user_id = user.id
            mem_ids = [m.id for m in mems]

        monkeypatch.setattr(
            MemoryRetrieval, "_get_embedding", AsyncMock(return_value=vec)
        )

        a_reached_phase2 = asyncio.Event()

        async def _worker(name: str, llm_hook):
            async with maker() as db:
                svc = ConversationMemoryService(db)
                svc._call_extract_v2_llm = llm_hook
                result = await svc.extract_and_save_v2(
                    user_message=_USER_MSG,
                    assistant_response=_ASSISTANT_MSG,
                    user_id=user_id,
                    session_id=f"memlock-{name}",
                    lang="de",
                )
                await db.commit()
                return result

        async def _llm_a(**_kwargs):
            # A has finished Phase 1 and holds whatever Phase 1 took.
            a_reached_phase2.set()
            # Give B time to enter its own Phase 1 / Phase 3 and (pre-fix)
            # grab the freed session-level advisory lock.
            await asyncio.sleep(1.5)
            return _noop_ops()

        async def _llm_b(**_kwargs):
            return _noop_ops()

        async def _run_b():
            await a_reached_phase2.wait()
            return await _worker("b", _llm_b)

        await asyncio.wait_for(
            asyncio.gather(_worker("a", _llm_a), _run_b()), timeout=60
        )

        async with maker() as check:
            rows = (
                await check.execute(
                    select(ConversationMemory.id, ConversationMemory.access_count)
                    .where(ConversationMemory.id.in_(mem_ids))
                )
            ).all()

        # Two extractions, each candidate read by both -> exactly 2.
        assert {r.access_count for r in rows} == {2}, dict(
            (r.id, r.access_count) for r in rows
        )
