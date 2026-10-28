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
    # Die gewaehrte Stufe steht seit F-ID-3 auf der Paarungszeile selbst.
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
        """🛑 `circle_tier` kommt aus `peer_users.granted_tier` — nicht aus einer
        CircleMembership (die auf der ENTFERNTEN Kennung keyte) und erst recht
        nicht erfunden."""
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
    """Die Stufe kommt seit F-ID-3 aus der PAARUNG, nicht aus einer Mitgliedschaft.

    🛑 Diese Klasse prueefte vier Fail-closed-Faelle, die es nicht mehr gibt:
    fehlende Mitgliedschaftszeile, fehlende `remote_user_id`, unlesbarer
    JSON-Wert. Alle drei entstanden daraus, dass die Stufe woanders lag als die
    Paarung und ueber eine FREMDE Nutzerkennung gesucht wurde. `granted_tier`
    ist `NOT NULL` auf derselben Zeile — es gibt keinen Fehlschlagfall mehr,
    auf den man antworten muesste. Das ist kein verlorener Riegel, sondern ein
    entfallener Fehlermodus.
    """

    @pytest.mark.unit
    def test_reads_the_tier_off_the_pairing_row(self):
        peer = MagicMock(granted_tier=3)
        assert _tier_for_peer(peer) == 3

    @pytest.mark.unit
    def test_takes_no_database_at_all(self):
        """🛑 Frueher eine Abfrage mit `scalar_one_or_none()` — die bei ZWEI
        Peers unter einem Eigentuemer (beide numerieren ab 1) geworfen haette
        (BL-0135). Ohne Abfrage gibt es die Mehrdeutigkeit nicht mehr."""
        import ast
        import inspect
        import textwrap

        from api.routes import federation_pairing

        # 🛑 Den RUMPF pruefen, nicht die Quelle als Text: der Docstring dieser
        # Funktion ERKLAERT, dass dort frueher eine `CircleMembership` gesucht
        # wurde — eine Zeichenkettenpruefung traefe also die Erklaerung statt
        # des Codes. Genau dieser Fehler ist mir am 2026-10-27 schon einmal
        # unterlaufen (beim Riegel-Test des Personen-Handschlags).
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
        # Negativkontrolle: der Rumpf ist nicht leer, die Pruefung misst also
        # ueberhaupt etwas.
        assert "granted_tier" in rumpf, rumpf

    @pytest.mark.unit
    def test_two_peers_under_one_owner_keep_their_own_tiers(self):
        """Das Abnahmekriterium aus BL-0135, auf Einheitsebene: zwei Peers,
        dieselbe `remote_user_id`, verschiedene Stufen — jeder seine eigene.
        Frueher haetten beide dieselbe Mitgliedschaftszeile getroffen."""
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
        # 🛑 Nur NOCH der SELECT auf die Peer-Zeile. Das zweite `execute` war ein
        # DELETE auf `circle_memberships` mit `member_user_id = remote_user_id` —
        # der Fehler selbst, und obendrein ohne `dimension`-Filter, also auch
        # `tenant`/`project`-Zeilen desselben Paares treffend.
        assert db.execute.await_count == 1
        # Die Reichweite faellt jetzt ueber die Zeile selbst: widerrufen UND
        # auf die engste Stufe zurueckgesetzt, damit ein spaeteres Entsperren
        # nicht still die alte Gewaehrung erbt.
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
        """🛑 Der Zwischenspeicher haelt `(circle_owner_id, member_user_id)`-Paare.

        Ein Peer hat seit F-ID-3 kein solches Paar mehr — seine Stufe steht auf
        der eigenen Zeile. Ihn trotzdem zu leeren waere harmlos, aber es waere
        ein Aufruf, der etwas behauptet, das nicht mehr stimmt: dass ein Peer
        eine Mitgliedschaft hat. Laufende Anfragen raeumt
        `purge_requests_for_pubkey` weg, das bleibt.
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

