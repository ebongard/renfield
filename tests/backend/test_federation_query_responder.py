"""
Tests for F3a — federation query_brain responder.

Coverage:
- initiate happy path (verified peer → request_id minted → background task kicked)
- signature mismatch rejected
- stale timestamp rejected
- nonce replay rejected
- unknown peer rejected (or revoked peer)
- retrieve pubkey-binding (stolen request_id can't be polled by another peer)
- retrieve unknown request → STATUS_EXPIRED (not an oracle)
- progress rate-limited to MAX_PROGRESS_UPDATES
- terminal response is signed by responder
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from services.federation_identity import (
    FederationIdentity,
    get_federation_identity,
    init_federation_identity,
    reset_federation_identity_for_tests,
)
from services.federation_pending_store import (
    _PendingRequest,
    get_pending_store,
    reset_store_for_tests,
)
from services.federation_query_responder import (
    MAX_PROGRESS_UPDATES,
    FederationQueryError,
    FederationQueryResponder,
    _clear_state_for_tests,
)
from services.federation_query_schemas import (
    STATUS_COMPLETE,
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_PROCESSING,
    QueryBrainInitiateRequest,
    QueryBrainRetrieveRequest,
    complete_canonical_payload,
    initiate_canonical_payload,
)
from services.mcp_streaming import (
    PROGRESS_LABEL_COMPLETE,
    PROGRESS_LABEL_RETRIEVING,
    PROGRESS_LABEL_SYNTHESIZING,
)
from services.pairing_service import _canonical_bytes
from utils.config import settings

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
async def responder_identity(tmp_path):
    """Fresh federation identity for the responder side + reset state."""
    reset_federation_identity_for_tests()
    init_federation_identity(tmp_path / "responder_key")
    reset_store_for_tests()  # Force re-select of InMemory backend each test
    await _clear_state_for_tests()
    yield get_federation_identity()
    reset_federation_identity_for_tests()
    await _clear_state_for_tests()
    reset_store_for_tests()


@pytest.fixture
def asker_identity():
    """Independent asker identity — not the responder's singleton."""
    priv = ed25519.Ed25519PrivateKey.generate()
    return FederationIdentity(priv)


@pytest.fixture
def mock_db_with_peer(asker_identity):
    """AsyncSession mock that returns a matching PeerUser for the asker."""
    db = MagicMock()
    peer = MagicMock()
    peer.id = 77
    peer.circle_owner_id = 1  # responder's local user
    peer.remote_pubkey = asker_identity.public_key_hex()
    peer.remote_user_id = 42
    # 🛑 Since F-ID-3 the PAIRING carries the tier. Before that it came from a
    # `CircleMembership` keyed on `remote_user_id` -- a number belonging to the
    # FAR SIDE, in a column with a foreign key on the LOCAL users.id.
    peer.granted_tier = 2
    peer.revoked_at = None

    db.execute = AsyncMock()

    async def execute_side(stmt):
        result = MagicMock()
        if "peer_users" in str(stmt):
            # 🛑 Since 2026-10-28 `_lookup_peer` fetches ALL active rows for
            # the key and refuses when there is more than one. Before that it
            # was a `scalar_one_or_none()`, which with two local accounts
            # sharing one far side would have thrown `MultipleResultsFound` --
            # so every federated request as a 500.
            result.scalars = lambda: MagicMock(all=lambda: [peer])
            result.scalar_one_or_none = lambda: peer
        else:
            result.scalars = lambda: MagicMock(all=lambda: [])
            result.scalar_one_or_none = lambda: None
        return result

    db.execute.side_effect = execute_side
    db.commit = AsyncMock()
    return db


# =============================================================================
# Helpers
# =============================================================================


class _SessionSeeingAnActivePairing:
    """Stand-in for a session the background task opens itself. Every
    statement finds a row -- which is all `_pairing_is_active` asks."""

    async def __aenter__(self):
        session = MagicMock()
        session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=lambda: 1))
        return session

    async def __aexit__(self, *a):
        return False


