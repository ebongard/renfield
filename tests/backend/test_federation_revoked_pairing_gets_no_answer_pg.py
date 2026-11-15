"""A revoked pairing gets no answer -- real Postgres.

Why this file exists (2026-11-15): a revocation purges the pending requests of
that far side, but the background task checked for its record only at its
start and at its end called `store.save(pending)`, which re-inserted the
record in both store backends. `handle_retrieve` never looked the pairing up
again. So a key revoked while its query was queued or in flight could still
poll a COMPLETE answer, and the data was read after the revocation.

What is pinned down here:

- revoked before the task got to work  -> no retrieval, nothing to poll;
- revoked during retrieval / synthesis -> nothing to poll, no record back;
- a record that exists anyway          -> the poll refuses it, and the refusal
  is the one an unknown request gets (no oracle);
- the audit row says what happened, with the same dividing line a
  cancellation has: read or not read;
- an ACTIVE pairing is still answered (the negative control).

Applies only with `RENFIELD_TEST_PG_URL` (skipped otherwise).
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models.database import FederationQueryLog, PeerUser, Role, User
from services.federation_identity import (
    FederationIdentity,
    init_federation_identity,
    reset_federation_identity_for_tests,
)
from services.federation_pending_store import (
    _PendingRequest,
    get_pending_store,
    reset_store_for_tests,
)
from services.federation_query_schemas import (
    STATUS_COMPLETE,
    STATUS_EXPIRED,
    QueryBrainInitiateRequest,
    QueryBrainRetrieveRequest,
)
from services.pairing_service import _canonical_bytes

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

_ANSWER = "the answer nobody may collect"


@pytest.fixture
def far_side() -> FederationIdentity:
    return FederationIdentity(ed25519.Ed25519PrivateKey.generate())


@pytest.fixture
async def maker(async_engine, own_session_to_test_db):
    return async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
async def pairing(maker, far_side) -> dict:
    """One local owner and one ACTIVE pairing with the far side -- committed,
    because the responder reads on sessions of its own."""
    async with maker() as seed:
        role = Role(name="revoke-role", description="t", permissions=[])
        seed.add(role)
        await seed.flush()
        owner = User(username="revoke-owner", password_hash="x", is_active=True, role_id=role.id)
        seed.add(owner)
        await seed.flush()
        peer = PeerUser(
            circle_owner_id=owner.id, remote_pubkey=far_side.public_key_hex(),
            remote_display_name="far side", remote_user_id=555,
            granted_tier=2, transport_config={},
        )
        seed.add(peer)
        await seed.commit()
        return {"owner": owner.id, "peer": peer.id}


@pytest.fixture
async def responder(tmp_path, maker):
    from services.federation_query_responder import (
        FederationQueryResponder,
        _clear_state_for_tests,
    )

    reset_federation_identity_for_tests()
    init_federation_identity(tmp_path / "responder_key")
    reset_store_for_tests()
    await _clear_state_for_tests()
    async with maker() as request_session:
        yield FederationQueryResponder(db=request_session)
    reset_federation_identity_for_tests()
    await _clear_state_for_tests()
    reset_store_for_tests()


def _request(pairing: dict, far_side: FederationIdentity, rid: str) -> _PendingRequest:
    return _PendingRequest(
        request_id=rid, asker_pubkey=far_side.public_key_hex(),
        peer_user_id=pairing["peer"], asker_local_user_id=None,
        max_visible_tier=2, query="what do you know?",
        initiated_at=time.time(), peer_owner_id=pairing["owner"], mapped=False,
        peer_display_name="far side",
    )


async def _admit(responder, far_side: FederationIdentity):
    """A request through the REAL entry point. Returns its id and a
    coroutine function that lets the background task run to its end.

    Until that is called the task is held at its very first step -- the
    QUEUED state, staged instead of left to the scheduler: a revocation in
    the test awaits the database, and the task would otherwise start working
    in exactly that moment.
    """
    import asyncio
    import secrets

    gate = asyncio.Event()
    run_query = responder._run_query

    async def held(*args):
        await gate.wait()
        await run_query(*args)

    responder._run_query = held

    import services.federation_query_responder as fqr

    me = far_side.public_key_hex()
    unsigned = {
        "version": 2, "asker_pubkey": me, "query": "what do you know?",
        "nonce": secrets.token_hex(16), "timestamp": int(time.time()),
        "depth": 3, "path": [me],
    }
    before = set(fqr._background_tasks)
    resp = await responder.handle_initiate(QueryBrainInitiateRequest(
        **unsigned, signature=far_side.sign(_canonical_bytes(unsigned)).hex()))
    (task,) = set(fqr._background_tasks) - before

    async def finish() -> None:
        gate.set()
        await task

    return resp.request_id, finish


def _poll(far_side: FederationIdentity, rid: str) -> QueryBrainRetrieveRequest:
    unsigned = {
        "version": 1, "request_id": rid,
        "asker_pubkey": far_side.public_key_hex(), "timestamp": int(time.time()),
    }
    return QueryBrainRetrieveRequest(
        **unsigned, signature=far_side.sign(_canonical_bytes(unsigned)).hex())


async def _revoke(maker, pairing: dict, far_side: FederationIdentity, *, purge: bool) -> None:
    """What the revoke route does: commit the revocation, THEN purge."""
    from services.federation_query_responder import purge_requests_for_pubkey

    async with maker() as db:
        await db.execute(
            update(PeerUser).where(PeerUser.id == pairing["peer"])
            .values(revoked_at=datetime.now(UTC).replace(tzinfo=None)))
        await db.commit()
    if purge:
        await purge_requests_for_pubkey(far_side.public_key_hex())


async def _answered_rows(maker) -> list[FederationQueryLog]:
    async with maker() as db:
        return list((await db.execute(
            select(FederationQueryLog)
            .where(FederationQueryLog.direction == "answered")
            .order_by(FederationQueryLog.id)
        )).scalars().all())


def _stub_work(responder, *, during_retrieval=None, during_synthesis=None) -> dict:
    """Retrieval and synthesis without a model server. `calls` counts them;
    the hooks run INSIDE the step, where a revocation would arrive."""
    calls = {"retrieve": 0, "synthesize": 0}

    async def retrieve(session, pending):
        calls["retrieve"] += 1
        if during_retrieval:
            await during_retrieval()
        return [
            SimpleNamespace(
                atom=SimpleNamespace(atom_id=f"atom-{i}", atom_type="memory"),
                snippet=f"snippet {i}", score=1.0 / (i + 1))
            for i in range(2)
        ]

    async def synthesize(query, matches):
        calls["synthesize"] += 1
        if during_synthesis:
            await during_synthesis()
        return _ANSWER

    responder._retrieve = retrieve
    responder._synthesize = synthesize
    return calls


async def _unknown_poll_response(responder, far_side) -> dict:
    return (await responder.handle_retrieve(_poll(far_side, "no-such-request"))).model_dump()


class TestAnActivePairingIsStillAnswered:
    async def test_the_negative_control(self, responder, maker, pairing, far_side):
        """Without this, every test below would also pass on a responder that
        answers nobody."""
        calls = _stub_work(responder)
        rid, finish = await _admit(responder, far_side)

        await finish()

        assert calls == {"retrieve": 1, "synthesize": 1}
        resp = await responder.handle_retrieve(_poll(far_side, rid))
        assert resp.status == STATUS_COMPLETE
        assert resp.answer == _ANSWER
        rows = await _answered_rows(maker)
        assert [r.final_status for r in rows] == ["success"]


class TestRevokedBeforeTheTaskGotToWork:
    async def test_purged_while_queued_nothing_is_read(
        self, responder, maker, pairing, far_side
    ):
        """Revocation and purge land between `initiate` and the task's first
        step -- the state a query is in while it waits for the event loop."""
        calls = _stub_work(responder)
        rid, finish = await _admit(responder, far_side)
        await _revoke(maker, pairing, far_side, purge=True)

        await finish()

        assert calls == {"retrieve": 0, "synthesize": 0}
        assert await get_pending_store().get(rid) is None
        rows = await _answered_rows(maker)
        assert [r.final_status for r in rows] == ["aborted"]
        assert "vor dem Abruf" in rows[0].error_message
        assert rows[0].answer_excerpt is None

    async def test_revoked_in_the_database_but_not_yet_purged(
        self, responder, maker, pairing, far_side
    ):
        """The purge runs AFTER the revocation commits. In that gap the
        record still exists -- the database decides."""
        calls = _stub_work(responder)
        rid, finish = await _admit(responder, far_side)
        await _revoke(maker, pairing, far_side, purge=False)

        await finish()

        assert calls == {"retrieve": 0, "synthesize": 0}
        assert await get_pending_store().get(rid) is None
        assert [r.final_status for r in await _answered_rows(maker)] == ["aborted"]


class TestRevokedWhileInFlight:
    async def test_during_retrieval_no_answer_and_no_record_comes_back(
        self, responder, maker, pairing, far_side
    ):
        async def revoke_now():
            await _revoke(maker, pairing, far_side, purge=True)

        _stub_work(responder, during_retrieval=revoke_now)
        rid, finish = await _admit(responder, far_side)

        await finish()

        # 🛑 The defect: `save` re-inserted the purged record as COMPLETE.
        assert await get_pending_store().get(rid) is None
        resp = await responder.handle_retrieve(_poll(far_side, rid))
        assert resp.model_dump() == await _unknown_poll_response(responder, far_side)
        assert resp.status == STATUS_EXPIRED and resp.answer is None

        rows = await _answered_rows(maker)
        assert [r.final_status for r in rows] == ["aborted"]
        # Data WAS read before the revocation arrived; the row says so.
        assert "nach dem Abruf" in rows[0].error_message
        assert "2 Treffer" in rows[0].error_message
        assert rows[0].answer_excerpt is None

    async def test_during_synthesis_the_finished_answer_is_not_stored(
        self, responder, maker, pairing, far_side
    ):
        async def revoke_now():
            await _revoke(maker, pairing, far_side, purge=True)

        calls = _stub_work(responder, during_synthesis=revoke_now)
        rid, finish = await _admit(responder, far_side)

        await finish()

        assert calls == {"retrieve": 1, "synthesize": 1}
        assert await get_pending_store().get(rid) is None
        rows = await _answered_rows(maker)
        assert [r.final_status for r in rows] == ["aborted"]
        assert all(_ANSWER not in (r.answer_excerpt or "") for r in rows)

    async def test_a_failing_query_does_not_bring_the_record_back_either(
        self, responder, maker, pairing, far_side
    ):
        """The failure branch saves too."""
        async def revoke_then_break():
            await _revoke(maker, pairing, far_side, purge=True)
            raise RuntimeError("broken after the revocation")

        _stub_work(responder, during_retrieval=revoke_then_break)
        rid, finish = await _admit(responder, far_side)

        await finish()

        assert await get_pending_store().get(rid) is None


class TestThePollChecksThePairingItself:
    async def test_a_complete_record_of_a_revoked_pairing_is_refused_like_an_unknown_one(
        self, responder, maker, pairing, far_side
    ):
        """Whatever put the record there -- stored just after the purge, a
        second worker, a future bug -- the poll does not hand it out."""
        pending = _request(pairing, far_side, "late-1")
        pending.status = STATUS_COMPLETE
        pending.answer = _ANSWER
        pending.answered_at = time.time()
        await get_pending_store().put(pending)
        await _revoke(maker, pairing, far_side, purge=False)

        resp = await responder.handle_retrieve(_poll(far_side, "late-1"))

        assert resp.model_dump() == await _unknown_poll_response(responder, far_side)
        assert _ANSWER not in resp.model_dump_json()
        assert await get_pending_store().get("late-1") is None

    async def test_the_same_record_on_an_active_pairing_is_handed_out(
        self, responder, maker, pairing, far_side
    ):
        """Negative control for the test above."""
        pending = _request(pairing, far_side, "late-2")
        pending.status = STATUS_COMPLETE
        pending.answer = _ANSWER
        pending.answered_at = time.time()
        await get_pending_store().put(pending)

        resp = await responder.handle_retrieve(_poll(far_side, "late-2"))
        assert resp.status == STATUS_COMPLETE and resp.answer == _ANSWER
