"""The responder records WHICH identity it answered on.

Why: on 2026-10-29 the first successful federated query in this project ran
(`status=success`, signature verified). Whether it ran as the linked person --
full own reach on the far side -- or fell back to the granted tier could NOT
be established afterwards. Only inferred: from the same `querier_ref` on both
sides and from the code. The pending record that carries `mapped` lives in
memory and is gone once the query ends.

For a distinction that decides the REACH of an access, an inference is not
enough.

And it has to be the RESPONDER. Until today only the asker side wrote
(`mcp_client.py`, a single caller). A record there would be the far side's
claim about its own access decision; the instance whose data was read has to
record the access itself.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from services.federation_query_responder import (
    _PendingRequest,
    _write_responder_audit,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _pending(*, mapped: bool, asker_local_user_id=None, peer_owner_id=None,
             peer_display_name="the neighbour's Renfield"):
    return _PendingRequest(
        request_id="r1",
        asker_pubkey="aa" * 32,
        peer_user_id=3,
        asker_local_user_id=asker_local_user_id,
        max_visible_tier=2,
        query="what is in x-idra?",
        initiated_at=time.time(),
        peer_owner_id=peer_owner_id,
        mapped=mapped,
        peer_display_name=peer_display_name,
    )


@pytest.fixture
def written(monkeypatch):
    """Intercepts the write instead of involving the database."""
    from services import federation_audit

    m = AsyncMock()
    monkeypatch.setattr(federation_audit, "write_federation_audit", m)
    return m


async def test_the_mapped_path_is_recorded_as_mapped(written):
    """The case that opens up the reach -- and that has to be provable."""
    p = _pending(mapped=True, asker_local_user_id=107)
    p.answer = "an answer"
    await _write_responder_audit(p, success=True)

    written.assert_awaited_once()
    k = written.await_args.kwargs
    assert k["identity_basis"] == "mapped"
    assert k["direction"] == "answered"
    assert k["user_id"] == 107, "the LINKED person, whose reach applied"


async def test_the_fallback_is_recorded_as_peer_tier(written):
    """Negative control against a check that always says "mapped". And
    `user_id` here is the OWNER of the pairing -- even in the fallback a local
    person's reach was used, just a narrower one."""
    await _write_responder_audit(_pending(mapped=False, peer_owner_id=2), success=True)

    k = written.await_args.kwargs
    assert k["identity_basis"] == "peer_tier"
    assert k["user_id"] == 2


async def test_a_failure_is_recorded_too(written):
    """A failed access is still an access attempt and belongs in the log --
    otherwise it shows only the successful ones and looks calmer than it
    was."""
    await _write_responder_audit(
        _pending(mapped=True, asker_local_user_id=107), success=False, error="broken")

    k = written.await_args.kwargs
    assert k["direction"] == "answered"
    # NOT `None`. In `_classify_final` that means "aborted" and would lose
    # the reason; this query FAILED, and recognisably so.
    assert k["final_item"]["success"] is False
    assert k["final_item"]["message"] == "broken"


async def test_a_record_with_NEITHER_writes_nothing(written):
    """Neither mapped nor with an owner: a record from an older version,
    which `_peer_scope_for` rejects anyway. A row with an invented `user_id`
    would be worse than none -- it would attribute an access to a person who
    did not make it."""
    await _write_responder_audit(_pending(mapped=False), success=True)
    written.assert_not_awaited()


async def test_the_asker_side_keeps_its_meaning():
    """Negative control on the defaults: whoever calls
    `write_federation_audit` without the new arguments -- that is, the
    unchanged asker side -- writes `asked` and NO identity basis. A default
    there would report ignorance as fact."""
    import inspect

    from services.federation_audit import write_federation_audit

    sig = inspect.signature(write_federation_audit)
    assert sig.parameters["direction"].default == "asked"
    assert sig.parameters["identity_basis"].default is None