def _sign_initiate(
    asker: FederationIdentity,
    query: str = "what is mom's recipe?",
    nonce: str | None = None,
    timestamp: int | None = None,
    depth: int = 3,
    path: list[str] | None = None,
    querier_ref: str | None = None,
) -> QueryBrainInitiateRequest:
    """Build a signed v2 initiate envelope.

    `path` defaults to `[asker.pubkey]` — the realistic first-hop shape
    after F5a. Tests that want to exercise cycle detection pass an
    explicit `path` carrying the responder's own pubkey. `querier_ref`
    (F-ID-1) is folded into the signed bytes ONLY when set, mirroring
    initiate_canonical_payload's conditional inclusion.
    """
    import secrets

    my_pubkey = asker.public_key_hex()
    unsigned = {
        "version": 2,
        "asker_pubkey": my_pubkey,
        "query": query,
        "nonce": nonce or secrets.token_hex(16),
        "timestamp": timestamp if timestamp is not None else int(time.time()),
        "depth": depth,
        "path": path if path is not None else [my_pubkey],
    }
    if querier_ref is not None:
        unsigned["querier_ref"] = querier_ref
    sig = asker.sign(_canonical_bytes(unsigned)).hex()
    return QueryBrainInitiateRequest(**unsigned, signature=sig)


def _sign_retrieve(
    asker: FederationIdentity,
    request_id: str,
    timestamp: int | None = None,
) -> QueryBrainRetrieveRequest:
    # Retrieve envelope is unchanged by F5a (depth/path live only on
    # /initiate) so version stays 1 for the poll request.
    unsigned = {
        "version": 1,
        "request_id": request_id,
        "asker_pubkey": asker.public_key_hex(),
        "timestamp": timestamp if timestamp is not None else int(time.time()),
    }
    sig = asker.sign(_canonical_bytes(unsigned)).hex()
    return QueryBrainRetrieveRequest(**unsigned, signature=sig)


# =============================================================================
# Initiate
# =============================================================================


class TestHandleInitiate:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_happy_path_mints_request_id(
        self, responder_identity, asker_identity, mock_db_with_peer,
    ):
        responder = FederationQueryResponder(db=mock_db_with_peer)

        # Stub out _run_query so we don't need Ollama/PolymorphicAtomStore.
        responder._run_query = AsyncMock()

        req = _sign_initiate(asker_identity)
        resp = await responder.handle_initiate(req)

        assert resp.request_id  # UUID-shaped
        assert len(resp.request_id) == 36  # UUID4 with hyphens
        assert resp.accepted_at >= 0

        # Pending entry exists with the correct asker_pubkey binding.
        pending = await get_pending_store().get(resp.request_id)
        assert pending is not None
        assert pending.asker_pubkey == asker_identity.public_key_hex()
        assert pending.peer_user_id == 77
        assert pending.max_visible_tier == 2

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_tampered_signature_rejected(
        self, responder_identity, asker_identity, mock_db_with_peer,
    ):
        responder = FederationQueryResponder(db=mock_db_with_peer)
        req = _sign_initiate(asker_identity)
        # Flip one byte in the signature
        bad = bytearray(bytes.fromhex(req.signature))
        bad[0] ^= 0xFF
        tampered = QueryBrainInitiateRequest(**{**req.model_dump(), "signature": bad.hex()})

        with pytest.raises(FederationQueryError, match="Signature"):
            await responder.handle_initiate(tampered)

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_stale_timestamp_rejected(
        self, responder_identity, asker_identity, mock_db_with_peer,
    ):
        responder = FederationQueryResponder(db=mock_db_with_peer)
        # 2 minutes in the past — outside ±60s window
        req = _sign_initiate(asker_identity, timestamp=int(time.time()) - 120)
        with pytest.raises(FederationQueryError, match="window"):
            await responder.handle_initiate(req)

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_nonce_replay_rejected(
        self, responder_identity, asker_identity, mock_db_with_peer,
    ):
        responder = FederationQueryResponder(db=mock_db_with_peer)
        responder._run_query = AsyncMock()

        req1 = _sign_initiate(asker_identity, nonce="deadbeefdeadbeef" * 2)
        await responder.handle_initiate(req1)

        # Build a second request reusing the same nonce (signer would need
        # to resign since timestamp changed; we simulate by re-signing the
        # same nonce at a new moment).
        req2 = _sign_initiate(asker_identity, nonce="deadbeefdeadbeef" * 2)
        with pytest.raises(FederationQueryError, match="replay"):
            await responder.handle_initiate(req2)

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_unknown_peer_rejected(self, responder_identity, asker_identity):
        """Asker whose pubkey isn't in peer_users must be rejected."""
        db = MagicMock()
        async def no_peer(stmt):
            r = MagicMock()
            r.scalar_one_or_none = lambda: None
            # No active pairing for this key -- the new shape.
            r.scalars = lambda: __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock(all=lambda: [])
            return r
        db.execute = AsyncMock(side_effect=no_peer)
        db.commit = AsyncMock()

        responder = FederationQueryResponder(db=db)
        req = _sign_initiate(asker_identity)
        with pytest.raises(FederationQueryError, match="Unknown or revoked"):
            await responder.handle_initiate(req)


