"""
Tests for F3b — federation query_brain asker.

Coverage:
- Happy path: initiate → poll processing (N chunks) → complete with
  valid signature → FinalResult success=True.
- Responder signature verification failure → FinalResult success=False.
- Terminal STATUS_FAILED / STATUS_EXPIRED mapped to FinalResult error.
- Transport error on initiate → FinalResult error.
- Timeout (deadline exceeded) → FinalResult error.
- Unknown progress label falls back to PROGRESS_LABEL_TOOL_RUNNING.
- ProgressChunk emission is deduped (same label repeated on sequential
  polls emits only once).
- Peer with no endpoint → FinalResult error before any HTTP call.
"""
from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from services.federation_identity import (
    FederationIdentity,
    get_federation_identity,
    init_federation_identity,
    reset_federation_identity_for_tests,
)
from services.federation_query_asker import (
    FederationQueryAsker,
    _final_error,
    _select_endpoint,
)
from services.federation_query_schemas import (
    STATUS_COMPLETE,
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_PROCESSING,
    FederationProvenance,
    QueryBrainInitiateResponse,
    QueryBrainRetrieveResponse,
    complete_canonical_payload,
)
from services.mcp_streaming import (
    PROGRESS_LABEL_COMPLETE,
    PROGRESS_LABEL_RETRIEVING,
    PROGRESS_LABEL_SYNTHESIZING,
    PROGRESS_LABEL_TOOL_RUNNING,
    ProgressChunk,
)
from services.pairing_service import _canonical_bytes


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def asker_identity(tmp_path):
    """Fresh Renfield-local identity (the asker's)."""
    reset_federation_identity_for_tests()
    init_federation_identity(tmp_path / "asker_key")
    yield get_federation_identity()
    reset_federation_identity_for_tests()


@pytest.fixture
def responder_identity():
    """Independent identity representing the remote peer we're querying."""
    return FederationIdentity(ed25519.Ed25519PrivateKey.generate())


@pytest.fixture
def mock_peer(responder_identity):
    """A PeerUser-shaped MagicMock pointing at a fake endpoint."""
    peer = MagicMock()
    peer.id = 1
    peer.remote_pubkey = responder_identity.public_key_hex()
    peer.remote_display_name = "Mom"
    peer.transport_config = {
        "endpoints": ["http://mom.local:8000"],
    }
    return peer


def _signed_complete(
    responder_identity: FederationIdentity,
    *,
    answer: str = "Pasta with tomato.",
) -> QueryBrainRetrieveResponse:
    """Build a valid responder-signed terminal response."""
    resp = QueryBrainRetrieveResponse(
        status=STATUS_COMPLETE,
        answer=answer,
        provenance=[FederationProvenance(
            atom_id="redacted-uuid",
            atom_type="conversation_memory",
            display_label="from mom's recipes",
            score=0.9,
        )],
        answered_at=int(time.time()),
        responder_pubkey=responder_identity.public_key_hex(),
    )
    payload = complete_canonical_payload(resp)
    resp.responder_signature = responder_identity.sign(_canonical_bytes(payload)).hex()
    return resp


class _FakeClient:
    """Minimal httpx.AsyncClient stand-in. Each POST maps by path suffix
    to a pre-registered response sequence (or a single response)."""

    def __init__(self):
        self.responses: dict[str, list] = {}
        self.post_count: dict[str, int] = {}

    def register(self, path_suffix: str, responses: list[httpx.Response]):
        self.responses[path_suffix] = list(responses)
        self.post_count[path_suffix] = 0

    async def post(self, url: str, json: dict[str, Any] | None = None) -> httpx.Response:
        for suffix, queue in self.responses.items():
            if url.endswith(suffix):
                self.post_count[suffix] = self.post_count.get(suffix, 0) + 1
                if not queue:
                    raise AssertionError(f"_FakeClient ran out of responses for {suffix}")
                return queue.pop(0)
        raise AssertionError(f"_FakeClient: no response registered for {url}")

    async def aclose(self):
        pass


def _mk_http_200(body: dict) -> httpx.Response:
    return httpx.Response(200, json=body)


# =============================================================================
# Happy path
# =============================================================================