def test_both_columns_exist_on_the_model():
    """The write is useless if the columns are missing -- and the migration
    is the only place that creates them."""
    from models.database import FederationQueryLog

    spalten = {c.name: c for c in FederationQueryLog.__table__.columns}
    assert spalten["direction"].nullable is False
    assert spalten["identity_basis"].nullable is True, (
        "nullable, because legacy rows and asker rows do NOT know it")


class TestTheRecordedOutcomeIsTheTruth:
    """The tests above check WHAT is passed -- not what becomes of it.

    That is exactly where a bug slipped past me in `/review`: the success path
    passed `{"answer": ...}`, and `_classify_final` reads
    `final.get("success")`. Without that key the record is falsy, and every
    SUCCESSFUL answer would have landed in the log as "failed / Unknown
    failure". An audit log that records successes as failures is worse than no
    log at all.

    So this measures all the way THROUGH `_classify_final` instead of stopping
    at the call boundary.
    """

    def test_the_success_shape_classifies_as_success(self):
        from services.federation_audit import _classify_final

        status, _sig, excerpt, error = _classify_final(
            {"success": True, "message": "an answer"})
        assert status == "success"
        assert excerpt == "an answer"
        assert error is None

    def test_the_failure_shape_carries_the_REAL_reason(self):
        """Do not pass `None`: there it means "aborted", and the reason would
        be lost."""
        from services.federation_audit import _classify_final

        status, _, _, error = _classify_final(
            {"success": False, "message": "timeout during retrieval"})
        assert status == "failed"
        assert "timeout" in error

    def test_the_OLD_shape_would_have_been_recorded_as_a_failure(self):
        """Negative control pinning down the bug I made. If this test fails,
        `_classify_final` was rewritten -- and then the success path above
        needs re-checking."""
        from services.federation_audit import _classify_final

        status, _, _, _ = _classify_final({"answer": "an answer"})
        assert status == "failed", (
            "without the `success` key the record counts as a failure -- "
            "which is why the success path has to set it")

    @pytest.mark.asyncio
    async def test_the_responder_passes_the_shape_that_classifies_right(self, written):
        """The chain end to end: what the responder passes has to come out of
        `_classify_final` as success or failure respectively."""
        from services.federation_audit import _classify_final

        p = _pending(mapped=True, asker_local_user_id=107)
        p.answer = "an answer"
        await _write_responder_audit(p, success=True)
        status, _, excerpt, _ = _classify_final(
            written.await_args.kwargs["final_item"])
        assert status == "success" and excerpt == "an answer"

        written.reset_mock()
        await _write_responder_audit(p, success=False, error="timeout")
        status, _, _, error = _classify_final(
            written.await_args.kwargs["final_item"])
        assert status == "failed" and "timeout" in error


class TestTheRecordSaysWHOAccessed:
    """Found in the adversarial pass on 2026-10-29: the responder wrote
    `peer_display_name_snapshot=""` -- hard-coded. In the audit view that spot
    says WHO accessed the data, and every answered row was blank there. For a
    log whose purpose is "who accessed my data, with what reach", that is half
    the statement missing.
    """

    @pytest.mark.asyncio
    async def test_the_peer_name_is_recorded(self, written):
        await _write_responder_audit(
            _pending(mapped=True, asker_local_user_id=107), success=True)
        assert written.await_args.kwargs[
            "peer_display_name_snapshot"] == "the neighbour's Renfield"

    @pytest.mark.asyncio
    async def test_an_empty_name_is_NOT_invented(self, written):
        """Negative control. A row from an older version does not know the
        field and falls back to the empty default -- which is passed through,
        not replaced by an invented name. The view shows the key instead."""
        await _write_responder_audit(
            _pending(mapped=True, asker_local_user_id=107, peer_display_name=""),
            success=True)
        assert written.await_args.kwargs["peer_display_name_snapshot"] == ""

    def test_the_name_survives_the_redis_round_trip(self):
        """With the Redis store the pending record goes through JSON. A field
        that does not survive the round trip would be silently empty during a
        rolling deploy -- and the bug would look just like the old one."""
        from services.federation_pending_store import _from_jsonable, _to_jsonable

        restored = _from_jsonable(_to_jsonable(
            _pending(mapped=True, asker_local_user_id=107)))
        assert restored.peer_display_name == "the neighbour's Renfield"

    def test_an_older_row_without_the_field_still_loads(self):
        """Forward compatibility: a missing key falls back to the default
        instead of blowing up the conversion."""
        from services.federation_pending_store import _from_jsonable, _to_jsonable

        d = _to_jsonable(_pending(mapped=True, asker_local_user_id=107))
        del d["peer_display_name"]
        assert _from_jsonable(d).peer_display_name == ""


