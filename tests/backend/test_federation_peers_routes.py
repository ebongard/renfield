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

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.routes.federation_pairing import (
    _tier_for_peer,
    list_peers,
    revoke_peer,
)


def _mock_request(manager=None):
    """FastAPI Request mock that exposes app.state.mcp_manager."""
    req = MagicMock()
    req.app.state.mcp_manager = manager
    return req


def _peer_row(*, id_=1, owner_id=42, pubkey="a" * 64, display="Mom",
              remote_user_id=99, revoked=None, paired_at=None, last_seen_at=None,
              granted_tier=4):
    row = MagicMock()
    row.id = id_
    row.circle_owner_id = owner_id
    row.remote_pubkey = pubkey
    row.remote_display_name = display
    row.remote_user_id = remote_user_id
    # Since F-ID-3 the granted tier stands on the pairing row itself.
    row.granted_tier = granted_tier
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
    async def test_hydrates_tier_from_the_pairing_row(self):
        """🛑 `circle_tier` comes from `peer_users.granted_tier` -- not from a
        CircleMembership (which keyed on the REMOTE id) and certainly not
        invented."""
        db = MagicMock()
        peers = [_peer_row(id_=1, owner_id=42, remote_user_id=99, display="Mom", granted_tier=2)]
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
    """Since F-ID-3 the tier comes from the PAIRING, not from a membership.

    🛑 This class used to check four fail-closed cases that no longer exist: a
    missing membership row, a missing `remote_user_id`, an unreadable JSON
    value. All three arose from the tier living somewhere other than the
    pairing and being looked up via a FOREIGN user id. `granted_tier` is
    `NOT NULL` on the same row -- there is no failure case left to answer. That
    is not a lost guard but a failure mode that has gone away.
    """

    @pytest.mark.unit
    def test_reads_the_tier_off_the_pairing_row(self):
        peer = MagicMock(granted_tier=3)
        assert _tier_for_peer(peer) == 3

    @pytest.mark.unit
    def test_takes_no_database_at_all(self):
        """🛑 Formerly a query with `scalar_one_or_none()` -- which would have
        raised for TWO peers under one owner (both number from 1) (BL-0135).
        Without a query the ambiguity no longer exists."""
        import ast
        import inspect
        import textwrap

        from api.routes import federation_pairing

        # 🛑 Check the BODY, not the source as text: this function's
        # docstring EXPLAINS that a `CircleMembership` used to be looked up
        # there -- so a string check would hit the explanation instead of the
        # code. I made exactly this mistake once already on 2026-10-27 (in the
        # guard test for the person handshake).
        baum = ast.parse(textwrap.dedent(
            inspect.getsource(federation_pairing._tier_for_peer)))
        fn = baum.body[0]
        rumpf = ast.unparse(ast.Module(
            body=[k for k in fn.body if not (
                isinstance(k, ast.Expr) and isinstance(k.value, ast.Constant)
                and isinstance(k.value.value, str))],
            type_ignores=[]))
        assert "execute" not in rumpf, rumpf
        assert "CircleMembership" not in rumpf, rumpf
        assert "scalar_one_or_none" not in rumpf, rumpf
        assert not inspect.iscoroutinefunction(federation_pairing._tier_for_peer)
        # Negative control: the body is not empty, so the check measures
        # something at all.
        assert "granted_tier" in rumpf, rumpf

    @pytest.mark.unit
    def test_two_peers_under_one_owner_keep_their_own_tiers(self):
        """The acceptance criterion from BL-0135, at unit level: two peers,
        the same `remote_user_id`, different tiers -- each one its own.
        Formerly both would have hit the same membership row."""
        a = MagicMock(granted_tier=2, remote_user_id=1)
        b = MagicMock(granted_tier=4, remote_user_id=1)
        assert _tier_for_peer(a) == 2
        assert _tier_for_peer(b) == 4


class TestRevokePeer:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_sets_revoked_at_and_narrows_the_tier(self):
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
        # 🛑 ONLY the SELECT on the peer row is left. The second `execute`
        # was a DELETE on `circle_memberships` with
        # `member_user_id = remote_user_id` -- the defect itself, and on top of
        # that without a `dimension` filter, so it also hit `tenant`/`project`
        # rows of the same pair.
        assert db.execute.await_count == 1
        # The reach now falls over the row itself: revoked AND reset to the
        # narrowest tier, so a later unblocking does not silently inherit the
        # old grant.
        assert peer.granted_tier == 4
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
    async def test_does_NOT_touch_the_circle_resolver_cache(self, monkeypatch):
        """🛑 The cache holds `(circle_owner_id, member_user_id)` pairs.

        Since F-ID-3 a peer no longer has such a pair -- its tier stands on its
        own row. Clearing it regardless would be harmless, but it would be a
        call asserting something that is no longer true: that a peer has a
        membership. Requests in flight are cleaned up by
        `purge_requests_for_pubkey`, and that stays.
        """
        peer = _peer_row(id_=1, owner_id=42, remote_user_id=99)
        db = MagicMock()
        r = MagicMock()
        r.scalar_one_or_none = lambda: peer
        db.execute = AsyncMock(return_value=r)
        db.commit = AsyncMock()

        gerufen: list = []

        def fake_invalidate(cls, owner_id, member_id):
            gerufen.append((owner_id, member_id))

        from services.circle_resolver import CircleResolver
        monkeypatch.setattr(
            CircleResolver, "invalidate_for_membership",
            classmethod(fake_invalidate),
        )

        await revoke_peer(peer_id=1, request=_mock_request(), db=db,
                          current_user=MagicMock(id=42))

        assert gerufen == [], gerufen

