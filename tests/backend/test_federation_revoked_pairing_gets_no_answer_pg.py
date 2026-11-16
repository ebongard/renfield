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

The same for a revoked PERSON LINK (2026-11-16, second half of the file). The
link is the widest reach there is -- the request runs AS a local person -- and
only the pairing was asked about again: `resolve_linked_user` ran once, at
`initiate`. A link revoked after that was covered by the purge alone, and the
purge misses a record stored after it (link resolved, link revoked and purged,
record `put`). Pinned down there: every one of those moments, both pending
store backends, and that the request is REFUSED -- it does not come back
answered on the pairing's tier.

Applies only with `RENFIELD_TEST_PG_URL` (skipped otherwise).
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models.database import FederationQueryLog, FederationUserLink, PeerUser, Role, User
from services.federation_audit import (
    STAGE_AFTER_ANSWER,
    STAGE_AFTER_READ,
    STAGE_BEFORE_READ,
    outcome_code,
)
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
from services.federation_query_responder import _REFUSED_GONE as GONE
from services.federation_query_responder import _REFUSED_LINK as LINK
from services.federation_query_responder import _REFUSED_PAIRING as PAIRING
from services.federation_query_responder import _undelivered_note, _unserved_note
from services.federation_query_schemas import (
    STATUS_COMPLETE,
    STATUS_EXPIRED,
    QueryBrainInitiateRequest,
    QueryBrainRetrieveRequest,
)
from services.pairing_service import _canonical_bytes
from utils.config import settings

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


def _what_happened(row: FederationQueryLog) -> tuple[str, str | None] | None:
    """The outcome of an audit row as its CODE -- cause and stage. The German
    sentence behind it is presentation and is not what these tests go by."""
    return outcome_code(row.error_message)


def _note(code: str, *, read: int | None) -> str:
    """The exact text the responder stores for `code` -- built by the
    responder's own function, never spelled out here."""
    return _unserved_note(code, SimpleNamespace(retrieved_count=read))

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


async def _admit(responder, far_side: FederationIdentity, *, querier_ref: str | None = None):
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
    if querier_ref is not None:
        unsigned["querier_ref"] = querier_ref
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
        assert _what_happened(rows[0]) == (PAIRING, STAGE_BEFORE_READ)
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
        # Data WAS read before the revocation arrived; the row says so --
        # in its code, and in a sentence that names the count.
        assert _what_happened(rows[0]) == (PAIRING, STAGE_AFTER_READ)
        assert rows[0].error_message == _note(PAIRING, read=2)
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

    async def test_purged_between_the_last_look_and_the_save(
        self, responder, maker, pairing, far_side, monkeypatch
    ):
        """The narrowest window: the record is there at the last check and
        gone when the result is stored. `save` does not create, so the
        answer is not stored, and the row says it was read."""
        import services.federation_query_responder as fqr

        look = fqr._refusal_for
        looks = {"n": 0}

        async def look_then_purge(pending):
            why = await look(pending)
            looks["n"] += 1
            if looks["n"] == 3:       # start, before synthesis, before the save
                await fqr.purge_requests_for_pubkey(far_side.public_key_hex())
            return why

        monkeypatch.setattr(fqr, "_refusal_for", look_then_purge)
        _stub_work(responder)
        rid, finish = await _admit(responder, far_side)

        await finish()

        assert looks["n"] == 3
        assert await get_pending_store().get(rid) is None
        rows = await _answered_rows(maker)
        assert [r.final_status for r in rows] == ["aborted"]
        # The answer had been made: its row is corrected, not left as
        # "answered" and not doubled.
        assert _what_happened(rows[0]) == (GONE, STAGE_AFTER_ANSWER)
        assert rows[0].answer_excerpt is None

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


# --------------------------------------------------------------------------- #
# A revoked PERSON LINK gets no answer either
# --------------------------------------------------------------------------- #

_REF = "ab" * 32