class TestHappyPath:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_initiate_poll_complete(
        self, asker_identity, responder_identity, mock_peer,
    ):
        """The full happy path: initiate → 2 processing polls → signed
        complete. Must yield 2 ProgressChunks + 1 FinalResult(success=True)."""
        fake = _FakeClient()
        # Initiate returns a fresh request_id.
        fake.register(
            "query_brain/initiate",
            [_mk_http_200(QueryBrainInitiateResponse(
                request_id="req-1", accepted_at=int(time.time()),
            ).model_dump())],
        )
        # Two polls in 'processing' with different progress labels + final signed complete.
        fake.register(
            "query_brain/retrieve",
            [
                _mk_http_200(QueryBrainRetrieveResponse(
                    status=STATUS_PROCESSING, progress=PROGRESS_LABEL_RETRIEVING,
                ).model_dump()),
                _mk_http_200(QueryBrainRetrieveResponse(
                    status=STATUS_PROCESSING, progress=PROGRESS_LABEL_SYNTHESIZING,
                ).model_dump()),
                _mk_http_200(
                    _signed_complete(responder_identity, answer="Pasta!").model_dump()
                ),
            ],
        )

        asker = FederationQueryAsker(client=fake)
        items = []
        async for item in asker.query_peer(mock_peer, "recipe?"):
            items.append(item)

        # Two distinct progress labels → two ProgressChunks
        chunks = [i for i in items if isinstance(i, ProgressChunk)]
        assert len(chunks) == 2
        assert chunks[0].label == PROGRESS_LABEL_RETRIEVING
        assert chunks[1].label == PROGRESS_LABEL_SYNTHESIZING
        assert chunks[0].detail["peer"] == "Mom"
        assert chunks[0].sequence == 1
        assert chunks[1].sequence == 2

        # One FinalResult at the end.
        finals = [i for i in items if not isinstance(i, ProgressChunk)]
        assert len(finals) == 1
        assert finals[0]["success"] is True
        assert finals[0]["message"] == "Pasta!"
        assert "provenance" in finals[0]["data"]

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_duplicate_progress_labels_deduped(
        self, asker_identity, responder_identity, mock_peer,
    ):
        """Two consecutive polls returning the SAME progress label
        must yield only ONE ProgressChunk — no UI flood."""
        fake = _FakeClient()
        fake.register(
            "query_brain/initiate",
            [_mk_http_200(QueryBrainInitiateResponse(
                request_id="req-2", accepted_at=int(time.time()),
            ).model_dump())],
        )
        fake.register(
            "query_brain/retrieve",
            [
                _mk_http_200(QueryBrainRetrieveResponse(
                    status=STATUS_PROCESSING, progress=PROGRESS_LABEL_RETRIEVING,
                ).model_dump()),
                _mk_http_200(QueryBrainRetrieveResponse(
                    status=STATUS_PROCESSING, progress=PROGRESS_LABEL_RETRIEVING,
                ).model_dump()),
                _mk_http_200(
                    _signed_complete(responder_identity).model_dump()
                ),
            ],
        )

        asker = FederationQueryAsker(client=fake)
        items = [it async for it in asker.query_peer(mock_peer, "q")]

        chunks = [i for i in items if isinstance(i, ProgressChunk)]
        assert len(chunks) == 1  # Dedup — only one retrieving chunk

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_unknown_progress_label_falls_back_to_tool_running(
        self, asker_identity, responder_identity, mock_peer,
    ):
        """A misbehaving responder returning a label not in PROGRESS_LABELS
        (simulating an exfil/side-channel attempt) must be coerced to
        PROGRESS_LABEL_TOOL_RUNNING on the asker's side — asker's UI
        never displays the raw string."""
        fake = _FakeClient()
        fake.register(
            "query_brain/initiate",
            [_mk_http_200(QueryBrainInitiateResponse(
                request_id="req-3", accepted_at=int(time.time()),
            ).model_dump())],
        )
        fake.register(
            "query_brain/retrieve",
            [
                _mk_http_200({
                    "status": STATUS_PROCESSING,
                    "progress": "peer_has_47_atoms",  # leaked label
                }),
                _mk_http_200(_signed_complete(responder_identity).model_dump()),
            ],
        )

        asker = FederationQueryAsker(client=fake)
        items = [it async for it in asker.query_peer(mock_peer, "q")]
        chunks = [i for i in items if isinstance(i, ProgressChunk)]
        assert chunks[0].label == PROGRESS_LABEL_TOOL_RUNNING


