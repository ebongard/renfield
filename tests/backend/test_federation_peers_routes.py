"""
Tests for F4a — /api/federation/peers list + DELETE (revoke).

Coverage:
- list_peers returns only non-revoked peers owned by current user
- list_peers hydrates circle_tier from CircleMembership
- revoke_peer sets revoked_at + deletes CircleMembership + re-syncs
  MCP registry
- revoke_peer returns 404 for unknown id
- revoke_peer returns 404 when peer belongs to another user (no
  existence oracle)
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.routes.federation_pairing import (
    list_peers,
    revoke_peer,
    _tier_for_peer,
)


def _mock_request(manager=None):
    """FastAPI Request mock that exposes app.state.mcp_manager."""
    req = MagicMock()
    req.app.state.mcp_manager = manager
    return req


def _peer_row(*, id_=1, owner_id=42, pubkey="a" * 64, display="Mom",
              remote_user_id=99, revoked=None, paired_at=None, last_seen_at=None):
    row = MagicMock()
    row.id = id_
    row.circle_owner_id = owner_id
    row.remote_pubkey = pubkey
    row.remote_display_name = display
    row.remote_user_id = remote_user_id
    row.revoked_at = revoked
    row.paired_at = paired_at or datetime.now(UTC).replace(tzinfo=None)
    row.last_seen_at = last_seen_at
    return row


class TestListPeers:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_returns_empty_when_no_peers(self):
        db = MagicMock()
        r = MagicMock()
        r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        db.execute = AsyncMock(return_value=r)

        user = MagicMock(id=42)
        resp = await list_peers(db=db, current_user=user)
        assert resp.peers == []

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_hydrates_tier_from_membership(self):
        """circle_tier must be pulled from CircleMembership, not made up."""
        db = MagicMock()
        peers = [_peer_row(id_=1, owner_id=42, remote_user_id=99, display="Mom")]
        membership = MagicMock(value=2)  # household tier

        call_idx = [0]
        async def execute_side(stmt):
            call_idx[0] += 1
            result = MagicMock()
            if call_idx[0] == 1:
                # First call: SELECT PeerUser
                result.scalars = MagicMock(
                    return_value=MagicMock(all=MagicMock(return_value=peers))
                )
            else:
                # Subsequent calls: _tier_for_peer SELECT CircleMembership
                result.scalar_one_or_none = lambda: membership
            return result

        db.execute = AsyncMock(side_effect=execute_side)

        user = MagicMock(id=42)
        resp = await list_peers(db=db, current_user=user)
        assert len(resp.peers) == 1
        assert resp.peers[0].circle_tier == 2
        assert resp.peers[0].remote_display_name == "Mom"


class TestTierForPeer:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_returns_private_when_no_membership(self):
        """Fail-closed: missing membership row → tier 0 (self), not 4 (public).
        A UI showing 'public' for a data-integrity bug would mislead the
        owner into thinking they shared broadly."""
        db = MagicMock()
        r = MagicMock()
        r.scalar_one_or_none = lambda: None
        db.execute = AsyncMock(return_value=r)

        tier = await _tier_for_peer(db, owner_id=1, remote_user_id=99)
        assert tier == 0

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_returns_private_when_remote_user_id_missing(self):
        """Fail-closed for missing remote_user_id. Also: no DB query issued."""
        db = MagicMock()
        db.execute = AsyncMock()

        tier = await _tier_for_peer(db, owner_id=1, remote_user_id=None)
        assert tier == 0
        db.execute.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_returns_private_on_non_integer_value(self):
        """Malformed membership.value → 0, not 4."""
        db = MagicMock()
        membership = MagicMock(value="not-a-number")
        r = MagicMock()
        r.scalar_one_or_none = lambda: membership
        db.execute = AsyncMock(return_value=r)

        tier = await _tier_for_peer(db, owner_id=1, remote_user_id=99)
        assert tier == 0

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_parses_tier_int(self):
        db = MagicMock()
        membership = MagicMock(value=3)
        r = MagicMock()
        r.scalar_one_or_none = lambda: membership
        db.execute = AsyncMock(return_value=r)

        tier = await _tier_for_peer(db, owner_id=1, remote_user_id=99)
        assert tier == 3


class TestRevokePeer:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_sets_revoked_at_and_deletes_membership(self):
        peer = _peer_row(id_=1, owner_id=42, remote_user_id=99)
        db = MagicMock()
        r1 = MagicMock()
        r1.scalar_one_or_none = lambda: peer
        db.execute = AsyncMock(return_value=r1)
        db.commit = AsyncMock()

        user = MagicMock(id=42)
        req = _mock_request(manager=None)  # no manager → registry sync skipped

        await revoke_peer(peer_id=1, request=req, db=db, current_user=user)

        # revoked_at is now set
        assert peer.revoked_at is not None
        assert isinstance(peer.revoked_at, datetime)
        # DELETE membership was issued (2 execute calls: SELECT peer + DELETE membership)
        assert db.execute.await_count == 2
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_returns_404_for_unknown_peer(self):
        db = MagicMock()
        r = MagicMock()
        r.scalar_one_or_none = lambda: None
        db.execute = AsyncMock(return_value=r)

        user = MagicMock(id=42)
        req = _mock_request()

        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            await revoke_peer(peer_id=999, request=req, db=db, current_user=user)
        assert ei.value.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_returns_404_for_other_users_peer(self):
        """No existence oracle — a peer owned by user B must return 404
        for user A, same as a genuinely missing peer. The SELECT is
        scoped by circle_owner_id so the query returns None naturally."""
        db = MagicMock()
        r = MagicMock()
        r.scalar_one_or_none = lambda: None  # scoped query filters it out
        db.execute = AsyncMock(return_value=r)

        user = MagicMock(id=42)
        req = _mock_request()

        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            await revoke_peer(peer_id=1, request=req, db=db, current_user=user)
        assert ei.value.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_re_syncs_mcp_registry(self, monkeypatch):
        """After revoking, the MCP peer registry must be refreshed so
        `mcp.peer_<id>.query_brain` disappears from the agent loop."""
        peer = _peer_row(id_=1, owner_id=42, remote_user_id=99)
        db = MagicMock()
        r = MagicMock()
        r.scalar_one_or_none = lambda: peer
        db.execute = AsyncMock(return_value=r)
        db.commit = AsyncMock()

        sync_called = False

        async def fake_sync(manager, db_arg):
            nonlocal sync_called
            sync_called = True

        monkeypatch.setattr(
            "services.peer_mcp_registry.sync_peers", fake_sync,
        )

        manager = MagicMock()
        req = _mock_request(manager=manager)
        user = MagicMock(id=42)

        await revoke_peer(peer_id=1, request=req, db=db, current_user=user)
        assert sync_called

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_sync_failure_is_non_fatal(self, monkeypatch):
        """If the registry sync blows up (e.g., manager in a bad state),
        revoke still succeeds — the DB is authoritative; F3's per-request
        peer lookup catches the revocation regardless."""
        peer = _peer_row(id_=1, owner_id=42, remote_user_id=99)
        db = MagicMock()
        r = MagicMock()
        r.scalar_one_or_none = lambda: peer
        db.execute = AsyncMock(return_value=r)
        db.commit = AsyncMock()

        async def broken_sync(manager, db_arg):
            raise RuntimeError("registry on fire")

        monkeypatch.setattr(
            "services.peer_mcp_registry.sync_peers", broken_sync,
        )

        manager = MagicMock()
        req = _mock_request(manager=manager)
        user = MagicMock(id=42)

        # Must not raise — revoke is committed even when sync fails.
        await revoke_peer(peer_id=1, request=req, db=db, current_user=user)
        assert peer.revoked_at is not None

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_purges_in_flight_requests_for_revoked_peer(self):
        """BLOCKING #1 regression: revoked peer must not be able to poll
        /retrieve for requests they initiated before revocation. The
        revoke flow purges every pending entry bound to the peer's
        pubkey via the pending store."""
        from services.federation_pending_store import (
            _PendingRequest,
            get_pending_store,
            reset_store_for_tests,
        )
        from services.federation_query_responder import _clear_state_for_tests

        reset_store_for_tests()
        await _clear_state_for_tests()
        store = get_pending_store()

        peer_pubkey = "a" * 64
        peer = _peer_row(id_=1, owner_id=42, pubkey=peer_pubkey, remote_user_id=99)

        # Seed two pending requests from this peer + one from a different peer.
        await store.put(_PendingRequest(
            request_id="req-from-peer-1",
            asker_pubkey=peer_pubkey,
            peer_user_id=1, asker_local_user_id=99, max_visible_tier=2,
            query="q1", initiated_at=0,
        ))
        await store.put(_PendingRequest(
            request_id="req-from-peer-2",
            asker_pubkey=peer_pubkey,
            peer_user_id=1, asker_local_user_id=99, max_visible_tier=2,
            query="q2", initiated_at=0,
        ))
        await store.put(_PendingRequest(
            request_id="req-from-other",
            asker_pubkey="b" * 64,  # different peer
            peer_user_id=2, asker_local_user_id=100, max_visible_tier=2,
            query="q3", initiated_at=0,
        ))

        db = MagicMock()
        r = MagicMock()
        r.scalar_one_or_none = lambda: peer
        db.execute = AsyncMock(return_value=r)
        db.commit = AsyncMock()

        req = _mock_request()
        user = MagicMock(id=42)

        await revoke_peer(peer_id=1, request=req, db=db, current_user=user)

        # Peer's 2 pending requests gone; other peer's 1 request survives.
        assert await store.get("req-from-peer-1") is None
        assert await store.get("req-from-peer-2") is None
        assert await store.get("req-from-other") is not None

        await _clear_state_for_tests()
        reset_store_for_tests()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_invalidates_circle_resolver_cache(self, monkeypatch):
        """BLOCKING #2 regression: CircleResolver's class-level
        (owner, member) → tier cache must be invalidated on revoke so
        any in-flight handler drops the stale pre-revocation tier."""
        peer = _peer_row(id_=1, owner_id=42, remote_user_id=99)
        db = MagicMock()
        r = MagicMock()
        r.scalar_one_or_none = lambda: peer
        db.execute = AsyncMock(return_value=r)
        db.commit = AsyncMock()

        invalidated_with: list[tuple[int, int]] = []

        def fake_invalidate(cls, owner_id, member_user_id):
            invalidated_with.append((owner_id, member_user_id))

        from services.circle_resolver import CircleResolver
        monkeypatch.setattr(
            CircleResolver, "invalidate_for_membership",
            classmethod(fake_invalidate),
        )

        user = MagicMock(id=42)
        await revoke_peer(peer_id=1, request=_mock_request(), db=db, current_user=user)

        assert invalidated_with == [(42, 99)]
