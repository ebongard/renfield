"""The reachability ping -- one signed round trip between paired instances.

Both halves side by side, on purpose (the responder half and the asker half of
a protocol drift apart when they are tested in separate files):

  responder   `FederationQueryResponder.handle_ping` + `POST /peer/ping`
  asker       `federation_query_asker.ping_peer`

What is pinned:

  - a ping is answered only to a signature-verified, paired, non-revoked key,
    and an unpaired caller sees exactly what a refused query shows it;
  - with the feature off the route is indistinguishable from one that does not
    exist;
  - a ping is not a request: it moves no `last_seen_at`, writes no audit row
    and no pending record, and spends no query token;
  - the asker tells seven outcomes apart, and "the far side cannot answer
    pings" is never "the far side is down";
  - the answer is bound to the paired key AND to the nonce that was sent.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from httpx import AsyncClient
from sqlalchemy import func, select

import services.federation_query_asker as asker_module
import services.federation_query_responder as responder_module
from models.database import FederationQueryLog, PeerUser, Role, User
from services import federation_rate_limits
from services.federation_identity import (
    FederationIdentity,
    canonical_bytes,
    get_federation_identity,
    init_federation_identity,
    reset_federation_identity_for_tests,
)
from services.federation_instance_name import NAME_ATTACHMENT_PURPOSE, wire_body
from services.federation_pending_store import get_pending_store, reset_store_for_tests
from services.federation_query_asker import (
    PING_INVALID_RESPONSE,
    PING_NO_ENDPOINT,
    PING_OUTCOMES,
    PING_PIN_MISMATCH,
    PING_REACHABLE,
    PING_REFUSED,
    PING_UNREACHABLE,
    PING_UNSUPPORTED,
    ping_peer,
)
from services.federation_query_responder import (
    FederationQueryError,
    FederationQueryResponder,
    _clear_state_for_tests,
)
from services.federation_query_schemas import (
    PeerPingRequest,
    PeerPingResponse,
    ping_canonical_payload,
    pong_canonical_payload,
)
from utils.config import settings

PING = "/api/federation/peer/ping"
INITIATE = "/api/federation/peer/query_brain/initiate"

#: The keys of a ping and of its answer from an instance without a name.
PING_KEYS = {"version", "asker_pubkey", "nonce", "timestamp", "signature"}
PONG_KEYS = {
    "version", "status", "nonce", "answered_at", "responder_pubkey", "responder_signature",
}
ATTACHMENT_KEYS = {"instance_name", "instance_name_signature"}

UNIFORM_REFUSAL = {"detail": "Federation query failed"}

# =============================================================================
# Fixtures
# =============================================================================


def _fresh_identity() -> FederationIdentity:
    return FederationIdentity(ed25519.Ed25519PrivateKey.generate())


@pytest.fixture
async def local_identity(tmp_path, monkeypatch):
    """THIS instance's identity on clean state, without a name."""
    reset_federation_identity_for_tests()
    init_federation_identity(tmp_path / "local_key")
    reset_store_for_tests()
    await _clear_state_for_tests()
    federation_rate_limits.reset_for_tests()
    monkeypatch.setattr(settings, "federation_instance_name", "")
    monkeypatch.setattr(settings, "federation_advertised_url", "")
    yield get_federation_identity()
    reset_federation_identity_for_tests()
    await _clear_state_for_tests()
    reset_store_for_tests()
    federation_rate_limits.reset_for_tests()


@pytest.fixture
def far_identity() -> FederationIdentity:
    return _fresh_identity()


@pytest.fixture
def feature_on(monkeypatch):
    monkeypatch.setattr(settings, "federation_reachability_enabled", True)


@pytest.fixture
def feature_off(monkeypatch):
    monkeypatch.setattr(settings, "federation_reachability_enabled", False)


def _signed_ping(
    asker: FederationIdentity, *, nonce: str | None = None, timestamp: int | None = None,
    **extra,
) -> PeerPingRequest:
    unsigned = {
        "version": 1,
        "asker_pubkey": asker.public_key_hex(),
        "nonce": nonce or secrets.token_hex(16),
        "timestamp": timestamp if timestamp is not None else int(time.time()),
    }
    signature = asker.sign(canonical_bytes({"kind": "ping", **unsigned})).hex()
    return PeerPingRequest(**unsigned, signature=signature, **extra)


def _attachment(identity: FederationIdentity, name: str, envelope_signature: str) -> dict[str, str]:
    payload = {
        "purpose": NAME_ATTACHMENT_PURPOSE, "instance_name": name,
        "pubkey": identity.public_key_hex(), "envelope_signature": envelope_signature,
    }
    return {
        "instance_name": name,
        "instance_name_signature": identity.sign(canonical_bytes(payload)).hex(),
    }


LAST_SEEN = datetime(2026, 11, 1, 12, 0, 0)


def _paired_row(asker: FederationIdentity) -> PeerUser:
    return PeerUser(
        id=77, circle_owner_id=1, remote_pubkey=asker.public_key_hex(),
        remote_display_name="evdb", granted_tier=2, transport_config={},
        last_seen_at=LAST_SEEN, revoked_at=None,
    )


def _db_returning(peers: list) -> MagicMock:
    db = MagicMock()

    async def _execute(stmt):
        result = MagicMock()
        rows = peers if "peer_users" in str(stmt) else []
        result.scalars = lambda: MagicMock(all=lambda: rows)
        result.scalar_one_or_none = lambda: rows[0] if rows else None
        return result

    db.execute = AsyncMock(side_effect=_execute)
    db.commit = AsyncMock()
    return db