# =============================================================================
# F5a — depth + cycle detection
# =============================================================================


class TestDepthAndCycleDetection:
    @pytest.mark.unit
    def test_negative_depth_rejected_at_parse_time(self, asker_identity):
        """Pydantic ge=0 on the `depth` field is the first line of
        defense: a wire-level request with negative depth can't even
        reach handle_initiate. The handler's own `depth < 0` branch is
        defense-in-depth for any future non-HTTP entry point."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="greater_than_equal"):
            _sign_initiate(asker_identity, depth=-1)

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_depth_zero_accepted_as_leaf(
        self, mock_db_with_peer, asker_identity, responder_identity,
    ):
        """depth=0 means 'you're the last hop — do the work but don't
        cascade'. Valid at initiate time; cascading from here would be
        a responder-side bug, not an asker-side one."""
        responder = FederationQueryResponder(db=mock_db_with_peer)
        req = _sign_initiate(asker_identity, depth=0)

        resp = await responder.handle_initiate(req)
        assert resp.request_id

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_cycle_rejected_when_own_pubkey_in_path(
        self, mock_db_with_peer, asker_identity, responder_identity,
    ):
        """Responder sees its own pubkey in path → refuses to process.
        This is the defense against A→B→C→B chains."""
        responder = FederationQueryResponder(db=mock_db_with_peer)
        my_pubkey = responder_identity.public_key_hex()
        asker_pubkey = asker_identity.public_key_hex()
        # Path includes the responder already — mimics a transitive
        # request that looped back.
        req = _sign_initiate(
            asker_identity,
            path=[asker_pubkey, my_pubkey],
        )

        with pytest.raises(FederationQueryError, match="cycle"):
            await responder.handle_initiate(req)

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_asker_pubkey_missing_from_path_rejected(
        self, mock_db_with_peer, asker_identity, responder_identity,
    ):
        """Path must include asker_pubkey — stripping it is an attempt
        to erase the originator from the call chain, which would let
        an adversary anonymize queries."""
        responder = FederationQueryResponder(db=mock_db_with_peer)
        # Sign with a path that omits the asker's own pubkey.
        req = _sign_initiate(asker_identity, path=["c" * 64])

        with pytest.raises(FederationQueryError, match=r"path|asker_pubkey"):
            await responder.handle_initiate(req)

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_happy_path_with_realistic_depth_and_path(
        self, mock_db_with_peer, asker_identity, responder_identity,
    ):
        """Sanity: a standard first-hop request (depth=3, path=[asker])
        is accepted. Regression guard against the schema bump breaking
        the common case."""
        responder = FederationQueryResponder(db=mock_db_with_peer)
        responder._run_query = AsyncMock()  # skip real synthesis
        req = _sign_initiate(asker_identity)  # defaults: depth=3, path=[asker]

        resp = await responder.handle_initiate(req)
        assert resp.request_id
        assert resp.accepted_at

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_tampered_path_breaks_signature(
        self, mock_db_with_peer, asker_identity, responder_identity,
    ):
        """An adversary stripping `path` on the wire (to hide the chain)
        must fail signature verification — the canonical payload covers
        path, so any mutation invalidates the sig."""
        responder = FederationQueryResponder(db=mock_db_with_peer)
        req = _sign_initiate(asker_identity)

        tampered = QueryBrainInitiateRequest(
            **{**req.model_dump(), "path": []},
        )
        with pytest.raises(FederationQueryError, match="Signature"):
            await responder.handle_initiate(tampered)


# =============================================================================
# Retrieve
# =============================================================================


class TestHandleRetrieve:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_returns_processing_state(
        self, responder_identity, asker_identity, mock_db_with_peer,
    ):
        responder = FederationQueryResponder(db=mock_db_with_peer)
        responder._run_query = AsyncMock()

        init_req = _sign_initiate(asker_identity)
        init_resp = await responder.handle_initiate(init_req)

        poll = _sign_retrieve(asker_identity, init_resp.request_id)
        resp = await responder.handle_retrieve(poll)
        assert resp.status == STATUS_PROCESSING
        assert resp.progress == PROGRESS_LABEL_RETRIEVING

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_unknown_request_id_returns_expired(
        self, responder_identity, asker_identity, mock_db_with_peer,
    ):
        """No oracle — non-existent request_id returns the same status
        as an actually-expired one. An attacker can't enumerate valid ids."""
        responder = FederationQueryResponder(db=mock_db_with_peer)
        poll = _sign_retrieve(asker_identity, "not-a-real-request-id")
        resp = await responder.handle_retrieve(poll)
        assert resp.status == STATUS_EXPIRED

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_stolen_request_id_rejected_with_different_pubkey(
        self, responder_identity, asker_identity, mock_db_with_peer,
    ):
        """A paired peer who somehow observed another peer's request_id
        cannot poll it — the pubkey binding closes that hole."""
        responder = FederationQueryResponder(db=mock_db_with_peer)
        responder._run_query = AsyncMock()

        init_req = _sign_initiate(asker_identity)
        init_resp = await responder.handle_initiate(init_req)

        # A different asker tries to poll (valid signature with THEIR key).
        attacker = FederationIdentity(ed25519.Ed25519PrivateKey.generate())
        poll = _sign_retrieve(attacker, init_resp.request_id)
        resp = await responder.handle_retrieve(poll)
        assert resp.status == STATUS_EXPIRED  # uniform — no oracle

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_terminal_response_is_signed(
        self, responder_identity, asker_identity, mock_db_with_peer,
    ):
        """A completed request returns a response signed with responder
        identity over `complete_canonical_payload`."""
        responder = FederationQueryResponder(db=mock_db_with_peer)
        # Inject a completed pending entry manually (skip the bg task).
        from services.atom_types import Provenance

        pending = _PendingRequest(
            request_id="test-req-1",
            asker_pubkey=asker_identity.public_key_hex(),
            peer_user_id=77,
            asker_local_user_id=42,
            max_visible_tier=2,
            query="q",
            initiated_at=time.time(),
            status=STATUS_COMPLETE,
            progress_label=PROGRESS_LABEL_COMPLETE,
            answer="Mom said pasta.",
            provenance=[Provenance(
                atom_id="AAAA-1111",
                atom_type="conversation_memory",
                display_label="from mom's recipes",
                score=0.87,
            ).redacted_for_remote()],
            answered_at=time.time(),
        )
        await get_pending_store().put(pending)

        poll = _sign_retrieve(asker_identity, pending.request_id)
        resp = await responder.handle_retrieve(poll)
        assert resp.status == STATUS_COMPLETE
        assert resp.answer == "Mom said pasta."
        assert resp.responder_pubkey == responder_identity.public_key_hex()
        assert resp.responder_signature is not None

        # Asker (a third party holding the responder's pubkey) MUST be
        # able to verify the signature.
        ok = FederationIdentity.verify(
            bytes.fromhex(resp.responder_pubkey),
            bytes.fromhex(resp.responder_signature),
            _canonical_bytes(complete_canonical_payload(resp)),
        )
        assert ok