@pytest.fixture(params=["memory", "redis"])
async def linked(request, maker, pairing, responder, monkeypatch) -> dict:
    """The pairing plus an ACTIVE local person linked to the far side's
    `querier_ref`, links switched on -- for BOTH pending store backends.

    `responder` is requested so the store is replaced AFTER its reset.
    """
    import services.federation_pending_store as fps
    from tests.backend.test_federation_pending_store import FakeRedis

    monkeypatch.setattr(settings, "federation_identity_links_enabled", True)
    monkeypatch.setattr(settings, "auth_enabled", True)
    if request.param == "redis":
        monkeypatch.setattr(fps, "_store", fps.RedisPendingStore(FakeRedis()))

    async with maker() as seed:
        role_id = (await seed.execute(select(Role.id))).scalars().first()
        person = User(username="linked-person", password_hash="x", is_active=True,
                      role_id=role_id)
        seed.add(person)
        await seed.flush()
        link = FederationUserLink(
            peer_id=pairing["peer"], querier_ref=_REF, local_user_id=person.id)
        seed.add(link)
        await seed.commit()
        return {**pairing, "person": person.id, "link": link.id}


async def _revoke_link(maker, linked: dict, far_side: FederationIdentity, *, purge: bool) -> None:
    """What both link routes do: delete the row, commit, THEN purge."""
    from services.federation_query_responder import purge_requests_for_pubkey

    async with maker() as db:
        await db.execute(delete(FederationUserLink).where(FederationUserLink.id == linked["link"]))
        await db.commit()
    if purge:
        await purge_requests_for_pubkey(far_side.public_key_hex())


def _mapped_complete(linked: dict, far_side: FederationIdentity, rid: str) -> _PendingRequest:
    return _PendingRequest(
        request_id=rid, asker_pubkey=far_side.public_key_hex(),
        peer_user_id=linked["peer"], asker_local_user_id=linked["person"],
        max_visible_tier=0, query="what do you know?", initiated_at=time.time(),
        peer_owner_id=None, mapped=True, link_id=linked["link"],
        peer_display_name="far side", status=STATUS_COMPLETE, answer=_ANSWER,
        answered_at=time.time(),
    )


class TestAStandingLinkIsStillAnswered:
    async def test_the_negative_control(self, responder, maker, linked, far_side):
        """Admitted through the real entry point as the linked person,
        answered, and recorded with that person's reach."""
        seen = {}
        calls = _stub_work(responder)
        retrieve = responder._retrieve

        async def retrieve_and_look(session, pending):
            seen.update(mapped=pending.mapped, person=pending.asker_local_user_id,
                        link=pending.link_id, owner=pending.peer_owner_id)
            return await retrieve(session, pending)

        responder._retrieve = retrieve_and_look
        rid, finish = await _admit(responder, far_side, querier_ref=_REF)

        await finish()

        assert seen == {"mapped": True, "person": linked["person"],
                        "link": linked["link"], "owner": None}
        assert calls == {"retrieve": 1, "synthesize": 1}
        resp = await responder.handle_retrieve(_poll(far_side, rid))
        assert resp.status == STATUS_COMPLETE and resp.answer == _ANSWER
        rows = await _answered_rows(maker)
        assert [(r.final_status, r.identity_basis, r.user_id) for r in rows] == [
            ("success", "mapped", linked["person"])]