# =============================================================================
# Signature verification on terminal response
# =============================================================================


class TestResponderSignatureVerification:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_missing_responder_signature_rejected(
        self, asker_identity, responder_identity, mock_peer,
    ):
        """A 'complete' response without a signature must fail — the
        asker does not trust the answer without cryptographic proof
        it came from the claimed pubkey."""
        fake = _FakeClient()
        fake.register(
            "query_brain/initiate",
            [_mk_http_200(QueryBrainInitiateResponse(
                request_id="req-sig", accepted_at=int(time.time()),
            ).model_dump())],
        )
        fake.register(
            "query_brain/retrieve",
            [_mk_http_200({
                "status": STATUS_COMPLETE,
                "answer": "forged answer",
                "provenance": [],
                "answered_at": int(time.time()),
                "responder_pubkey": responder_identity.public_key_hex(),
                # signature field missing — should be rejected
            })],
        )

        asker = FederationQueryAsker(client=fake)
        items = [it async for it in asker.query_peer(mock_peer, "q")]
        final = items[-1]
        assert final["success"] is False
        assert "signature" in final["message"].lower() or "missing" in final["message"].lower()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_tampered_signature_rejected(
        self, asker_identity, responder_identity, mock_peer,
    ):
        """An attacker-in-the-middle flipping a byte of the answer
        invalidates the responder signature — asker rejects."""
        fake = _FakeClient()
        fake.register(
            "query_brain/initiate",
            [_mk_http_200(QueryBrainInitiateResponse(
                request_id="req-tamper", accepted_at=int(time.time()),
            ).model_dump())],
        )

        # Build a valid signed response, then mutate the answer AFTER
        # signing. Signature no longer covers the new payload.
        resp = _signed_complete(responder_identity, answer="original")
        tampered = resp.model_dump()
        tampered["answer"] = "mitm answer"
        fake.register("query_brain/retrieve", [_mk_http_200(tampered)])

        asker = FederationQueryAsker(client=fake)
        items = [it async for it in asker.query_peer(mock_peer, "q")]
        final = items[-1]
        assert final["success"] is False
        assert "forged" in final["message"].lower() or "verification" in final["message"].lower()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_wrong_pubkey_rejected(
        self, asker_identity, responder_identity, mock_peer,
    ):
        """Responder claims one pubkey but signs with another — Ed25519
        verify fails against the claimed pubkey."""
        attacker = FederationIdentity(ed25519.Ed25519PrivateKey.generate())
        fake = _FakeClient()
        fake.register(
            "query_brain/initiate",
            [_mk_http_200(QueryBrainInitiateResponse(
                request_id="req-wrong-key", accepted_at=int(time.time()),
            ).model_dump())],
        )

        # Responder claims they're `responder_identity` but signs with `attacker`.
        resp = QueryBrainRetrieveResponse(
            status=STATUS_COMPLETE,
            answer="injected",
            provenance=[],
            answered_at=int(time.time()),
            responder_pubkey=responder_identity.public_key_hex(),  # claim
        )
        resp.responder_signature = attacker.sign(
            _canonical_bytes(complete_canonical_payload(resp))
        ).hex()

        fake.register("query_brain/retrieve", [_mk_http_200(resp.model_dump())])

        asker = FederationQueryAsker(client=fake)
        items = [it async for it in asker.query_peer(mock_peer, "q")]
        final = items[-1]
        assert final["success"] is False

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_attacker_key_signs_for_itself_rejected_by_pair_anchor(
        self, asker_identity, mock_peer,
    ):
        """CRITICAL regression (review #1): a MITM who replaces the
        response with a message signed by their OWN key, and sets
        `responder_pubkey` to that same attacker key, passes raw Ed25519
        verification (attacker signed + pubkey matches signer) — but
        the pair-anchor check rejects it because the claimed pubkey
        doesn't match `peer.remote_pubkey`."""
        attacker = FederationIdentity(ed25519.Ed25519PrivateKey.generate())
        fake = _FakeClient()
        fake.register(
            "query_brain/initiate",
            [_mk_http_200(QueryBrainInitiateResponse(
                request_id="req-mitm", accepted_at=int(time.time()),
            ).model_dump())],
        )

        # Build a response signed by the ATTACKER with the attacker's
        # OWN pubkey. Raw Ed25519 verify(attacker_pk, attacker_sig, msg)
        # succeeds — but pair-anchor binding to peer.remote_pubkey
        # (= the legit responder's pubkey, from F2 pairing) fails.
        resp = QueryBrainRetrieveResponse(
            status=STATUS_COMPLETE,
            answer="MITM answer — trust me bro",
            provenance=[],
            answered_at=int(time.time()),
            responder_pubkey=attacker.public_key_hex(),  # NOT the paired peer's key
        )
        resp.responder_signature = attacker.sign(
            _canonical_bytes(complete_canonical_payload(resp))
        ).hex()

        fake.register("query_brain/retrieve", [_mk_http_200(resp.model_dump())])

        asker = FederationQueryAsker(client=fake)
        items = [it async for it in asker.query_peer(mock_peer, "q")]
        final = items[-1]
        assert final["success"] is False
        # Error message mentions the pair-anchor mismatch explicitly
        assert "match" in final["message"].lower() or "paired" in final["message"].lower()