# =============================================================================
# Responder half
# =============================================================================


class TestResponderAnswersAPing:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_paired_key_gets_a_signed_acknowledgement(self, local_identity, far_identity):
        req = _signed_ping(far_identity)
        resp = await FederationQueryResponder(
            db=_db_returning([_paired_row(far_identity)])).handle_ping(req)
        assert resp.status == "ok"
        assert resp.nonce == req.nonce, "the echo is what binds the answer to THIS ping"
        assert resp.responder_pubkey == local_identity.public_key_hex()
        assert FederationIdentity.verify(
            local_identity.public_key_bytes(),
            bytes.fromhex(resp.responder_signature),
            canonical_bytes(pong_canonical_payload(resp)),
        )

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_the_answer_carries_no_content(self, local_identity, far_identity):
        """Exactly these keys from an instance without a name."""
        resp = await FederationQueryResponder(
            db=_db_returning([_paired_row(far_identity)])).handle_ping(_signed_ping(far_identity))
        assert set(wire_body(resp)) == PONG_KEYS

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_named_instance_attaches_its_name(self, local_identity, far_identity, monkeypatch):
        monkeypatch.setattr(settings, "federation_instance_name", "Instance A")
        resp = await FederationQueryResponder(
            db=_db_returning([_paired_row(far_identity)])).handle_ping(_signed_ping(far_identity))
        body = wire_body(resp)
        assert set(body) == PONG_KEYS | ATTACHMENT_KEYS
        assert body["instance_name"] == "Instance A"
        assert "instance_name" not in pong_canonical_payload(resp)

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_an_unpaired_key_is_refused(self, local_identity, far_identity):
        """A stranger signs perfectly well with its own key. The pairing is
        what it lacks."""
        with pytest.raises(FederationQueryError, match="Unknown or revoked"):
            await FederationQueryResponder(db=_db_returning([])).handle_ping(
                _signed_ping(far_identity))

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_tampered_signature_is_refused(self, local_identity, far_identity):
        req = _signed_ping(far_identity)
        bad = bytearray(bytes.fromhex(req.signature))
        bad[0] ^= 0xFF
        with pytest.raises(FederationQueryError, match="Signature"):
            await FederationQueryResponder(
                db=_db_returning([_paired_row(far_identity)])
            ).handle_ping(req.model_copy(update={"signature": bad.hex()}))

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_key_cannot_ping_in_another_keys_name(self, local_identity, far_identity):
        stranger = _fresh_identity()
        forged = _signed_ping(stranger).model_copy(
            update={"asker_pubkey": far_identity.public_key_hex()})
        with pytest.raises(FederationQueryError, match="Signature"):
            await FederationQueryResponder(
                db=_db_returning([_paired_row(far_identity)])).handle_ping(forged)

    @pytest.mark.asyncio
    @pytest.mark.unit
    @pytest.mark.parametrize("skew", [-120, 120])
    async def test_a_stale_or_future_ping_is_refused(self, local_identity, far_identity, skew):
        req = _signed_ping(far_identity, timestamp=int(time.time()) + skew)
        with pytest.raises(FederationQueryError, match="window"):
            await FederationQueryResponder(
                db=_db_returning([_paired_row(far_identity)])).handle_ping(req)

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_replayed_ping_is_refused(self, local_identity, far_identity):
        responder = FederationQueryResponder(db=_db_returning([_paired_row(far_identity)]))
        req = _signed_ping(far_identity)
        await responder.handle_ping(req)
        with pytest.raises(FederationQueryError, match="replay"):
            await responder.handle_ping(req)

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_ping_is_not_valid_as_any_other_envelope(self, local_identity, far_identity):
        """Domain separation: the signed bytes carry `kind: ping`. A signature
        over the same fields WITHOUT it -- what a look-alike structure would
        produce -- is not a ping."""
        unsigned = {
            "version": 1, "asker_pubkey": far_identity.public_key_hex(),
            "nonce": secrets.token_hex(16), "timestamp": int(time.time()),
        }
        req = PeerPingRequest(
            **unsigned, signature=far_identity.sign(canonical_bytes(unsigned)).hex())
        assert ping_canonical_payload(req)["kind"] == "ping"
        with pytest.raises(FederationQueryError, match="Signature"):
            await FederationQueryResponder(
                db=_db_returning([_paired_row(far_identity)])).handle_ping(req)