# =============================================================================
# Progress rate limiting
# =============================================================================


class TestBackgroundTaskSession:
    """Regression guard for CRITICAL review finding #1 — bg task MUST
    open its own AsyncSession. Using the request-scoped session would
    hit a closed-session error after the initiate HTTP handler returns."""

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_run_query_opens_fresh_session_via_AsyncSessionLocal(
        self, responder_identity, asker_identity, monkeypatch,
    ):
        """Patch AsyncSessionLocal to track whether _run_query opened
        its own session — if it reuses self.db, the patched factory
        is never called."""
        from services import federation_query_responder as fqr

        factory_calls = 0
        _real_factory = fqr.AsyncSessionLocal

        def factory():
            nonlocal factory_calls
            factory_calls += 1
            return _SessionSeeingAnActivePairing()

        monkeypatch.setattr(fqr, "AsyncSessionLocal", factory)

        # Inject a ready-to-run pending entry.
        pending = _PendingRequest(
            request_id="bg-test-1",
            asker_pubkey=asker_identity.public_key_hex(),
            peer_user_id=1,
            asker_local_user_id=2,
            max_visible_tier=2,
            query="q",
            initiated_at=time.time(),
        )
        await get_pending_store().put(pending)

        responder = FederationQueryResponder(db=MagicMock())
        # Stub _retrieve + _synthesize so we only exercise the session-
        # acquisition path.
        responder._retrieve = AsyncMock(return_value=[])
        responder._synthesize = AsyncMock(return_value="answer")

        await responder._run_query(pending.request_id)

        # One for the retrieval, and one per look at whether the pairing is
        # still active (before the work, before synthesis and before the
        # result is stored).
        assert factory_calls == 4, (
            "bg task did not open its own session via AsyncSessionLocal — "
            "the request-scoped session from handle_initiate would already "
            "be closed. CRITICAL regression."
        )
        # Re-fetch because the bg task saves back through the store.
        pending_after = await get_pending_store().get(pending.request_id)
        assert pending_after.status == STATUS_COMPLETE

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_run_query_outer_try_catches_everything(
        self, responder_identity, asker_identity, monkeypatch,
    ):
        """If _retrieve raises any Exception (ImportError, KeyError,
        anything), the outer try/except must still mark the pending
        as STATUS_FAILED + set answered_at so the asker sees a
        terminal status instead of polling into TTL."""

        pending = _PendingRequest(
            request_id="bg-test-fail",
            asker_pubkey=asker_identity.public_key_hex(),
            peer_user_id=1,
            asker_local_user_id=2,
            max_visible_tier=2,
            query="q",
            initiated_at=time.time(),
        )
        await get_pending_store().put(pending)

        from services import federation_query_responder as fqr
        monkeypatch.setattr(fqr, "AsyncSessionLocal", _SessionSeeingAnActivePairing)

        responder = FederationQueryResponder(db=MagicMock())
        responder._retrieve = AsyncMock(side_effect=RuntimeError("boom"))

        # Must not raise out of the bg task.
        await responder._run_query(pending.request_id)

        pending_after = await get_pending_store().get(pending.request_id)
        assert pending_after.status == STATUS_FAILED
        assert pending_after.error_message == "RuntimeError: boom"
        assert pending_after.answered_at is not None  # set on failure per fix