class TestALinkRevokedBeforeTheTaskGotToWork:
    @pytest.mark.parametrize("purge", [True, False])
    async def test_nothing_is_read_and_nothing_falls_back_to_the_pairing_tier(
        self, responder, maker, linked, far_side, purge
    ):
        """🛑 The pairing itself is still active -- a request refused only on
        the pairing would run. It must not run at all: neither as the person
        nor, silently, on the pairing's tier."""
        calls = _stub_work(responder)
        rid, finish = await _admit(responder, far_side, querier_ref=_REF)
        await _revoke_link(maker, linked, far_side, purge=purge)

        await finish()

        assert calls == {"retrieve": 0, "synthesize": 0}
        assert await get_pending_store().get(rid) is None
        resp = await responder.handle_retrieve(_poll(far_side, rid))
        assert resp.model_dump() == await _unknown_poll_response(responder, far_side)
        rows = await _answered_rows(maker)
        assert [(r.final_status, r.identity_basis, r.user_id) for r in rows] == [
            ("aborted", "mapped", linked["person"])]
        assert _what_happened(rows[0]) == (LINK, STAGE_BEFORE_READ)

    async def test_revoked_and_purged_between_resolving_the_link_and_storing_the_record(
        self, responder, maker, linked, far_side
    ):
        """🛑 The `initiate` race. The link is resolved, then the revocation
        commits and purges -- and finds nothing, because the record is only
        stored afterwards. No purge will ever reach that record; it carries
        `mapped=True` and the person's id."""
        store = get_pending_store()
        put = store.put
        stored = []

        async def revoke_then_put(pending):
            await _revoke_link(maker, linked, far_side, purge=True)
            await put(pending)
            stored.append((pending.mapped, pending.asker_local_user_id))

        store.put = revoke_then_put
        calls = _stub_work(responder)
        rid, finish = await _admit(responder, far_side, querier_ref=_REF)
        # The staging worked: a mapped record was stored AFTER the purge.
        assert stored == [(True, linked["person"])]
        assert await store.get(rid) is not None

        await finish()

        assert calls == {"retrieve": 0, "synthesize": 0}
        assert await store.get(rid) is None
        rows = await _answered_rows(maker)
        assert [r.final_status for r in rows] == ["aborted"]
        assert _what_happened(rows[0]) == (LINK, STAGE_BEFORE_READ)

    async def test_a_deactivated_account_lends_nothing_any_more(
        self, responder, maker, linked, far_side
    ):
        """The link row is still there; the account behind it is not active.
        `resolve_link` would not have admitted it -- the re-check must not
        be weaker than the admission."""
        calls = _stub_work(responder)
        _rid, finish = await _admit(responder, far_side, querier_ref=_REF)
        async with maker() as db:
            await db.execute(update(User).where(User.id == linked["person"])
                             .values(is_active=False))
            await db.commit()

        await finish()

        assert calls == {"retrieve": 0, "synthesize": 0}
        assert [r.final_status for r in await _answered_rows(maker)] == ["aborted"]


class TestALinkRevokedWhileInFlight:
    @pytest.mark.parametrize("purge", [True, False])
    async def test_during_retrieval_the_model_is_not_asked_and_nothing_is_stored(
        self, responder, maker, linked, far_side, purge
    ):
        async def revoke_now():
            await _revoke_link(maker, linked, far_side, purge=purge)

        calls = _stub_work(responder, during_retrieval=revoke_now)
        rid, finish = await _admit(responder, far_side, querier_ref=_REF)

        await finish()

        assert calls == {"retrieve": 1, "synthesize": 0}
        assert await get_pending_store().get(rid) is None
        rows = await _answered_rows(maker)
        assert [r.final_status for r in rows] == ["aborted"]
        assert _what_happened(rows[0]) == (LINK, STAGE_AFTER_READ)
        assert rows[0].error_message == _note(LINK, read=2)
        assert rows[0].answer_excerpt is None

    @pytest.mark.parametrize("purge", [True, False])
    async def test_during_synthesis_the_finished_answer_is_not_stored(
        self, responder, maker, linked, far_side, purge
    ):
        async def revoke_now():
            await _revoke_link(maker, linked, far_side, purge=purge)

        _stub_work(responder, during_synthesis=revoke_now)
        rid, finish = await _admit(responder, far_side, querier_ref=_REF)

        await finish()

        assert await get_pending_store().get(rid) is None
        resp = await responder.handle_retrieve(_poll(far_side, rid))
        assert resp.status == STATUS_EXPIRED and resp.answer is None
        rows = await _answered_rows(maker)
        assert [r.final_status for r in rows] == ["aborted"]
        assert all(_ANSWER not in (r.answer_excerpt or "") for r in rows)