class TestAPingIsNotARequest:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_it_does_not_move_last_seen_at(self, local_identity, far_identity):
        """🛑 `last_seen_at` means "the far side last sent a real REQUEST". A
        wall display pinging every few minutes would turn it into "the far
        side's kiosk is switched on"."""
        peer = _paired_row(far_identity)
        db = _db_returning([peer])
        await FederationQueryResponder(db=db).handle_ping(_signed_ping(far_identity))
        assert peer.last_seen_at == LAST_SEEN
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_the_lookup_a_QUERY_runs_does_move_it(self, local_identity, far_identity):
        """Negative control for the test above: the same fixture and the same
        lookup, called the way a query calls it, and the column moves. Without
        this the test above could be green because the fixture never lets
        anything be written."""
        peer = _paired_row(far_identity)
        db = _db_returning([peer])
        await FederationQueryResponder(db=db)._lookup_peer(far_identity.public_key_hex())
        assert peer.last_seen_at != LAST_SEEN
        db.commit.assert_awaited()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_it_leaves_no_pending_record_and_writes_no_audit(
        self, local_identity, far_identity, monkeypatch,
    ):
        audit = AsyncMock()
        monkeypatch.setattr(responder_module, "_write_responder_audit", audit)
        await FederationQueryResponder(
            db=_db_returning([_paired_row(far_identity)])).handle_ping(_signed_ping(far_identity))
        assert await get_pending_store().list_for_pubkey(far_identity.public_key_hex()) == []
        audit.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_pings_have_a_budget_of_their_own(self, local_identity, far_identity, monkeypatch):
        """A pinging peer is limited, and the limit is not the query limit: a
        spent ping budget leaves the query budget whole, and the reverse."""
        monkeypatch.setattr(settings, "federation_ping_rate_per_minute", 2)
        federation_rate_limits.reset_for_tests()
        responder = FederationQueryResponder(db=_db_returning([_paired_row(far_identity)]))
        await responder.handle_ping(_signed_ping(far_identity))
        await responder.handle_ping(_signed_ping(far_identity))
        with pytest.raises(FederationQueryError, match="rate limit"):
            await responder.handle_ping(_signed_ping(far_identity))
        # Three pings, and the QUERY registry has not even been touched ...
        assert federation_rate_limits._responder_inbound == {}
        assert federation_rate_limits._asker_outbound == {}
        # ... so a query from the same peer still has its full budget.
        assert await federation_rate_limits.acquire_responder_token(far_identity.public_key_hex())

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_stranger_gets_no_bucket(self, local_identity, monkeypatch):
        """The bucket comes after the pairing lookup: a fresh key per request
        cannot grow the registry."""
        responder = FederationQueryResponder(db=_db_returning([]))
        for _ in range(3):
            with pytest.raises(FederationQueryError):
                await responder.handle_ping(_signed_ping(_fresh_identity()))
        assert federation_rate_limits._ping_inbound == {}


class TestAStrangerLeavesNothingBehind:
    """🛑 The route is open to the internet and a signature costs a stranger
    nothing: it signs with a key of its own. So everything that WRITES comes
    after the pairing lookup -- signature, timestamp, pairing, rate bucket,
    nonce. The nonce store is capped; written before the lookup, a stream of
    unpaired pings pushed the nonces of real requests out of it, and a
    recorded query could then be replayed inside its minute."""

    @pytest.fixture
    def nonce_writes(self, monkeypatch):
        spy = AsyncMock(side_effect=responder_module._record_nonce)
        monkeypatch.setattr(responder_module, "_record_nonce", spy)
        return spy

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_an_unpaired_ping_writes_no_nonce_and_no_state(
        self, local_identity, monkeypatch, nonce_writes,
    ):
        audit = AsyncMock()
        monkeypatch.setattr(responder_module, "_write_responder_audit", audit)
        responder = FederationQueryResponder(db=_db_returning([]))
        strangers = [_fresh_identity() for _ in range(5)]
        for stranger in strangers:
            with pytest.raises(FederationQueryError, match="Unknown or revoked"):
                await responder.handle_ping(_signed_ping(stranger))

        nonce_writes.assert_not_awaited()
        assert federation_rate_limits._ping_inbound == {}
        assert federation_rate_limits._responder_inbound == {}
        for stranger in strangers:
            assert await get_pending_store().list_for_pubkey(stranger.public_key_hex()) == []
        audit.assert_not_awaited()
        responder.db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_stranger_cannot_burn_a_nonce(self, local_identity, far_identity):
        """The same statement without looking inside: a nonce a stranger sent
        is still fresh for the paired peer."""
        nonce = secrets.token_hex(16)
        with pytest.raises(FederationQueryError, match="Unknown or revoked"):
            await FederationQueryResponder(db=_db_returning([])).handle_ping(
                _signed_ping(_fresh_identity(), nonce=nonce))
        resp = await FederationQueryResponder(
            db=_db_returning([_paired_row(far_identity)])).handle_ping(
                _signed_ping(far_identity, nonce=nonce))
        assert resp.status == "ok"

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_revoked_or_stale_or_forged_ping_writes_no_nonce_either(
        self, local_identity, far_identity, nonce_writes,
    ):
        paired = FederationQueryResponder(db=_db_returning([_paired_row(far_identity)]))
        with pytest.raises(FederationQueryError, match="window"):
            await paired.handle_ping(_signed_ping(far_identity, timestamp=int(time.time()) - 600))
        forged = _signed_ping(_fresh_identity()).model_copy(
            update={"asker_pubkey": far_identity.public_key_hex()})
        with pytest.raises(FederationQueryError, match="Signature"):
            await paired.handle_ping(forged)
        nonce_writes.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_paired_peer_over_its_budget_writes_no_nonce(
        self, local_identity, far_identity, monkeypatch, nonce_writes,
    ):
        """The bucket stands before the store as well: a paired peer gone
        wild fills its bucket, not the nonce store."""
        monkeypatch.setattr(settings, "federation_ping_rate_per_minute", 1)
        federation_rate_limits.reset_for_tests()
        responder = FederationQueryResponder(db=_db_returning([_paired_row(far_identity)]))
        await responder.handle_ping(_signed_ping(far_identity))
        for _ in range(4):
            with pytest.raises(FederationQueryError, match="rate limit"):
                await responder.handle_ping(_signed_ping(far_identity))
        assert nonce_writes.await_count == 1

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_the_paired_peers_own_replay_is_still_refused(
        self, local_identity, far_identity, nonce_writes,
    ):
        """Control for the spy: for a paired key the nonce IS written, and the
        second use of it is a replay."""
        responder = FederationQueryResponder(db=_db_returning([_paired_row(far_identity)]))
        req = _signed_ping(far_identity)
        await responder.handle_ping(req)
        with pytest.raises(FederationQueryError, match="replay"):
            await responder.handle_ping(req)
        assert nonce_writes.await_count == 2