class TestACancelledAccessIsStillAnAccess:
    """Found on 2026-10-29: the cancellation branch of `_run_query` wrote
    NOTHING and re-raised. The ASKER side has written its record inside a
    `finally` since F4d, explicitly for this case -- this side came later and
    did not adopt it. And this is the side that records a FOREIGN access to
    one's own data.

    The privacy-relevant dividing line is `_retrieve`: before it nothing was
    read, after it foreign data sat in memory -- even with no answer
    delivered. That cannot be reconstructed afterwards.
    """

    @pytest.mark.asyncio
    async def test_a_cancellation_AFTER_the_read_is_recorded(self, written):
        from services.federation_query_responder import (
            _record_cancelled_access,
        )

        p = _pending(mapped=True, asker_local_user_id=107)
        p.retrieved_count = 3
        await _record_cancelled_access(p)

        k = written.await_args.kwargs
        assert k["direction"] == "answered"
        assert k["identity_basis"] == "mapped"
        assert "3 Treffer" in k["final_item"]["message"]

    @pytest.mark.asyncio
    async def test_a_cancellation_BEFORE_the_read_is_recorded_TOO(self, written):
        """An access attempt without a read belongs in the log too -- same
        reasoning as `test_a_failure_is_recorded_too`: showing only the
        successful ones would look calmer than it was."""
        from services.federation_query_responder import (
            _record_cancelled_access,
        )

        p = _pending(mapped=True, asker_local_user_id=107)
        assert p.retrieved_count is None, "Vorgabe: noch nichts gelesen"
        await _record_cancelled_access(p)

        assert "keine Daten gelesen" in written.await_args.kwargs[
            "final_item"]["message"]

    @pytest.mark.asyncio
    async def test_the_two_cases_do_NOT_collapse(self, written):
        """Negative control. `retrieved_count = 0` means "read, found
        nothing" and must not collapse into `None` ("not read") -- which is
        why the field is `int | None` and not `int = 0`."""
        from services.federation_query_responder import (
            _record_cancelled_access,
        )

        p = _pending(mapped=True, asker_local_user_id=107)
        p.retrieved_count = 0
        await _record_cancelled_access(p)
        text_null = written.await_args.kwargs["final_item"]["message"]

        written.reset_mock()
        p.retrieved_count = None
        await _record_cancelled_access(p)
        text_none = written.await_args.kwargs["final_item"]["message"]

        assert text_null != text_none
        assert "nach dem Abruf" in text_null
        assert "vor dem Abruf" in text_none

    @pytest.mark.asyncio
    async def test_it_classifies_as_aborted_NOT_failed(self, written):
        """The chain, not the call boundary. A cancellation carries
        `success=False` and would otherwise fall into the failure branch --
        where a completed read sits among genuine failures and gets skipped
        when someone reads through the log."""
        from services.federation_audit import _classify_final
        from services.federation_query_responder import (
            _record_cancelled_access,
        )

        p = _pending(mapped=True, asker_local_user_id=107)
        p.retrieved_count = 3
        await _record_cancelled_access(p)

        status, verified, excerpt, error = _classify_final(
            written.await_args.kwargs["final_item"])
        assert status == "aborted"
        assert verified is False
        assert excerpt is None
        assert "3 Treffer" in error

    def test_a_plain_failure_is_STILL_failed(self):
        """Negative control for the classifier: the new branch must not
        swallow the old one. Without `aborted` it stays a failure."""
        from services.federation_audit import _classify_final

        status, _, _, _ = _classify_final(
            {"success": False, "message": "timeout"})
        assert status == "failed"

    @pytest.mark.asyncio
    async def test_a_failing_write_does_NOT_swallow_the_cancellation(self, monkeypatch):
        """The record must never mask the cancellation: `_run_query` has to
        keep raising `CancelledError` even when the write fails."""
        from services import federation_audit
        from services.federation_query_responder import (
            _record_cancelled_access,
        )

        monkeypatch.setattr(
            federation_audit, "write_federation_audit",
            AsyncMock(side_effect=RuntimeError("database gone")))
        p = _pending(mapped=True, asker_local_user_id=107)
        p.retrieved_count = 3
        # Does not raise -- the caller re-raises afterwards itself.
        await _record_cancelled_access(p)