class TestTheRetrievalStepIsBounded:
    """🛑 The timing chain was asserted in prose only. Each source of the
    fan-out has a deadline, but graph expansion runs after it with none, and
    the far side stops polling when the request window (60 s) closes."""

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_retrieval_that_never_returns_ends_as_failed(
        self, responder_identity, asker_identity, monkeypatch,
    ):
        import asyncio

        from services import federation_query_responder as fqr

        pending = _PendingRequest(
            request_id="bg-test-slow", asker_pubkey=asker_identity.public_key_hex(),
            peer_user_id=1, asker_local_user_id=None, max_visible_tier=2,
            query="q", initiated_at=time.time(), peer_owner_id=1,
        )
        await get_pending_store().put(pending)
        monkeypatch.setattr(fqr, "AsyncSessionLocal", _SessionSeeingAnActivePairing)
        # 60 - 54.8 - 5 headroom: the deadline is DERIVED from the window.
        monkeypatch.setattr(settings, "federation_synthesis_timeout", 54.8)
        assert settings.federation_retrieve_deadline == pytest.approx(0.2)

        cancelled = []

        async def stuck(_session, _pending):
            try:
                await asyncio.sleep(3600)      # graph expansion that never ends
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        responder = FederationQueryResponder(db=MagicMock())
        responder._retrieve = stuck
        responder._synthesize = AsyncMock(return_value="never reached")

        await asyncio.wait_for(responder._run_query(pending.request_id), 5)

        assert cancelled == [True]
        responder._synthesize.assert_not_awaited()
        after = await get_pending_store().get(pending.request_id)
        assert after.status == STATUS_FAILED
        # The backstop names the step and the deadline. It read
        # `TimeoutError: ` with nothing after the colon: `str(TimeoutError())`
        # is empty.
        assert after.error_message == (
            "RetrievalDeadlineExceeded: retrieval step did not finish within 0.2 s")
        assert after.retrieved_count is None      # nothing was handed on

    @pytest.mark.unit
    def test_the_deadline_is_what_the_window_leaves(self):
        from services.federation_pending_store import REQUEST_TTL_SECONDS
        from services.federation_query_asker import MAX_POLL_DURATION_SECONDS
        from utils.config import (
            FEDERATION_ANSWER_HEADROOM_SECONDS,
            FEDERATION_REQUEST_TTL_SECONDS,
            Settings,
        )

        # One window on both sides of the wire.
        assert REQUEST_TTL_SECONDS == FEDERATION_REQUEST_TTL_SECONDS == MAX_POLL_DURATION_SECONDS
        s = Settings(_env_file=None)
        assert s.federation_retrieve_deadline == pytest.approx(
            FEDERATION_REQUEST_TTL_SECONDS - s.federation_synthesis_timeout
            - FEDERATION_ANSWER_HEADROOM_SECONDS)
        # The defaults fit, with room for the fan-out's own deadline.
        assert s.atom_fanout_source_timeout <= s.federation_retrieve_deadline
        # The retrieval's own budget ends a margin before the step does; the
        # sources must fit into THAT, and the defaults leave expansion time.
        assert s.federation_retrieve_budget == pytest.approx(
            s.federation_retrieve_deadline - 1.0)
        assert s.atom_fanout_source_timeout < s.federation_retrieve_budget
        # A short step keeps most of itself: the margin is at most a tenth.
        short = Settings(_env_file=None, federation_synthesis_timeout=54.0,
                         atom_fanout_source_timeout=0.5)
        assert short.federation_retrieve_budget == pytest.approx(0.9)

    @pytest.mark.unit
    @pytest.mark.parametrize("source, synthesis", [
        (29.0, 59.0),      # both field maxima: 88 s into a 60 s window
        (20.0, 35.5),      # the default source deadline, synthesis a little long
        (25.1, 30.0),      # the default synthesis, source a little long
        (24.5, 30.0),      # inside the step, but over the retrieval's budget
        (25.0, 30.0),      # exactly the step
        (24.0, 30.0),      # exactly the BUDGET: the source's timer and the
        (20.0, 34.0),      # fan-out's race, and the scheduler decides between
                           # one crashed source and a failed query
    ])
    def test_a_configuration_that_cannot_fit_refuses_to_start(self, source, synthesis):
        from pydantic import ValidationError

        from utils.config import Settings

        with pytest.raises(ValidationError) as refused:
            Settings(_env_file=None, atom_fanout_source_timeout=source,
                     federation_synthesis_timeout=synthesis)
        text = str(refused.value)
        assert "ATOM_FANOUT_SOURCE_TIMEOUT" in text
        assert "FEDERATION_SYNTHESIS_TIMEOUT" in text

    @pytest.mark.unit
    @pytest.mark.parametrize("source, synthesis", [
        (20.0, 30.0), (23.9, 30.0), (20.0, 33.9), (0.1, 54.8), (27.9, 26.0),
    ])
    def test_a_configuration_that_fits_starts(self, source, synthesis):
        """Negative control, up to the boundary: a source stays BELOW the
        retrieval's budget."""
        from utils.config import Settings

        s = Settings(_env_file=None, atom_fanout_source_timeout=source,
                     federation_synthesis_timeout=synthesis)
        assert s.atom_fanout_source_timeout < s.federation_retrieve_budget