class TestThePollChecksTheLinkItself:
    async def test_a_complete_mapped_record_of_a_revoked_link_is_refused_like_an_unknown_one(
        self, responder, maker, linked, far_side
    ):
        await get_pending_store().put(_mapped_complete(linked, far_side, "late-link-1"))
        await _revoke_link(maker, linked, far_side, purge=False)

        resp = await responder.handle_retrieve(_poll(far_side, "late-link-1"))

        assert resp.model_dump() == await _unknown_poll_response(responder, far_side)
        assert _ANSWER not in resp.model_dump_json()
        assert await get_pending_store().get("late-link-1") is None

    async def test_a_link_created_anew_does_not_reach_back(
        self, responder, maker, linked, far_side
    ):
        """Revoked and linked again with the same ref and the same account:
        consent given afresh is a NEW row. The old request stays refused."""
        await get_pending_store().put(_mapped_complete(linked, far_side, "late-link-2"))
        await _revoke_link(maker, linked, far_side, purge=False)
        async with maker() as db:
            db.add(FederationUserLink(
                peer_id=linked["peer"], querier_ref=_REF, local_user_id=linked["person"]))
            await db.commit()

        resp = await responder.handle_retrieve(_poll(far_side, "late-link-2"))
        assert resp.status == STATUS_EXPIRED

    async def test_a_mapped_record_without_a_link_id_is_refused(
        self, responder, maker, linked, far_side
    ):
        """A row from a version that did not carry the field: its link cannot
        be asked about, so it is not answered."""
        pending = _mapped_complete(linked, far_side, "late-link-3")
        pending.link_id = None
        await get_pending_store().put(pending)

        resp = await responder.handle_retrieve(_poll(far_side, "late-link-3"))
        assert resp.status == STATUS_EXPIRED

    async def test_the_same_record_on_a_standing_link_is_handed_out(
        self, responder, maker, linked, far_side
    ):
        """Negative control for the three tests above."""
        await get_pending_store().put(_mapped_complete(linked, far_side, "late-link-4"))

        resp = await responder.handle_retrieve(_poll(far_side, "late-link-4"))
        assert resp.status == STATUS_COMPLETE and resp.answer == _ANSWER


class TestTheRecordSaysWhatTheCodeKnows:
    async def test_a_record_that_is_merely_gone_is_not_called_revoked(
        self, responder, maker, pairing, far_side
    ):
        """🛑 The purge is coarse (a link revocation discards every request of
        that far side) and a record can lapse. With pairing and link intact,
        all the code knows is that the record is gone -- the row used to say
        "Widerrufen" regardless."""
        calls = _stub_work(responder)
        rid, finish = await _admit(responder, far_side)
        await get_pending_store().delete_many([rid])

        await finish()

        assert calls == {"retrieve": 0, "synthesize": 0}
        rows = await _answered_rows(maker)
        assert [r.final_status for r in rows] == ["aborted"]
        assert _what_happened(rows[0]) == (GONE, STAGE_BEFORE_READ)

    async def test_a_revoked_pairing_is_named_even_after_the_purge(
        self, responder, maker, pairing, far_side
    ):
        """The record is gone AND the database says why: the row names it."""
        _stub_work(responder)
        _rid, finish = await _admit(responder, far_side)
        await _revoke(maker, pairing, far_side, purge=True)

        await finish()

        rows = await _answered_rows(maker)
        assert _what_happened(rows[0]) == (PAIRING, STAGE_BEFORE_READ)


# --------------------------------------------------------------------------- #
# The log does not call a refused answer delivered
# --------------------------------------------------------------------------- #


def _row_state(row: FederationQueryLog) -> tuple:
    return (row.id, row.final_status, row.verified_signature, row.answer_excerpt,
            row.error_message, row.finalized_at)