class TestTheCancellationBranchIsACTUALLYWired:
    """The class above tests the FUNCTION. This one tests that `_run_query`
    actually CALLS it on cancellation -- exactly the gap already demonstrated
    to me once in this session: a measured function and an unchecked use.
    """

    @pytest.mark.asyncio
    async def test_cancelling_mid_retrieve_still_writes_a_row(self, monkeypatch):
        import asyncio

        from services import federation_query_responder as fqr

        written = AsyncMock()
        monkeypatch.setattr(fqr, "_record_cancelled_access", written)

        p = _pending(mapped=True, asker_local_user_id=107)
        store = AsyncMock()
        store.get = AsyncMock(return_value=p)
        store.save = AsyncMock()
        monkeypatch.setattr(fqr, "get_pending_store", lambda: store)

        responder = fqr.FederationQueryResponder(db=AsyncMock())
        monkeypatch.setattr(responder, "_emit_progress", AsyncMock())

        losgelaufen = asyncio.Event()

        async def haengender_abruf(session, pending):
            losgelaufen.set()
            await asyncio.sleep(3600)   # bis zum Abbruch

        monkeypatch.setattr(responder, "_retrieve", haengender_abruf)
        # The background run's own session must not touch a database.
        monkeypatch.setattr(fqr, "AsyncSessionLocal", lambda: AsyncMock())

        task = asyncio.create_task(responder._run_query(p.request_id))
        await losgelaufen.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        written.assert_awaited_once()
        assert written.await_args.args[0] is p

    @pytest.mark.asyncio
    async def test_the_retrieved_count_is_set_before_synthesis(self, monkeypatch):
        """The dividing line has to be SET before the next suspension point.
        If it were only set after synthesis, a cancellation during synthesis
        would be recorded as "nothing read" -- the very understatement this
        whole thing is written against."""
        import asyncio

        from services import federation_query_responder as fqr

        monkeypatch.setattr(fqr, "_record_cancelled_access", AsyncMock())
        p = _pending(mapped=True, asker_local_user_id=107)
        store = AsyncMock()
        store.get = AsyncMock(return_value=p)
        store.save = AsyncMock()
        monkeypatch.setattr(fqr, "get_pending_store", lambda: store)
        monkeypatch.setattr(fqr, "AsyncSessionLocal", lambda: AsyncMock())

        responder = fqr.FederationQueryResponder(db=AsyncMock())
        monkeypatch.setattr(responder, "_emit_progress", AsyncMock())
        monkeypatch.setattr(responder, "_retrieve", AsyncMock(return_value=[1, 2, 3]))

        in_synthese = asyncio.Event()

        async def haengende_synthese(frage, treffer):
            in_synthese.set()
            await asyncio.sleep(3600)

        monkeypatch.setattr(responder, "_synthesize", haengende_synthese)

        task = asyncio.create_task(responder._run_query(p.request_id))
        await in_synthese.wait()
        assert p.retrieved_count == 3, (
            "by the time synthesis runs it must be established that a read "
            "happened")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