class TestThePingTeachesTheName:
    @pytest.fixture
    def remembered(self, monkeypatch):
        spy = AsyncMock(return_value=True)
        monkeypatch.setattr(responder_module, "remember_remote_instance_name", spy)
        return spy

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_paired_asker_teaches_its_name(self, local_identity, far_identity, remembered):
        req = _signed_ping(far_identity)
        req = req.model_copy(update=_attachment(far_identity, "Instance B", req.signature))
        await FederationQueryResponder(
            db=_db_returning([_paired_row(far_identity)])).handle_ping(req)
        remembered.assert_awaited_once_with(77, "Instance B", current=None)

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_an_unpaired_asker_teaches_nothing(self, local_identity, far_identity, remembered):
        req = _signed_ping(far_identity)
        req = req.model_copy(update=_attachment(far_identity, "Mallory", req.signature))
        with pytest.raises(FederationQueryError):
            await FederationQueryResponder(db=_db_returning([])).handle_ping(req)
        remembered.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_bad_attachment_is_dropped_and_the_ping_answered(
        self, local_identity, far_identity, remembered,
    ):
        req = _signed_ping(far_identity)
        req = req.model_copy(update=_attachment(_fresh_identity(), "Mallory", req.signature))
        resp = await FederationQueryResponder(
            db=_db_returning([_paired_row(far_identity)])).handle_ping(req)
        assert resp.status == "ok"
        remembered.assert_not_awaited()


# =============================================================================
# The route, through HTTP on real Postgres
# =============================================================================


@pytest.fixture
async def paired(db_session, far_identity):
    """A real pairing row for `far_identity`, plus a revoked one for another key."""
    role = Role(name="role-fed-ping", permissions=["chat.own"], is_system=False)
    db_session.add(role)
    await db_session.flush()
    owner = User(username="fed-ping-owner", password_hash="x", role_id=role.id)
    db_session.add(owner)
    await db_session.flush()
    revoked_identity = _fresh_identity()
    active = PeerUser(
        circle_owner_id=owner.id, remote_pubkey=far_identity.public_key_hex(),
        remote_display_name="evdb", granted_tier=2, transport_config={},
        paired_at=LAST_SEEN - timedelta(days=3), last_seen_at=LAST_SEEN,
    )
    revoked = PeerUser(
        circle_owner_id=owner.id, remote_pubkey=revoked_identity.public_key_hex(),
        remote_display_name="gone", granted_tier=2, transport_config={},
        paired_at=LAST_SEEN - timedelta(days=9), last_seen_at=LAST_SEEN,
        revoked_at=LAST_SEEN + timedelta(days=1),
    )
    db_session.add_all([active, revoked])
    await db_session.commit()
    return {"active": active, "revoked": revoked, "revoked_identity": revoked_identity}


def _json(req: PeerPingRequest) -> dict:
    return wire_body(req)


@pytest.mark.database
class TestRouteFeatureOff:
    """Dark: the route answers like a route that was never built."""

    async def test_a_valid_ping_from_a_paired_peer_gets_the_frameworks_404(
        self, async_client: AsyncClient, local_identity, far_identity, paired, feature_off,
    ):
        resp = await async_client.post(PING, json=_json(_signed_ping(far_identity)))
        missing = await async_client.post(PING + "-there-is-no-such-route", json={})
        assert resp.status_code == missing.status_code == 404
        assert resp.json() == missing.json() == {"detail": "Not Found"}

    @pytest.mark.parametrize("body", [b"", b"not json", b"{}", b"[1, 2]", b"x" * 10_000])
    async def test_whatever_is_posted_the_answer_is_the_same(
        self, async_client: AsyncClient, local_identity, feature_off, body,
    ):
        """No 422 that would reveal a body schema behind the 404."""
        resp = await async_client.post(
            PING, content=body, headers={"content-type": "application/json"})
        assert resp.status_code == 404
        assert resp.json() == {"detail": "Not Found"}

    async def test_nothing_is_written(
        self, async_client: AsyncClient, db_session, local_identity, far_identity, paired,
        feature_off,
    ):
        await async_client.post(PING, json=_json(_signed_ping(far_identity)))
        await db_session.refresh(paired["active"])
        assert paired["active"].last_seen_at == LAST_SEEN