class TestAnAnswerRefusedAtThePollIsNotRecordedAsDelivered:
    """🛑 The responder writes its `success` row when the answer is READY. A
    pairing or link revoked between that moment and the far side's poll is
    refused at the poll -- and the row went on saying "answered", excerpt
    included, for an answer nobody received."""

    async def test_pairing_revoked_between_the_answer_and_the_poll(
        self, responder, maker, pairing, far_side
    ):
        _stub_work(responder)
        rid, finish = await _admit(responder, far_side)
        await finish()
        (before,) = await _answered_rows(maker)
        # The premise: the row says delivered, and names its request.
        assert (before.final_status, before.answer_excerpt) == ("success", _ANSWER)
        assert before.request_id == rid
        await _revoke(maker, pairing, far_side, purge=False)

        resp = await responder.handle_retrieve(_poll(far_side, rid))

        assert resp.model_dump() == await _unknown_poll_response(responder, far_side)
        (after,) = await _answered_rows(maker)       # still ONE row -- the same
        assert after.id == before.id
        assert after.final_status == "aborted"
        assert _what_happened(after) == (PAIRING, STAGE_AFTER_ANSWER)
        assert after.error_message == _undelivered_note(
            PAIRING, SimpleNamespace(retrieved_count=2))
        assert after.answer_excerpt is None and after.verified_signature is False
        # Who and what stay as they were written.
        assert (after.user_id, after.identity_basis, after.query_text) == (
            before.user_id, before.identity_basis, before.query_text)

    async def test_link_revoked_between_the_answer_and_the_poll(
        self, responder, maker, linked, far_side
    ):
        """The widest reach, both pending-store backends."""
        _stub_work(responder)
        rid, finish = await _admit(responder, far_side, querier_ref=_REF)
        await finish()
        await _revoke_link(maker, linked, far_side, purge=False)

        resp = await responder.handle_retrieve(_poll(far_side, rid))

        assert resp.status == STATUS_EXPIRED
        (row,) = await _answered_rows(maker)
        assert (row.final_status, row.identity_basis, row.user_id) == (
            "aborted", "mapped", linked["person"])
        assert _what_happened(row) == (LINK, STAGE_AFTER_ANSWER)
        assert row.answer_excerpt is None

    async def test_repeated_polls_leave_one_record_written_once(
        self, responder, maker, pairing, far_side
    ):
        """The far side polls about twice a second and does not stop because
        it was refused."""
        _stub_work(responder)
        rid, finish = await _admit(responder, far_side)
        await finish()
        await _revoke(maker, pairing, far_side, purge=False)

        await responder.handle_retrieve(_poll(far_side, rid))
        (first,) = await _answered_rows(maker)
        for _ in range(5):
            resp = await responder.handle_retrieve(_poll(far_side, rid))
            assert resp.status == STATUS_EXPIRED

        (last,) = await _answered_rows(maker)
        assert _row_state(last) == _row_state(first)

    async def test_two_polls_refused_at_the_same_moment_leave_one_record(
        self, responder, maker, pairing, far_side
    ):
        """Both have read the pending record before either deleted it -- two
        workers, or two requests on one. Staged, not left to the scheduler:
        each poll is held right after its look at the database."""
        import asyncio

        import services.federation_query_responder as fqr
        from services.federation_query_responder import FederationQueryResponder

        _stub_work(responder)
        rid, finish = await _admit(responder, far_side)
        await finish()
        await _revoke(maker, pairing, far_side, purge=False)

        looked = asyncio.Semaphore(0)
        go = asyncio.Event()
        look = fqr._standing_refusal

        async def look_and_wait(db, pending):
            why = await look(db, pending)
            looked.release()
            await go.wait()
            return why

        fqr._standing_refusal = look_and_wait
        try:
            async with maker() as second_session:
                other = FederationQueryResponder(db=second_session)
                polls = [
                    asyncio.create_task(r.handle_retrieve(_poll(far_side, rid)))
                    for r in (responder, other)]
                await asyncio.wait_for(looked.acquire(), 10)
                await asyncio.wait_for(looked.acquire(), 10)
                go.set()
                answers = await asyncio.gather(*polls)
        finally:
            fqr._standing_refusal = look

        assert [a.status for a in answers] == [STATUS_EXPIRED, STATUS_EXPIRED]
        rows = await _answered_rows(maker)
        assert [r.final_status for r in rows] == ["aborted"]
        assert _what_happened(rows[0]) == (PAIRING, STAGE_AFTER_ANSWER)

    async def test_an_answer_that_was_handed_over_stays_recorded_as_delivered(
        self, responder, maker, pairing, far_side
    ):
        """Negative control: collected first, revoked afterwards, polled
        again. The second poll is refused -- and the row is NOT rewritten,
        because the answer did go out."""
        _stub_work(responder)
        rid, finish = await _admit(responder, far_side)
        await finish()
        delivered = await responder.handle_retrieve(_poll(far_side, rid))
        assert delivered.status == STATUS_COMPLETE and delivered.answer == _ANSWER
        (before,) = await _answered_rows(maker)
        await _revoke(maker, pairing, far_side, purge=False)

        resp = await responder.handle_retrieve(_poll(far_side, rid))

        assert resp.status == STATUS_EXPIRED
        (after,) = await _answered_rows(maker)
        assert _row_state(after) == _row_state(before)
        assert after.final_status == "success" and after.answer_excerpt == _ANSWER

    async def test_a_poll_on_the_normal_path_writes_no_record(
        self, responder, maker, pairing, far_side, monkeypatch
    ):
        """Polling stays a read of the audit log: while the answer is being
        made, when it is handed over, and on every poll after that."""
        import services.federation_audit as audit

        corrections = []

        async def spy(**kwargs):
            corrections.append(kwargs)
            return False

        monkeypatch.setattr(audit, "mark_answer_not_handed_over", spy)
        _stub_work(responder)
        rid, finish = await _admit(responder, far_side)
        while_processing = await responder.handle_retrieve(_poll(far_side, rid))
        assert while_processing.status == "processing"
        assert await _answered_rows(maker) == []
        await finish()
        (before,) = await _answered_rows(maker)

        for _ in range(3):
            resp = await responder.handle_retrieve(_poll(far_side, rid))
            assert resp.status == STATUS_COMPLETE

        (after,) = await _answered_rows(maker)
        assert _row_state(after) == _row_state(before)
        assert corrections == []
        assert (await get_pending_store().get(rid)).handed_over_at is not None