# =============================================================================
# Non-success terminal statuses
# =============================================================================


class TestNonSuccessTerminal:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_status_failed_becomes_final_error(
        self, asker_identity, mock_peer,
    ):
        fake = _FakeClient()
        fake.register(
            "query_brain/initiate",
            [_mk_http_200(QueryBrainInitiateResponse(
                request_id="req-fail", accepted_at=int(time.time()),
            ).model_dump())],
        )
        fake.register(
            "query_brain/retrieve",
            [_mk_http_200(QueryBrainRetrieveResponse(
                status=STATUS_FAILED, answered_at=int(time.time()),
            ).model_dump())],
        )

        asker = FederationQueryAsker(client=fake)
        items = [it async for it in asker.query_peer(mock_peer, "q")]
        final = items[-1]
        assert final["success"] is False
        assert "failure" in final["message"].lower()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_status_expired_becomes_final_error(
        self, asker_identity, mock_peer,
    ):
        fake = _FakeClient()
        fake.register(
            "query_brain/initiate",
            [_mk_http_200(QueryBrainInitiateResponse(
                request_id="req-exp", accepted_at=int(time.time()),
            ).model_dump())],
        )
        fake.register(
            "query_brain/retrieve",
            [_mk_http_200(QueryBrainRetrieveResponse(status=STATUS_EXPIRED).model_dump())],
        )

        asker = FederationQueryAsker(client=fake)
        items = [it async for it in asker.query_peer(mock_peer, "q")]
        final = items[-1]
        assert final["success"] is False


# =============================================================================
# Transport / peer edge cases
# =============================================================================