@pytest.mark.database
class TestRouteFeatureOn:
    async def test_a_paired_peer_gets_a_verifiable_answer(
        self, async_client: AsyncClient, local_identity, far_identity, paired, feature_on,
    ):
        req = _signed_ping(far_identity)
        resp = await async_client.post(PING, json=_json(req))
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == PONG_KEYS
        pong = PeerPingResponse.model_validate(body)
        assert pong.nonce == req.nonce
        assert pong.responder_pubkey == local_identity.public_key_hex()
        assert FederationIdentity.verify(
            local_identity.public_key_bytes(),
            bytes.fromhex(pong.responder_signature),
            canonical_bytes(pong_canonical_payload(pong)),
        )

    async def test_it_moves_no_last_seen_and_writes_no_audit_row(
        self, async_client: AsyncClient, db_session, local_identity, far_identity, paired,
        feature_on,
    ):
        """The two statements of the brief, against the real tables."""
        for _ in range(3):
            resp = await async_client.post(PING, json=_json(_signed_ping(far_identity)))
            assert resp.status_code == 200
        await db_session.refresh(paired["active"])
        assert paired["active"].last_seen_at == LAST_SEEN
        rows = (await db_session.execute(
            select(func.count()).select_from(FederationQueryLog))).scalar()
        assert rows == 0
        assert await get_pending_store().list_for_pubkey(far_identity.public_key_hex()) == []

    async def test_every_refusal_looks_like_a_refused_query(
        self, async_client: AsyncClient, local_identity, far_identity, paired, feature_on,
    ):
        """🛑 No oracle. An unpaired key, a revoked pairing, a broken signature,
        a stale timestamp, a replay and a body that is no ping at all get one
        and the same answer -- the one `/query_brain/initiate` gives a stranger."""
        stranger = _fresh_identity()
        tampered = _json(_signed_ping(far_identity))
        tampered["nonce"] = secrets.token_hex(16)
        replayed = _json(_signed_ping(far_identity))
        assert (await async_client.post(PING, json=replayed)).status_code == 200

        answers = {
            "unpaired": await async_client.post(PING, json=_json(_signed_ping(stranger))),
            "revoked": await async_client.post(
                PING, json=_json(_signed_ping(paired["revoked_identity"]))),
            "tampered": await async_client.post(PING, json=tampered),
            "stale": await async_client.post(PING, json=_json(
                _signed_ping(far_identity, timestamp=int(time.time()) - 600))),
            "replayed": await async_client.post(PING, json=replayed),
            "not_a_ping": await async_client.post(PING, json={"hello": "world"}),
            "not_json": await async_client.post(
                PING, content=b"\xff\xfe", headers={"content-type": "application/json"}),
            "oversized": await async_client.post(
                PING, content=b'{"x": "' + b"a" * 8000 + b'"}',
                headers={"content-type": "application/json"}),
        }
        for case, resp in answers.items():
            assert resp.status_code == 400, case
            assert resp.json() == UNIFORM_REFUSAL, case

        # ... and that IS what a stranger's query gets.
        pubkey = stranger.public_key_hex()
        unsigned = {
            "version": 2, "asker_pubkey": pubkey, "query": "frage",
            "nonce": secrets.token_hex(16), "timestamp": int(time.time()),
            "depth": 3, "path": [pubkey],
        }
        query = await async_client.post(INITIATE, json={
            **unsigned, "signature": stranger.sign(canonical_bytes(unsigned)).hex()})
        assert query.status_code == 400
        assert query.json() == UNIFORM_REFUSAL

    async def test_a_body_far_above_the_cap_gets_the_uniform_refusal(
        self, async_client: AsyncClient, local_identity, far_identity, paired, feature_on,
    ):
        """A megabyte with a declared length, a megabyte chunked without one,
        and a VALID ping padded past the cap: all the uniform 400."""
        async def _chunks():
            for _ in range(1024):
                yield b"a" * 1024

        padded = {**_json(_signed_ping(far_identity)), "padding": "a" * 8192}
        answers = {
            "declared": await async_client.post(
                PING, content=b"a" * (1024 * 1024),
                headers={"content-type": "application/json"}),
            "chunked": await async_client.post(
                PING, content=_chunks(), headers={"content-type": "application/json"}),
            "valid_but_padded": await async_client.post(PING, json=padded),
        }
        for case, resp in answers.items():
            assert resp.status_code == 400, case
            assert resp.json() == UNIFORM_REFUSAL, case

    async def test_a_revoked_pairing_is_not_answered(
        self, async_client: AsyncClient, local_identity, paired, feature_on,
    ):
        """Stated on its own: revocation ends the answers at once."""
        resp = await async_client.post(
            PING, json=_json(_signed_ping(paired["revoked_identity"])))
        assert resp.status_code == 400
        assert "responder_signature" not in resp.text


class _Upload:
    """What `_read_capped` reads off a request: the headers and the stream."""

    def __init__(self, chunks, content_length: str | None = None):
        self.headers = {} if content_length is None else {"content-length": content_length}
        self._chunks = chunks
        self.read = 0

    async def stream(self):
        for chunk in self._chunks:
            self.read += 1
            yield chunk