class TestProgressRateLimit:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_emit_progress_caps_at_max_updates(
        self, responder_identity, asker_identity,
    ):
        """Traffic-analysis defence: responder can't phase-by-phase
        telegraph timing beyond a fixed chunk count."""
        pending = _PendingRequest(
            request_id="x",
            asker_pubkey=asker_identity.public_key_hex(),
            peer_user_id=1,
            asker_local_user_id=2,
            max_visible_tier=2,
            query="q",
            initiated_at=time.time(),
        )
        await get_pending_store().put(pending)
        for _i in range(10):
            await FederationQueryResponder._emit_progress(pending, PROGRESS_LABEL_SYNTHESIZING)
        assert pending.progress_count == MAX_PROGRESS_UPDATES


class TestEnforceCirclesOnRetrieve:
    """The responder's retrieval MUST run PolymorphicAtomStore.query with
    `peer` gesetzt — otherwise an auth-off (household) responder bypasses
    every circle filter and leaks its whole brain, and even with auth on the
    remote-supplied asker_id can collide with a local owner id (see the
    circle_sql peer_scoped fix)."""

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_retrieve_forces_a_named_peer_scope(self, responder_identity, monkeypatch):
        import services.polymorphic_atom_store as pas
        from services.federation_pending_store import _PendingRequest

        captured = {}

        class _FakeStore:
            expansion_skipped = False

            def __init__(self, session, *, source_sessions=None):
                captured["source_sessions"] = source_sessions

            async def query(self, query_text, *, asker_id, max_visible_tier, top_k,
                            peer=None, federated=False, deadline=None):
                captured["deadline"] = deadline
                captured["peer"] = peer
                captured["federated"] = federated
                captured["asker_id"] = asker_id
                captured["max_visible_tier"] = max_visible_tier
                return []

        monkeypatch.setattr(pas, "PolymorphicAtomStore", _FakeStore)

        responder = FederationQueryResponder(db=MagicMock())
        pending = _PendingRequest(
            request_id="r1",
            asker_pubkey="ab" * 32,
            peer_user_id=77,
            # 🛑 On the fallback path there is NO asking local person any
            # more. This used to read `asker_local_user_id=42` --
            # `peer.remote_user_id`, a number belonging to the far side that
            # the filter read as a local id.
            asker_local_user_id=None,
            max_visible_tier=2,
            peer_owner_id=1,
            query="what does the household know?",
            initiated_at=time.time(),
        )

        # _retrieve opens its own AsyncSessionLocal; patch it to a dummy CM.
        import services.federation_query_responder as fqr

        class _DummySession:
            async def __aenter__(self):
                return MagicMock()

            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr(fqr, "AsyncSessionLocal", lambda: _DummySession())

        async with fqr.AsyncSessionLocal() as session:
            await responder._retrieve(session, pending)

        # 🛑 The peer-scoped filter runs -- and it NAMES the peer. A bare
        # "yes, enforce" was the defect: it left open WHICH peer, and the
        # filter then reached for the asker's id.
        assert captured["peer"] is not None
        assert captured["peer"].owner_id == 1
        assert captured["peer"].tier == 2
        # 🛑 The path is STATED, not derived from `peer`. On the mapped path
        # `peer` is `None`, so the cold run would otherwise stay unmarked --
        # and cold against warm is the whole difference (measured: 1 of 8
        # sources against 8 of 8).
        assert captured["federated"] is True
        # The store gets the retrieval's budget, so it handles the bound
        # itself before the step's backstop does.
        assert captured["deadline"] == settings.federation_retrieve_budget
        # 🛑 The session handed to `_retrieve` is fresh, so the fan-out must
        # NOT share it: every source gets a session of its own, from the one
        # process-wide `SourceSessions` (which is what bounds them).
        assert captured["source_sessions"] is fqr._SOURCE_SESSIONS
        # The id is 0 (the placeholder), and that has no effect: in the
        # peer-scoped branch `circles_filter_params` no longer creates an
        # `:asker_id` binding at all.
        assert captured["asker_id"] == 0


