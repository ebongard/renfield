"""
Tests for F5b — federation rate limits.

Two registries, tested independently + in integration:

- `acquire_asker_token(peer_pubkey)` — asker-side outbound bucket
- `acquire_responder_token(asker_pubkey)` — responder-side inbound bucket

Integration tests drive through the full `MCPManager._execute_federation_streaming`
and `FederationQueryResponder.handle_initiate` paths to confirm the
limiters actually gate the wire-level flows.
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from services.federation_identity import (
    FederationIdentity,
    init_federation_identity,
    reset_federation_identity_for_tests,
)
from services.federation_rate_limits import (
    _asker_outbound,
    _responder_inbound,
    acquire_asker_token,
    acquire_responder_token,
    reset_for_tests,
)


# =============================================================================
# Unit — registries in isolation
# =============================================================================


class TestRegistries:
    def setup_method(self) -> None:
        reset_for_tests()

    def teardown_method(self) -> None:
        reset_for_tests()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_asker_registry_creates_one_limiter_per_peer(self, monkeypatch):
        """First acquire seeds a bucket; subsequent reuses it."""
        assert len(_asker_outbound) == 0
        ok = await acquire_asker_token("a" * 64)
        assert ok is True
        assert len(_asker_outbound) == 1
        # Second peer gets its own bucket.
        ok = await acquire_asker_token("b" * 64)
        assert ok is True
        assert len(_asker_outbound) == 2
        # Same peer reuses.
        ok = await acquire_asker_token("a" * 64)
        assert ok is True
        assert len(_asker_outbound) == 2

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_asker_registry_exhausts_then_refills(self, monkeypatch):
        """Spending all tokens returns False; time passing refills."""
        # Pin rate low so we can drain quickly.
        from services.federation_rate_limits import _asker_outbound as store
        from services.mcp_client import TokenBucketRateLimiter

        bucket = TokenBucketRateLimiter(rate_per_minute=3)  # 3 tokens initial
        store["x" * 64] = bucket

        assert await acquire_asker_token("x" * 64) is True
        assert await acquire_asker_token("x" * 64) is True
        assert await acquire_asker_token("x" * 64) is True
        # Exhausted.
        assert await acquire_asker_token("x" * 64) is False

        # Simulate time passing for refill. Manually advance the
        # bucket's internal clock — otherwise we'd need to sleep.
        bucket.last_update -= 30  # 30 seconds ago → 1.5 tokens refilled
        assert await acquire_asker_token("x" * 64) is True

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_responder_registry_is_independent_of_asker_registry(self):
        """Two separate namespaces — an asker-side hit on key K doesn't
        exhaust the responder-side bucket for the same K."""
        assert await acquire_asker_token("k" * 64) is True
        # Responder-side still has its full budget for the same key.
        assert await acquire_responder_token("k" * 64) is True
        assert len(_asker_outbound) == 1
        assert len(_responder_inbound) == 1

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_rate_limit_isolates_peers(self):
        """Exhausting peer A's bucket leaves peer B's untouched."""
        from services.federation_rate_limits import _asker_outbound as store
        from services.mcp_client import TokenBucketRateLimiter

        store["a" * 64] = TokenBucketRateLimiter(rate_per_minute=1)
        # Drain A
        assert await acquire_asker_token("a" * 64) is True
        assert await acquire_asker_token("a" * 64) is False
        # B is untouched.
        assert await acquire_asker_token("b" * 64) is True

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_concurrent_first_acquire_creates_one_bucket(self):
        """Belt-and-suspenders: many concurrent first-acquires for the
        same key must end up sharing ONE bucket (the double-checked
        lock pattern). If two coroutines both lost the lock race and
        each created their own bucket, one would be discarded and the
        rate limit would silently double for that key."""
        import asyncio
        results = await asyncio.gather(
            *[acquire_asker_token("z" * 64) for _ in range(20)]
        )
        # All should succeed (default 60/min budget, 20 < 60).
        assert all(results)
        # Critically: exactly one bucket created.
        assert len(_asker_outbound) == 1


# =============================================================================
# Integration — asker-side block surfaces as FinalResult failure
# =============================================================================


class TestAskerSideIntegration:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_execute_federation_streaming_rate_limited(
        self, tmp_path, monkeypatch,
    ):
        """When the asker-side token bucket is empty, the MCPManager
        federation branch must NOT call the asker — yields a single
        FinalResult failure with a user-friendly message."""
        from services.federation_rate_limits import _asker_outbound
        from services.mcp_client import (
            MCPManager,
            MCPServerConfig,
            MCPServerState,
            MCPToolInfo,
            MCPTransportType,
            TokenBucketRateLimiter,
        )

        reset_for_tests()
        reset_federation_identity_for_tests()
        init_federation_identity(tmp_path / "key")

        manager = MCPManager()
        config = MCPServerConfig(
            name="peer_1", transport=MCPTransportType.FEDERATION,
            streaming=True, peer_user_id=1,
        )
        state = MCPServerState(config=config)
        state.connected = True
        manager._servers["peer_1"] = state
        manager._tool_index["mcp.peer_1.query_brain"] = MCPToolInfo(
            server_name="peer_1", original_name="query_brain",
            namespaced_name="mcp.peer_1.query_brain", description="", input_schema={},
        )

        fake_peer = SimpleNamespace(
            id=1, remote_pubkey="z" * 64, remote_display_name="Mom",
            revoked_at=None,
        )
        session_mock = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none = lambda: fake_peer
        session_mock.execute = AsyncMock(return_value=result_mock)
        session_mock.__aenter__ = AsyncMock(return_value=session_mock)
        session_mock.__aexit__ = AsyncMock(return_value=False)
        monkeypatch.setattr(
            "services.database.AsyncSessionLocal", lambda: session_mock,
        )

        # Pre-exhaust the bucket for this peer.
        _asker_outbound["z" * 64] = TokenBucketRateLimiter(rate_per_minute=1)
        assert await acquire_asker_token("z" * 64) is True  # consume the 1 token
        assert await acquire_asker_token("z" * 64) is False

        # Asker must NOT be invoked — assert via spy.
        asker_called = False

        async def should_not_be_called(self, peer, text):
            nonlocal asker_called
            asker_called = True
            yield {"success": True, "message": "", "data": None}

        monkeypatch.setattr(
            "services.federation_query_asker.FederationQueryAsker.query_peer",
            should_not_be_called,
        )

        final = await manager.execute_tool(
            "mcp.peer_1.query_brain", {"query": "q"}, user_id=1,
        )
        assert asker_called is False
        assert final["success"] is False
        assert "rate limit" in final["message"].lower()

        reset_federation_identity_for_tests()
        reset_for_tests()


# =============================================================================
# Integration — responder-side block surfaces as FederationQueryError
# =============================================================================


class TestResponderSideIntegration:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_handle_initiate_rate_limited(self, tmp_path):
        """When the responder-side bucket is empty for this asker,
        handle_initiate raises FederationQueryError after sig+nonce
        checks pass."""
        import secrets

        from services.federation_query_responder import (
            FederationQueryError,
            FederationQueryResponder,
            _clear_state_for_tests,
        )
        from services.federation_query_schemas import QueryBrainInitiateRequest
        from services.federation_rate_limits import (
            _responder_inbound,
        )
        from services.mcp_client import TokenBucketRateLimiter
        from services.pairing_service import _canonical_bytes

        reset_for_tests()
        reset_federation_identity_for_tests()
        init_federation_identity(tmp_path / "responder_key")
        await _clear_state_for_tests()

        asker = FederationIdentity(ed25519.Ed25519PrivateKey.generate())
        asker_pubkey = asker.public_key_hex()

        # db mock that returns a valid peer for this asker
        db = MagicMock()
        peer = MagicMock()
        peer.id = 7
        peer.circle_owner_id = 1
        peer.remote_pubkey = asker_pubkey
        peer.remote_user_id = 42
        peer.revoked_at = None
        membership = MagicMock(value=2)

        async def execute_side(stmt):
            r = MagicMock()
            if "peer_users" in str(stmt):
                r.scalar_one_or_none = lambda: peer
            elif "circle_memberships" in str(stmt):
                r.scalar_one_or_none = lambda: membership
            else:
                r.scalar_one_or_none = lambda: None
            return r

        db.execute = AsyncMock(side_effect=execute_side)
        db.commit = AsyncMock()

        responder = FederationQueryResponder(db=db)
        responder._run_query = AsyncMock()

        def build_req(nonce: str) -> QueryBrainInitiateRequest:
            unsigned = {
                "version": 2,
                "asker_pubkey": asker_pubkey,
                "query": "q",
                "nonce": nonce,
                "timestamp": int(time.time()),
                "depth": 3,
                "path": [asker_pubkey],
            }
            sig = asker.sign(_canonical_bytes(unsigned)).hex()
            return QueryBrainInitiateRequest(**unsigned, signature=sig)

        # Pre-exhaust the responder bucket for this asker_pubkey.
        _responder_inbound[asker_pubkey] = TokenBucketRateLimiter(
            rate_per_minute=1,
        )
        # First call consumes the token and SUCCEEDS.
        resp = await responder.handle_initiate(build_req(secrets.token_hex(16)))
        assert resp.request_id

        # Second call exhausts → rejected with rate-limit error.
        with pytest.raises(FederationQueryError, match="[Rr]ate limit"):
            await responder.handle_initiate(build_req(secrets.token_hex(16)))

        reset_federation_identity_for_tests()
        await _clear_state_for_tests()
        reset_for_tests()