class TestTheRequestBodyIsCapped:
    """🛑 The ping route is reachable WITHOUT a pairing -- the signature can
    only be checked once the body is here. So the body is bounded before the
    first byte and at every chunk, not after `request.body()` buffered it."""

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_declared_length_above_the_cap_is_refused_UNREAD(self):
        from api.routes.federation_query import _PING_BODY_MAX_BYTES, _read_capped

        assert _PING_BODY_MAX_BYTES == 4096
        upload = _Upload([b"a" * 1024] * 1024, content_length=str(1024 * 1024))
        assert await _read_capped(upload, _PING_BODY_MAX_BYTES) is None
        assert upload.read == 0, "the body was read although it declared its size"

    @pytest.mark.asyncio
    @pytest.mark.unit
    @pytest.mark.parametrize("declared", ["-1", "abc", "1e3", "", " 12"])
    async def test_a_length_that_is_no_number_is_refused_unread(self, declared):
        from api.routes.federation_query import _read_capped

        upload = _Upload([b"{}"], content_length=declared)
        assert await _read_capped(upload, 4096) is None
        assert upload.read == 0

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_chunked_body_is_read_up_to_the_cap_and_no_further(self):
        """No declared length (chunked): the read stops at the first chunk
        that crosses the cap, with a megabyte still to come."""
        from api.routes.federation_query import _read_capped

        upload = _Upload([b"a" * 1024] * 1024)
        assert await _read_capped(upload, 4096) is None
        assert upload.read == 5, "4 KiB fit, the fifth chunk crosses the cap"

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_length_that_lies_is_capped_by_the_read(self):
        from api.routes.federation_query import _read_capped

        upload = _Upload([b"a" * 1024] * 1024, content_length="10")
        assert await _read_capped(upload, 4096) is None
        assert upload.read == 5

    @pytest.mark.asyncio
    @pytest.mark.unit
    @pytest.mark.parametrize("declared", [None, "10"])
    async def test_a_body_within_the_cap_comes_back_whole(self, declared):
        from api.routes.federation_query import _read_capped

        upload = _Upload([b"01234", b"56789"], content_length=declared)
        assert await _read_capped(upload, 4096) == b"0123456789"
        assert await _read_capped(_Upload([b"a" * 4096]), 4096) == b"a" * 4096


# =============================================================================
# Asker half
# =============================================================================


class _Peer:
    """What `ping_peer` reads off a pairing row."""

    def __init__(self, far: FederationIdentity, transport_config: dict | None = None):
        self.id = 3
        self.remote_pubkey = far.public_key_hex()
        self.remote_display_name = "evdb"
        self.remote_instance_name = None
        self.transport_config = (
            transport_config if transport_config is not None
            else {"endpoints": ["http://far.test:8000"]}
        )


class _Client(httpx.AsyncClient):
    """A REAL httpx client over a mock transport: every request goes to
    `handler(url, body) -> httpx.Response`.

    Real on purpose. The asker reads the answer as a stream and stops at a
    cap; a stand-in with a `post` method of its own would test the stand-in.
    """

    def __init__(self, handler):
        self.posted: list[tuple[str, dict]] = []

        async def _dispatch(request: httpx.Request) -> httpx.Response:
            url, body = str(request.url), json.loads(request.content or b"{}")
            self.posted.append((url, body))
            result = handler(url, body)
            if asyncio.iscoroutine(result):
                result = await result
            return result

        super().__init__(transport=httpx.MockTransport(_dispatch))


def _pong(far: FederationIdentity, body: dict, **overrides) -> dict:
    """A correct answer of the far side to the posted ping `body`."""
    resp = PeerPingResponse(
        status="ok", nonce=body["nonce"], answered_at=int(time.time()),
        responder_pubkey=far.public_key_hex(),
    )
    resp.responder_signature = far.sign(canonical_bytes(pong_canonical_payload(resp))).hex()
    return {**wire_body(resp), **overrides}


def _answering(far: FederationIdentity, **overrides) -> _Client:
    return _Client(lambda url, body: httpx.Response(200, json=_pong(far, body, **overrides)))


def _status(code: int) -> _Client:
    return _Client(lambda url, body: httpx.Response(code, json={"detail": "x"}))


@pytest.fixture
def asker_remembered(monkeypatch):
    spy = AsyncMock(return_value=True)
    monkeypatch.setattr(asker_module, "remember_remote_instance_name", spy)
    return spy


class TestAskerOutcomes:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_reachable(self, local_identity, far_identity, asker_remembered):
        client = _answering(far_identity)
        assert await ping_peer(_Peer(far_identity), client=client) == PING_REACHABLE
        url, body = client.posted[0]
        assert url == "http://far.test:8000/api/federation/peer/ping"
        assert set(body) == PING_KEYS, "no name, no extra keys"
        assert FederationIdentity.verify(
            local_identity.public_key_bytes(), bytes.fromhex(body["signature"]),
            canonical_bytes(ping_canonical_payload(PeerPingRequest.model_validate(body))),
        )

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_the_ping_carries_our_name_when_we_have_one(
        self, local_identity, far_identity, asker_remembered, monkeypatch,
    ):
        monkeypatch.setattr(settings, "federation_instance_name", "Instance A")
        client = _answering(far_identity)
        assert await ping_peer(_Peer(far_identity), client=client) == PING_REACHABLE
        body = client.posted[0][1]
        assert set(body) == PING_KEYS | ATTACHMENT_KEYS
        assert body["instance_name"] == "Instance A"

    @pytest.mark.asyncio
    @pytest.mark.unit
    @pytest.mark.parametrize("code", [404, 405, 501])
    async def test_a_far_side_without_the_route_is_UNSUPPORTED_never_unreachable(
        self, local_identity, far_identity, code,
    ):
        """🛑 An instance one release behind, or with the feature off, answers
        404 while its pairing works. That is "we do not know", not "down"."""
        assert await ping_peer(_Peer(far_identity), client=_status(code)) == PING_UNSUPPORTED

    @pytest.mark.asyncio
    @pytest.mark.unit
    @pytest.mark.parametrize("code", [400, 401, 403])
    async def test_refused(self, local_identity, far_identity, code):
        assert await ping_peer(_Peer(far_identity), client=_status(code)) == PING_REFUSED

    @pytest.mark.asyncio
    @pytest.mark.unit
    @pytest.mark.parametrize("code", [500, 502, 503, 504, 429, 302])
    async def test_any_other_status_is_unreachable(self, local_identity, far_identity, code):
        assert await ping_peer(_Peer(far_identity), client=_status(code)) == PING_UNREACHABLE

    @pytest.mark.asyncio
    @pytest.mark.unit
    @pytest.mark.parametrize("error", [
        httpx.ConnectError("refused"), httpx.ConnectTimeout("slow"),
        httpx.ReadTimeout("slow"), OSError("network down"),
    ])
    async def test_a_transport_failure_is_unreachable(self, local_identity, far_identity, error):
        def _raise(url, body):
            raise error

        assert await ping_peer(_Peer(far_identity), client=_Client(_raise)) == PING_UNREACHABLE

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_peer_that_hangs_is_cut_off(self, local_identity, far_identity, monkeypatch):
        """The hard ceiling over the whole exchange, not httpx's per-phase
        timeouts: a peer that accepts and then says nothing."""
        monkeypatch.setattr(asker_module, "PING_TOTAL_TIMEOUT_SECONDS", 0.05)

        async def _hang(url, body):
            await asyncio.sleep(30)

        started = time.monotonic()
        assert await ping_peer(_Peer(far_identity), client=_Client(_hang)) == PING_UNREACHABLE
        assert time.monotonic() - started < 2

    @pytest.mark.asyncio
    @pytest.mark.unit
    @pytest.mark.parametrize("config", [{}, {"endpoints": []}])
    async def test_no_endpoint(self, local_identity, far_identity, config):
        client = _answering(far_identity)
        assert await ping_peer(_Peer(far_identity, config), client=client) == PING_NO_ENDPOINT
        assert client.posted == []

    @pytest.mark.unit
    def test_the_outcome_set_is_closed(self):
        assert PING_OUTCOMES == {
            PING_REACHABLE, PING_UNREACHABLE, PING_PIN_MISMATCH, PING_REFUSED,
            PING_UNSUPPORTED, PING_INVALID_RESPONSE, PING_NO_ENDPOINT,
        }