class TestPersonScopedMapping:
    """F-ID-1: a querier_ref that maps (via federation_user_links) to a local
    user must serve AS that user (`peer=None`); everything else --
    flag off, no ref, no link -- must fall back to the peer-scoped path
    (`peer_owner_id` set). The pending record carries the decision."""

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_flag_off_ignores_querier_ref_and_falls_back(
        self, responder_identity, asker_identity, mock_db_with_peer, monkeypatch,
    ):
        monkeypatch.setattr(settings, "federation_identity_links_enabled", False)
        responder = FederationQueryResponder(db=mock_db_with_peer)
        responder._run_query = AsyncMock()

        req = _sign_initiate(asker_identity, querier_ref="deadbeef" * 8)
        resp = await responder.handle_initiate(req)

        pending = await get_pending_store().get(resp.request_id)
        assert pending.peer_owner_id == 1                      # fallback: bound to the owner
        # 🛑 The fallback no longer borrows a local id. This used to read
        # `== 42`, i.e. `peer.remote_user_id` -- exactly the foreign number the
        # filter read as a local person, which on 2026-10-27 hit the wrong
        # person in the household and broke the foreign key on x-idra.
        assert pending.asker_local_user_id is None

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_flag_on_no_link_falls_back(
        self, responder_identity, asker_identity, mock_db_with_peer, monkeypatch,
    ):
        monkeypatch.setattr(settings, "federation_identity_links_enabled", True)
        monkeypatch.setattr(settings, "auth_enabled", True)
        # resolve_link is imported inside _handle_initiate — patch at source.
        import services.federation_identity_link as fil
        monkeypatch.setattr(fil, "resolve_link", AsyncMock(return_value=None))

        responder = FederationQueryResponder(db=mock_db_with_peer)
        responder._run_query = AsyncMock()

        req = _sign_initiate(asker_identity, querier_ref="deadbeef" * 8)
        resp = await responder.handle_initiate(req)

        pending = await get_pending_store().get(resp.request_id)
        assert pending.peer_owner_id == 1                      # fallback: bound to the owner
        assert pending.asker_local_user_id is None

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_flag_on_with_link_maps_to_local_user(
        self, responder_identity, asker_identity, mock_db_with_peer, monkeypatch,
    ):
        monkeypatch.setattr(settings, "federation_identity_links_enabled", True)
        monkeypatch.setattr(settings, "auth_enabled", True)
        import services.federation_identity_link as fil
        monkeypatch.setattr(fil, "resolve_link", AsyncMock(return_value=(5, 99)))

        responder = FederationQueryResponder(db=mock_db_with_peer)
        responder._run_query = AsyncMock()

        req = _sign_initiate(asker_identity, querier_ref="cafebabe" * 8)
        resp = await responder.handle_initiate(req)

        pending = await get_pending_store().get(resp.request_id)
        assert pending.peer_owner_id is None                   # MAPPED -- runs as the person
        assert pending.asker_local_user_id == 99               # the linked local user
        # ... and the link it was admitted on, so it can be asked about again.
        assert pending.link_id == 5 and pending.mapped is True

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_auth_off_never_maps_even_with_link(
        self, responder_identity, asker_identity, mock_db_with_peer, monkeypatch,
    ):
        # SECURITY: under AUTH_ENABLED=false a mapped path would inherit the
        # single-user full-bypass and leak the WHOLE brain. The mapped path must
        # be refused (fall back to peer-scoped) regardless of a matching link.
        monkeypatch.setattr(settings, "federation_identity_links_enabled", True)
        monkeypatch.setattr(settings, "auth_enabled", False)
        import services.federation_identity_link as fil
        spy = AsyncMock(return_value=(5, 99))
        monkeypatch.setattr(fil, "resolve_link", spy)

        responder = FederationQueryResponder(db=mock_db_with_peer)
        responder._run_query = AsyncMock()

        req = _sign_initiate(asker_identity, querier_ref="cafebabe" * 8)
        resp = await responder.handle_initiate(req)

        spy.assert_not_awaited()                               # never even consults the link
        pending = await get_pending_store().get(resp.request_id)
        assert pending.peer_owner_id == 1                      # forced fallback
        # No borrowed id here either -- and above all not the linked user's:
        # with auth off there is NO mapping.
        assert pending.asker_local_user_id is None

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_no_querier_ref_never_consults_links(
        self, responder_identity, asker_identity, mock_db_with_peer, monkeypatch,
    ):
        monkeypatch.setattr(settings, "federation_identity_links_enabled", True)
        import services.federation_identity_link as fil
        spy = AsyncMock(return_value=(5, 99))
        monkeypatch.setattr(fil, "resolve_link", spy)

        responder = FederationQueryResponder(db=mock_db_with_peer)
        responder._run_query = AsyncMock()

        req = _sign_initiate(asker_identity)  # no querier_ref
        resp = await responder.handle_initiate(req)

        spy.assert_not_awaited()                               # short-circuit on empty ref
        pending = await get_pending_store().get(resp.request_id)
        assert pending.peer_owner_id == 1