class TestAnAnswerThatCouldNotBeStoredIsNotRecordedAsDelivered:
    """The row for a finished answer is written before the answer becomes
    collectable. Whatever goes wrong in between corrects that row -- there is
    never a `success` row for an answer that could not be collected, and
    never a second row beside it."""

    async def test_the_store_failing_corrects_the_row(
        self, responder, maker, pairing, far_side, monkeypatch
    ):
        from services.federation_query_responder import _NOT_STORED

        _stub_work(responder)
        rid, finish = await _admit(responder, far_side)
        store = get_pending_store()
        save = store.save
        failures = []

        async def save_fails_once_the_answer_is_in(pending):
            if pending.status == STATUS_COMPLETE and not failures:
                failures.append(1)
                raise ConnectionError("store unreachable")
            return await save(pending)

        monkeypatch.setattr(store, "save", save_fails_once_the_answer_is_in)
        await finish()

        assert failures == [1]
        (row,) = await _answered_rows(maker)
        assert row.final_status == "aborted"
        assert _what_happened(row) == (_NOT_STORED, STAGE_AFTER_ANSWER)
        assert row.answer_excerpt is None
        after = await store.get(rid)
        assert after.status == "failed"
        resp = await responder.handle_retrieve(_poll(far_side, rid))
        assert resp.status == "failed" and resp.answer is None

    async def test_a_cancellation_while_storing_corrects_the_row(
        self, responder, maker, pairing, far_side, monkeypatch
    ):
        import asyncio

        from services.federation_query_responder import _CANCELLED

        _stub_work(responder)
        rid, finish = await _admit(responder, far_side)
        store = get_pending_store()
        save = store.save
        storing = asyncio.Event()

        async def save_hangs_once_the_answer_is_in(pending):
            if pending.status == STATUS_COMPLETE:
                storing.set()
                await asyncio.sleep(3600)
            return await save(pending)

        monkeypatch.setattr(store, "save", save_hangs_once_the_answer_is_in)
        task = asyncio.create_task(finish())
        await asyncio.wait_for(storing.wait(), 10)
        import services.federation_query_responder as fqr

        (work,) = [t for t in fqr._background_tasks if not t.done()]
        work.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        (row,) = await _answered_rows(maker)
        assert row.final_status == "aborted"
        assert _what_happened(row) == (_CANCELLED, STAGE_AFTER_ANSWER)
        assert row.answer_excerpt is None
        # And what the row calls undelivered cannot be collected after all.
        monkeypatch.setattr(store, "save", save)
        resp = await responder.handle_retrieve(_poll(far_side, rid))
        assert resp.model_dump() == await _unknown_poll_response(responder, far_side)