class TestAskerChecksTheAnswer:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_an_answer_from_another_key_is_not_reachable(
        self, local_identity, far_identity, asker_remembered,
    ):
        """🛑 Pair-anchor binding: SOMETHING answered at that address with a
        perfectly valid signature of its own. It is not the instance we
        paired with (a regenerated identity, or somebody else)."""
        impostor = _fresh_identity()
        client = _answering(impostor)
        assert await ping_peer(_Peer(far_identity), client=client) == PING_INVALID_RESPONSE

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_recorded_answer_cannot_be_replayed(self, local_identity, far_identity):
        """The nonce: yesterday's genuine acknowledgement, served again by
        whoever sits on the path, does not match today's ping."""
        recorded = _pong(far_identity, {"nonce": secrets.token_hex(16)})
        client = _Client(lambda url, body: httpx.Response(200, json=recorded))
        assert await ping_peer(_Peer(far_identity), client=client) == PING_INVALID_RESPONSE

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_broken_signature_is_not_reachable(self, local_identity, far_identity):
        client = _answering(far_identity, responder_signature="00" * 64)
        assert await ping_peer(_Peer(far_identity), client=client) == PING_INVALID_RESPONSE

    @pytest.mark.asyncio
    @pytest.mark.unit
    @pytest.mark.parametrize("overrides", [
        {"responder_signature": "nothex"},
        {"responder_signature": ""},
        {"status": "busy"},
        {"answered_at": 0},          # signed field changed after signing
    ])
    async def test_an_altered_answer_is_not_reachable(self, local_identity, far_identity, overrides):
        client = _answering(far_identity, **overrides)
        assert await ping_peer(_Peer(far_identity), client=client) == PING_INVALID_RESPONSE

    @pytest.mark.asyncio
    @pytest.mark.unit
    @pytest.mark.parametrize("payload", [b"<html>captive portal</html>", b"[]", b"{}", b"null"])
    async def test_a_200_that_is_no_answer_is_not_reachable(
        self, local_identity, far_identity, payload,
    ):
        """A captive portal or a proxy's welcome page answers 200 too."""
        client = _Client(lambda url, body: httpx.Response(200, content=payload))
        assert await ping_peer(_Peer(far_identity), client=client) == PING_INVALID_RESPONSE

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_valid_answer_padded_past_the_cap_is_not_read(
        self, local_identity, far_identity, asker_remembered,
    ):
        """🛑 The far side decides what it sends back. The signature of this
        answer is GENUINE -- only its size is wrong -- so without the cap it
        reads as "reachable" after the whole body was buffered."""
        assert asker_module.PING_RESPONSE_MAX_BYTES == 4096

        def _padded(url, body):
            return httpx.Response(200, json={**_pong(far_identity, body), "padding": "a" * 8192})

        assert await ping_peer(
            _Peer(far_identity), client=_Client(_padded)) == PING_INVALID_RESPONSE
        asker_remembered.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_an_answer_without_a_declared_length_is_read_only_up_to_the_cap(
        self, local_identity, far_identity,
    ):
        """Chunked, and endless: the read stops just past the cap."""
        sent = {"chunks": 0}

        async def _endless():
            while sent["chunks"] < 10_000:
                sent["chunks"] += 1
                yield b"a" * 1024

        client = _Client(lambda url, body: httpx.Response(200, content=_endless()))
        assert await ping_peer(_Peer(far_identity), client=client) == PING_INVALID_RESPONSE
        assert sent["chunks"] <= 6, "the answer was read past the cap"

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_an_answer_that_declares_itself_too_large_is_not_read_at_all(
        self, local_identity, far_identity,
    ):
        sent = {"chunks": 0}

        async def _body():
            for _ in range(1024):
                sent["chunks"] += 1
                yield b"a" * 1024

        client = _Client(lambda url, body: httpx.Response(
            200, headers={"content-length": str(1024 * 1024)}, content=_body()))
        assert await ping_peer(_Peer(far_identity), client=client) == PING_INVALID_RESPONSE
        assert sent["chunks"] == 0

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_valid_answer_sent_in_chunks_is_still_reachable(
        self, local_identity, far_identity, asker_remembered,
    ):
        """Control: chunked as such is fine -- a small, genuine answer."""
        def _chunked(url, body):
            raw = json.dumps(_pong(far_identity, body)).encode()

            async def _parts():
                yield raw[:40]
                yield raw[40:]

            return httpx.Response(200, content=_parts())

        assert await ping_peer(_Peer(far_identity), client=_Client(_chunked)) == PING_REACHABLE

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_verified_answer_teaches_the_name(
        self, local_identity, far_identity, asker_remembered,
    ):
        def _handler(url, body):
            pong = _pong(far_identity, body)
            pong.update(_attachment(far_identity, "Instance B", pong["responder_signature"]))
            return httpx.Response(200, json=pong)

        assert await ping_peer(_Peer(far_identity), client=_Client(_handler)) == PING_REACHABLE
        asker_remembered.assert_awaited_once_with(3, "Instance B", current=None)

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_an_unverified_answer_teaches_nothing(
        self, local_identity, far_identity, asker_remembered,
    ):
        impostor = _fresh_identity()

        def _handler(url, body):
            pong = _pong(impostor, body)
            pong.update(_attachment(impostor, "Mallory", pong["responder_signature"]))
            return httpx.Response(200, json=pong)

        assert await ping_peer(
            _Peer(far_identity), client=_Client(_handler)) == PING_INVALID_RESPONSE
        asker_remembered.assert_not_awaited()