class TestQuerierRefCanonicalPayload:
    """The querier_ref must be in the signed bytes ONLY when set — so a flag-off
    asker's payload is byte-identical to pre-F-ID-1 (backward-compatible
    signatures) and a stripped ref flips the verifier's recomputed payload."""

    @pytest.mark.unit
    def test_absent_querier_ref_omitted_from_payload(self, asker_identity):
        req = _sign_initiate(asker_identity)  # no querier_ref
        payload = initiate_canonical_payload(req)
        assert "querier_ref" not in payload

    @pytest.mark.unit
    def test_present_querier_ref_included_in_payload(self, asker_identity):
        req = _sign_initiate(asker_identity, querier_ref="abc123" * 4)
        payload = initiate_canonical_payload(req)
        assert payload["querier_ref"] == "abc123" * 4

    @pytest.mark.unit
    def test_signature_covers_querier_ref(self, asker_identity):
        # A ref-bearing envelope verifies; stripping the ref (recompute without
        # it) breaks verification — proving the ref is bound under the signature.
        req = _sign_initiate(asker_identity, querier_ref="feed" * 8)
        good = _canonical_bytes(initiate_canonical_payload(req))
        assert FederationIdentity.verify(
            bytes.fromhex(req.asker_pubkey), bytes.fromhex(req.signature), good
        )
        stripped = dict(initiate_canonical_payload(req))
        stripped.pop("querier_ref")
        assert not FederationIdentity.verify(
            bytes.fromhex(req.asker_pubkey), bytes.fromhex(req.signature),
            _canonical_bytes(stripped),
        )
