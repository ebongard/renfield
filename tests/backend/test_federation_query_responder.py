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

import asyncio
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
from services.federation_query_responder import (
    FederationQueryError,
    FederationQueryResponder,
    MAX_PROGRESS_UPDATES,
    _PendingRequest,
    _clear_state_for_tests,
    _pending_requests,
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
    retrieve_canonical_payload,
)
from services.mcp_streaming import (
    PROGRESS_LABEL_COMPLETE,
    PROGRESS_LABEL_RETRIEVING,
    PROGRESS_LABEL_SYNTHESIZING,
)
from services.pairing_service import _canonical_bytes


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def responder_identity(tmp_path):
    """Fresh federation identity for the responder side + reset state."""
    reset_federation_identity_for_tests()
    init_federation_identity(tmp_path / "responder_key")
    _clear_state_for_tests()
    yield get_federation_identity()
    reset_federation_identity_for_tests()
    _clear_state_for_tests()


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
    peer.revoked_at = None

    db.execute = AsyncMock()
    # Default: first execute returns peer, subsequent return membership tier
    membership = MagicMock(value=2)  # responder granted asker tier=2 (household)

    async def execute_side(stmt):
        result = MagicMock()
        if "peer_users" in str(stmt):
            result.scalar_one_or_none = lambda: peer
        elif "circle_memberships" in str(stmt):
            result.scalar_one_or_none = lambda: membership
        else:
            result.scalar_one_or_none = lambda: None
        return result

    db.execute.side_effect = execute_side
    db.commit = AsyncMock()
    return db


# =============================================================================
# Helpers
# =============================================================================


def _sign_initiate(
    asker: FederationIdentity,
    query: str = "what is mom's recipe?",
    nonce: str | None = None,
    timestamp: int | None = None,
    depth: int = 3,
    path: list[str] | None = None,
) -> QueryBrainInitiateRequest:
    """Build a signed v2 initiate envelope.

    `path` defaults to `[asker.pubkey]` — the realistic first-hop shape
    after F5a. Tests that want to exercise cycle detection pass an
    explicit `path` carrying the responder's own pubkey.
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
        pending = _pending_requests[resp.request_id]
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

        with pytest.raises(FederationQueryError, match="path|asker_pubkey"):
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
        from services.federation_query_responder import _pending_requests
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
        _pending_requests[pending.request_id] = pending

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
        real_factory = fqr.AsyncSessionLocal

        class _TrackedSession:
            """Minimal async-ctx-mgr stand-in for AsyncSession."""
            async def __aenter__(self):
                return MagicMock()
            async def __aexit__(self, *a):
                return False

        def factory():
            nonlocal factory_calls
            factory_calls += 1
            return _TrackedSession()

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
        fqr._pending_requests[pending.request_id] = pending

        responder = FederationQueryResponder(db=MagicMock())
        # Stub _retrieve + _synthesize so we only exercise the session-
        # acquisition path.
        responder._retrieve = AsyncMock(return_value=[])
        responder._synthesize = AsyncMock(return_value="answer")

        await responder._run_query(pending.request_id)

        assert factory_calls == 1, (
            "bg task did not open its own session via AsyncSessionLocal — "
            "the request-scoped session from handle_initiate would already "
            "be closed. CRITICAL regression."
        )
        # And the pending should be marked COMPLETE by the full run.
        assert pending.status == STATUS_COMPLETE

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_run_query_outer_try_catches_everything(
        self, responder_identity, asker_identity,
    ):
        """If _retrieve raises any Exception (ImportError, KeyError,
        anything), the outer try/except must still mark the pending
        as STATUS_FAILED + set answered_at so the asker sees a
        terminal status instead of polling into TTL."""
        from services import federation_query_responder as fqr

        pending = _PendingRequest(
            request_id="bg-test-fail",
            asker_pubkey=asker_identity.public_key_hex(),
            peer_user_id=1,
            asker_local_user_id=2,
            max_visible_tier=2,
            query="q",
            initiated_at=time.time(),
        )
        fqr._pending_requests[pending.request_id] = pending

        responder = FederationQueryResponder(db=MagicMock())
        responder._retrieve = AsyncMock(side_effect=RuntimeError("boom"))

        # Must not raise out of the bg task.
        await responder._run_query(pending.request_id)

        assert pending.status == STATUS_FAILED
        assert pending.error_message == "boom"
        assert pending.answered_at is not None  # set on failure per fix


class TestProgressRateLimit:
    @pytest.mark.unit
    def test_emit_progress_caps_at_max_updates(
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
        for i in range(10):
            FederationQueryResponder._emit_progress(pending, PROGRESS_LABEL_SYNTHESIZING)
        assert pending.progress_count == MAX_PROGRESS_UPDATES