class TestAskerAndThePinnedCertificate:
    PINNED = {"endpoints": ["https://far.test:8443"], "tls_fingerprint": "aa" * 32}

    def _pin_check(self, monkeypatch, result):
        import services.federation_cert_pin as cert_pin

        check = AsyncMock(return_value=result)
        monkeypatch.setattr(cert_pin, "verify_peer_cert_fingerprint", check)
        return check

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_different_certificate_is_PIN_MISMATCH_and_nothing_is_sent(
        self, local_identity, far_identity, monkeypatch,
    ):
        """The far side is up and its certificate changed (a renewal). That
        needs a re-pairing, not patience -- and no signed request goes to a
        host whose certificate we do not recognise."""
        self._pin_check(monkeypatch, (False, "bb" * 32))
        client = _answering(far_identity)
        assert await ping_peer(
            _Peer(far_identity, self.PINNED), client=client) == PING_PIN_MISMATCH
        assert client.posted == []

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_no_certificate_at_all_is_unreachable(
        self, local_identity, far_identity, monkeypatch,
    ):
        self._pin_check(monkeypatch, (False, None))
        client = _answering(far_identity)
        assert await ping_peer(
            _Peer(far_identity, self.PINNED), client=client) == PING_UNREACHABLE
        assert client.posted == []

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_matching_pin_lets_the_ping_through(
        self, local_identity, far_identity, monkeypatch, asker_remembered,
    ):
        check = self._pin_check(monkeypatch, (True, "aa" * 32))
        client = _answering(far_identity)
        assert await ping_peer(
            _Peer(far_identity, self.PINNED), client=client) == PING_REACHABLE
        assert check.await_args.args[:2] == ("https://far.test:8443", "aa" * 32)
        assert check.await_args.kwargs["timeout"] <= 5


# =============================================================================
# Both halves against each other
# =============================================================================


class TestRoundTrip:
    """The asker's real request into the responder's real handler and back --
    the only place where a drift between the two halves (field list, `kind`,
    canonical bytes) shows."""

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_asker_and_responder_understand_each_other(self, tmp_path, monkeypatch):
        federation_rate_limits.reset_for_tests()
        reset_store_for_tests()
        await _clear_state_for_tests()
        monkeypatch.setattr(settings, "federation_instance_name", "")
        monkeypatch.setattr(settings, "federation_advertised_url", "")
        monkeypatch.setattr(asker_module, "remember_remote_instance_name", AsyncMock())
        monkeypatch.setattr(responder_module, "remember_remote_instance_name", AsyncMock())

        asker_id, responder_id = _fresh_identity(), _fresh_identity()
        monkeypatch.setattr(asker_module, "get_federation_identity", lambda: asker_id)

        responder = FederationQueryResponder.__new__(FederationQueryResponder)
        responder.db = _db_returning([_paired_row(asker_id)])
        responder.identity = responder_id

        async def _serve(url, body):
            try:
                pong = await responder.handle_ping(
                    PeerPingRequest.model_validate_json(json.dumps(body)))
            except FederationQueryError:
                return httpx.Response(400, json=UNIFORM_REFUSAL)
            return httpx.Response(200, json=wire_body(pong))

        assert await ping_peer(_Peer(responder_id), client=_Client(_serve)) == PING_REACHABLE

        # The same wire, but the responder does not know the asker: refused.
        responder.db = _db_returning([])
        assert await ping_peer(_Peer(responder_id), client=_Client(_serve)) == PING_REFUSED
        await _clear_state_for_tests()