class TestEdgeCases:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_no_endpoint_fails_before_http_call(
        self, asker_identity,
    ):
        """Peer with empty transport_config returns an error IMMEDIATELY
        without any HTTP — not caught as transport error, surfaced as
        a clear 'no usable transport' message."""
        peer = MagicMock()
        peer.remote_display_name = "Dad"
        peer.transport_config = {}  # no endpoints

        asker = FederationQueryAsker(client=_FakeClient())
        items = [it async for it in asker.query_peer(peer, "q")]
        assert len(items) == 1
        assert items[0]["success"] is False
        assert "endpoint" in items[0]["message"].lower()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_initiate_http_error_surfaces_as_final_error(
        self, asker_identity, mock_peer,
    ):
        """Transport error on initiate (connection refused, DNS fail,
        etc.) produces a FinalResult error rather than an exception."""
        class _FailingClient:
            async def post(self, url, json=None):
                raise httpx.ConnectError("unreachable")
            async def aclose(self):
                pass

        asker = FederationQueryAsker(client=_FailingClient())
        items = [it async for it in asker.query_peer(mock_peer, "q")]
        assert items[-1]["success"] is False

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_initiate_non_200_surfaces_as_final_error(
        self, asker_identity, mock_peer,
    ):
        """Responder's 400 (pairing handshake failed / signature bad /
        whatever) surfaces as an asker-side failure, not an exception."""
        fake = _FakeClient()
        fake.register(
            "query_brain/initiate",
            [httpx.Response(400, json={"detail": "federation query failed"})],
        )

        asker = FederationQueryAsker(client=fake)
        items = [it async for it in asker.query_peer(mock_peer, "q")]
        assert items[-1]["success"] is False

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_poll_deadline_exceeded_returns_timeout_error(
        self, asker_identity, mock_peer, monkeypatch,
    ):
        """If the responder stays 'processing' past MAX_POLL_DURATION,
        the asker yields a timeout FinalResult rather than polling
        forever. Simulate by feeding enough processing responses that
        the wall clock 'advances' past the deadline via monkey-patched
        time.time()."""
        from services import federation_query_asker as fqa

        fake = _FakeClient()
        fake.register(
            "query_brain/initiate",
            [_mk_http_200(QueryBrainInitiateResponse(
                request_id="req-timeout", accepted_at=int(time.time()),
            ).model_dump())],
        )
        # Infinite processing responses
        fake.register(
            "query_brain/retrieve",
            [_mk_http_200(QueryBrainRetrieveResponse(
                status=STATUS_PROCESSING, progress=PROGRESS_LABEL_RETRIEVING,
            ).model_dump()) for _ in range(10)],
        )

        # Advance clock in large jumps so the deadline is crossed quickly.
        clock = [time.time()]
        def fake_time():
            clock[0] += 30  # 30s per call — deadline (60s) trips after two calls
            return clock[0]
        monkeypatch.setattr(fqa.time, "time", fake_time)
        # Also patch the sleep to no-op so the test runs fast.
        async def no_sleep(_):
            return
        monkeypatch.setattr(fqa.asyncio, "sleep", no_sleep)

        asker = FederationQueryAsker(client=fake)
        items = [it async for it in asker.query_peer(mock_peer, "q")]
        final = items[-1]
        assert final["success"] is False
        assert "timed out" in final["message"].lower()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_consumer_break_closes_owned_client(
        self, asker_identity, responder_identity, monkeypatch,
    ):
        """When the asker doesn't inject a client, the `async with`
        inside query_peer guarantees cleanup on consumer break.
        Regression for review SHOULD-FIX #3."""
        # Patch httpx.AsyncClient to a spy that records aclose calls.
        close_count = 0

        class _SpyClient:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                nonlocal close_count
                close_count += 1
                return False
            async def post(self, url, json=None):
                # Return a fake initiate response then a processing poll
                if "initiate" in url:
                    return _mk_http_200(QueryBrainInitiateResponse(
                        request_id="req-cancel", accepted_at=int(time.time()),
                    ).model_dump())
                return _mk_http_200(QueryBrainRetrieveResponse(
                    status=STATUS_PROCESSING, progress=PROGRESS_LABEL_RETRIEVING,
                ).model_dump())

        from services import federation_query_asker as fqa
        monkeypatch.setattr(fqa.httpx, "AsyncClient", lambda **kwargs: _SpyClient())

        peer = MagicMock()
        peer.remote_pubkey = responder_identity.public_key_hex()
        peer.remote_display_name = "Mom"
        peer.transport_config = {"endpoints": ["http://mom.local:8000"]}

        asker = FederationQueryAsker()  # owned client path
        it = asker.query_peer(peer, "q")
        # Consume one progress chunk, then abort.
        await it.__anext__()
        await it.aclose()

        assert close_count == 1, "owned httpx client must be closed on consumer break"


# =============================================================================
# _select_endpoint unit
# =============================================================================


class TestSelectEndpoint:
    @pytest.mark.unit
    def test_list_of_strings(self):
        peer = MagicMock(transport_config={"endpoints": ["http://a", "http://b"]})
        assert _select_endpoint(peer) == "http://a"

    @pytest.mark.unit
    def test_list_of_dicts(self):
        peer = MagicMock(transport_config={"endpoints": [{"url": "http://x"}]})
        assert _select_endpoint(peer) == "http://x"

    @pytest.mark.unit
    def test_legacy_endpoint_url_fallback(self):
        peer = MagicMock(transport_config={"endpoint_url": "http://legacy"})
        assert _select_endpoint(peer) == "http://legacy"

    @pytest.mark.unit
    def test_accepted_endpoints_key(self):
        """Responder's pairing-time transport_config uses `accepted_endpoints`."""
        peer = MagicMock(transport_config={"accepted_endpoints": ["http://mom"]})
        assert _select_endpoint(peer) == "http://mom"

    @pytest.mark.unit
    def test_empty_returns_none(self):
        peer = MagicMock(transport_config={})
        assert _select_endpoint(peer) is None
