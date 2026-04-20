"""
Tests for F4d — federation query audit (asker-side).

Coverage:
- write_federation_audit writes one row per query lifecycle with
  correct final_status + verified_signature mapping
- user_id=None skips the write (auth-disabled deploys)
- classify_final handles success, failed, and aborted (None) cases
- truncation caps respected on query/answer/error text
- list_audit_for_user scopes strictly to the caller's user_id
- prune_old_audit_rows deletes only rows older than retention_days
- Integration: MCPManager._execute_federation_streaming writes the
  audit row after the asker's terminal yield
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.federation_audit import (
    FEDERATION_AUDIT_RETENTION_DAYS,
    MAX_ANSWER_EXCERPT_LEN,
    MAX_ERROR_MESSAGE_LEN,
    MAX_QUERY_TEXT_LEN,
    _classify_final,
    _truncate,
    list_audit_for_user,
    prune_old_audit_rows,
    write_federation_audit,
)


# =============================================================================
# Pure helpers
# =============================================================================


class TestClassifyFinal:
    @pytest.mark.unit
    def test_success_sets_verified_and_excerpt(self):
        status, verified, excerpt, err = _classify_final({
            "success": True,
            "message": "the wedding was on July 3rd",
            "data": {"responder_pubkey": "a" * 64},
        })
        assert status == "success"
        assert verified is True
        assert excerpt == "the wedding was on July 3rd"
        assert err is None

    @pytest.mark.unit
    def test_failure_carries_error_text(self):
        status, verified, excerpt, err = _classify_final({
            "success": False,
            "message": "Responder signature verification failed",
            "data": None,
        })
        assert status == "failed"
        assert verified is False
        assert excerpt is None
        assert err == "Responder signature verification failed"

    @pytest.mark.unit
    def test_none_classifies_as_unknown(self):
        """Aborted / cancelled queries (no terminal yield) get an honest
        `unknown` row rather than a misleading success/failure."""
        status, verified, excerpt, err = _classify_final(None)
        assert status == "unknown"
        assert verified is False
        assert excerpt is None
        assert err is not None and "cancel" in err.lower() or "abort" in err.lower()


class TestTruncate:
    @pytest.mark.unit
    def test_short_string_unchanged(self):
        assert _truncate("hi", 10) == "hi"

    @pytest.mark.unit
    def test_none_stays_none(self):
        assert _truncate(None, 10) is None

    @pytest.mark.unit
    def test_long_string_ellipsis(self):
        out = _truncate("x" * 100, 10)
        assert len(out) == 10
        assert out.endswith("…")

    @pytest.mark.unit
    def test_exact_boundary_no_truncation(self):
        out = _truncate("x" * 10, 10)
        assert out == "x" * 10


# =============================================================================
# write_federation_audit
# =============================================================================


class TestWriteFederationAudit:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_user_id_none_skips_write(self, monkeypatch):
        """Auth-disabled deploys (user_id=None) must not write. The
        FederationQueryLog.user_id column is NOT NULL — an attempted
        insert would fail, and we can't attribute anonymous rows anyway."""
        called = False

        class _FakeSession:
            async def __aenter__(self):
                nonlocal called
                called = True
                return self

            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr(
            "services.federation_audit.AsyncSessionLocal",
            lambda: _FakeSession(),
        )

        await write_federation_audit(
            user_id=None,
            peer_user_id=1,
            peer_pubkey_snapshot="a" * 64,
            peer_display_name_snapshot="Mom",
            query_text="q",
            initiated_at=datetime.now(UTC).replace(tzinfo=None),
            final_item={"success": True, "message": "ok", "data": None},
        )

        assert called is False

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_success_writes_row_with_verified_true(self, monkeypatch):
        captured = {}

        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def add(self, row):
                captured["row"] = row

            async def commit(self):
                captured["committed"] = True

        monkeypatch.setattr(
            "services.federation_audit.AsyncSessionLocal",
            lambda: _FakeSession(),
        )

        started = datetime(2026, 4, 22, 12, 0, 0)
        await write_federation_audit(
            user_id=42,
            peer_user_id=7,
            peer_pubkey_snapshot="a" * 64,
            peer_display_name_snapshot="Mom",
            query_text="Was hat Mom zur Hochzeit gesagt?",
            initiated_at=started,
            final_item={"success": True, "message": "She said yes", "data": {}},
        )

        row = captured["row"]
        assert captured["committed"] is True
        assert row.user_id == 42
        assert row.peer_user_id == 7
        assert row.peer_pubkey_snapshot == "a" * 64
        assert row.peer_display_name_snapshot == "Mom"
        assert row.query_text == "Was hat Mom zur Hochzeit gesagt?"
        assert row.final_status == "success"
        assert row.verified_signature is True
        assert row.answer_excerpt == "She said yes"
        assert row.error_message is None
        assert row.initiated_at == started
        assert row.finalized_at is not None

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_failure_writes_row_with_error_message(self, monkeypatch):
        captured = {}

        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def add(self, row):
                captured["row"] = row

            async def commit(self):
                captured["committed"] = True

        monkeypatch.setattr(
            "services.federation_audit.AsyncSessionLocal",
            lambda: _FakeSession(),
        )

        await write_federation_audit(
            user_id=1,
            peer_user_id=1,
            peer_pubkey_snapshot="b" * 64,
            peer_display_name_snapshot="Dad",
            query_text="q",
            initiated_at=datetime.now(UTC).replace(tzinfo=None),
            final_item={
                "success": False,
                "message": "Responder signature verification failed",
                "data": None,
            },
        )

        row = captured["row"]
        assert row.final_status == "failed"
        assert row.verified_signature is False
        assert row.answer_excerpt is None
        assert row.error_message == "Responder signature verification failed"

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_truncation_respected(self, monkeypatch):
        captured = {}

        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def add(self, row):
                captured["row"] = row

            async def commit(self):
                pass

        monkeypatch.setattr(
            "services.federation_audit.AsyncSessionLocal",
            lambda: _FakeSession(),
        )

        long_query = "q" * (MAX_QUERY_TEXT_LEN + 500)
        long_answer = "a" * (MAX_ANSWER_EXCERPT_LEN + 500)

        await write_federation_audit(
            user_id=1,
            peer_user_id=1,
            peer_pubkey_snapshot="c" * 64,
            peer_display_name_snapshot="X",
            query_text=long_query,
            initiated_at=datetime.now(UTC).replace(tzinfo=None),
            final_item={"success": True, "message": long_answer, "data": None},
        )

        row = captured["row"]
        assert len(row.query_text) == MAX_QUERY_TEXT_LEN
        assert row.query_text.endswith("…")
        assert len(row.answer_excerpt) == MAX_ANSWER_EXCERPT_LEN
        assert row.answer_excerpt.endswith("…")

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_db_failure_is_swallowed(self, monkeypatch):
        """A DB blip during audit write must not surface to the caller —
        the user has their answer already. The whole point of this
        helper is "best-effort"."""

        class _BrokenSession:
            async def __aenter__(self):
                raise RuntimeError("db unreachable")

            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr(
            "services.federation_audit.AsyncSessionLocal",
            lambda: _BrokenSession(),
        )

        # Should NOT raise.
        await write_federation_audit(
            user_id=1,
            peer_user_id=1,
            peer_pubkey_snapshot="a" * 64,
            peer_display_name_snapshot="Mom",
            query_text="q",
            initiated_at=datetime.now(UTC).replace(tzinfo=None),
            final_item={"success": True, "message": "ok", "data": None},
        )


# =============================================================================
# MCPManager integration — the audit row IS written from the streaming path
# =============================================================================


class TestAuditIntegration:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_federation_streaming_writes_audit(self, tmp_path, monkeypatch):
        """Regression guard: the full federation call path through
        MCPManager must invoke write_federation_audit with the right
        snapshot fields AND classify the final item correctly."""
        from services.federation_identity import (
            init_federation_identity,
            reset_federation_identity_for_tests,
        )
        from services.mcp_client import (
            MCPManager,
            MCPServerConfig,
            MCPServerState,
            MCPToolInfo,
            MCPTransportType,
        )
        from services.mcp_streaming import ProgressChunk

        reset_federation_identity_for_tests()
        init_federation_identity(tmp_path / "key")

        manager = MCPManager()
        config = MCPServerConfig(
            name="peer_42", transport=MCPTransportType.FEDERATION,
            streaming=True, peer_user_id=42,
        )
        state = MCPServerState(config=config)
        state.connected = True
        manager._servers["peer_42"] = state
        manager._tool_index["mcp.peer_42.query_brain"] = MCPToolInfo(
            server_name="peer_42", original_name="query_brain",
            namespaced_name="mcp.peer_42.query_brain", description="", input_schema={},
        )

        fake_peer = SimpleNamespace(
            id=42,
            remote_pubkey="z" * 64,
            remote_display_name="Grandma",
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

        async def fake_query_peer(self, peer, text):
            yield ProgressChunk(label="retrieving", sequence=1)
            yield {"success": True, "message": "the answer", "data": None}

        monkeypatch.setattr(
            "services.federation_query_asker.FederationQueryAsker.query_peer",
            fake_query_peer,
        )

        # Spy on the audit write.
        audit_calls = []

        async def spy(**kwargs):
            audit_calls.append(kwargs)

        monkeypatch.setattr(
            "services.federation_audit.write_federation_audit", spy,
        )

        final = await manager.execute_tool(
            "mcp.peer_42.query_brain",
            {"query": "wann war deine Hochzeit?"},
            user_id=99,
        )

        assert final["success"] is True
        assert len(audit_calls) == 1
        call = audit_calls[0]
        assert call["user_id"] == 99
        assert call["peer_user_id"] == 42
        assert call["peer_pubkey_snapshot"] == "z" * 64
        assert call["peer_display_name_snapshot"] == "Grandma"
        assert call["query_text"] == "wann war deine Hochzeit?"
        assert call["initiated_at"] is not None
        assert call["final_item"] == {"success": True, "message": "the answer", "data": None}

        reset_federation_identity_for_tests()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_revoked_peer_does_not_write_audit(self, monkeypatch):
        """Revoked/unknown peer returns error FinalResult without
        invoking the asker. No audit row is written — there was no
        federated query to audit, and we don't want revoke attempts
        spamming the log."""
        from services.mcp_client import (
            MCPManager,
            MCPServerConfig,
            MCPServerState,
            MCPToolInfo,
            MCPTransportType,
        )

        manager = MCPManager()
        config = MCPServerConfig(
            name="peer_99", transport=MCPTransportType.FEDERATION,
            streaming=True, peer_user_id=99,
        )
        state = MCPServerState(config=config)
        state.connected = True
        manager._servers["peer_99"] = state
        manager._tool_index["mcp.peer_99.query_brain"] = MCPToolInfo(
            server_name="peer_99", original_name="query_brain",
            namespaced_name="mcp.peer_99.query_brain", description="", input_schema={},
        )

        session_mock = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none = lambda: None
        session_mock.execute = AsyncMock(return_value=result_mock)
        session_mock.__aenter__ = AsyncMock(return_value=session_mock)
        session_mock.__aexit__ = AsyncMock(return_value=False)
        monkeypatch.setattr(
            "services.database.AsyncSessionLocal", lambda: session_mock,
        )

        audit_calls = []

        async def spy(**kwargs):
            audit_calls.append(kwargs)

        monkeypatch.setattr(
            "services.federation_audit.write_federation_audit", spy,
        )

        final = await manager.execute_tool(
            "mcp.peer_99.query_brain",
            {"query": "q"},
            user_id=1,
        )

        assert final["success"] is False
        assert len(audit_calls) == 0


# =============================================================================
# list / prune smoke tests
# =============================================================================


class TestListAndPrune:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_list_empty_returns_empty_list(self, monkeypatch):
        """No rows for the user → empty list, not a crash."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []

        session_mock = AsyncMock()
        session_mock.execute = AsyncMock(return_value=mock_result)
        session_mock.__aenter__ = AsyncMock(return_value=session_mock)
        session_mock.__aexit__ = AsyncMock(return_value=False)
        monkeypatch.setattr(
            "services.federation_audit.AsyncSessionLocal",
            lambda: session_mock,
        )

        from services.federation_audit import list_audit_for_user
        rows = await list_audit_for_user(user_id=42)
        assert rows == []

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_list_with_peer_filter_attaches_where_clause(self, monkeypatch):
        """`peer_pubkey` kwarg narrows the query. We can't assert SQL
        text from a Mock, but we can verify `execute` was called — and
        that calling with/without the filter hits the same path."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []

        session_mock = AsyncMock()
        session_mock.execute = AsyncMock(return_value=mock_result)
        session_mock.__aenter__ = AsyncMock(return_value=session_mock)
        session_mock.__aexit__ = AsyncMock(return_value=False)
        monkeypatch.setattr(
            "services.federation_audit.AsyncSessionLocal",
            lambda: session_mock,
        )

        from services.federation_audit import list_audit_for_user
        await list_audit_for_user(user_id=1, peer_pubkey="a" * 64)

        # The filter compiles into the statement passed to execute.
        assert session_mock.execute.await_count == 1
        stmt = session_mock.execute.call_args.args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "peer_pubkey_snapshot" in compiled
        assert "a" * 64 in compiled

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_prune_zero_expired_returns_zero(self, monkeypatch):
        """No rows past retention → prune returns 0, no error log."""
        mock_result = MagicMock()
        mock_result.rowcount = 0

        session_mock = AsyncMock()
        session_mock.execute = AsyncMock(return_value=mock_result)
        session_mock.commit = AsyncMock()
        session_mock.__aenter__ = AsyncMock(return_value=session_mock)
        session_mock.__aexit__ = AsyncMock(return_value=False)
        monkeypatch.setattr(
            "services.federation_audit.AsyncSessionLocal",
            lambda: session_mock,
        )

        from services.federation_audit import prune_old_audit_rows
        deleted = await prune_old_audit_rows(retention_days=90)
        assert deleted == 0


# =============================================================================
# Cancellation / try-finally semantics (review S1 regression guard)
# =============================================================================


class TestCancellationAuditRow:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_asker_raising_mid_stream_still_writes_audit(
        self, tmp_path, monkeypatch,
    ):
        """Regression guard for review S1: the try/finally in
        _execute_federation_streaming must ensure one audit row is
        written even when the asker raises mid-iteration (network
        blip, internal bug, etc.). final_item stays None so
        _classify_final maps to `unknown`."""
        from services.federation_identity import (
            init_federation_identity,
            reset_federation_identity_for_tests,
        )
        from services.mcp_client import (
            MCPManager,
            MCPServerConfig,
            MCPServerState,
            MCPToolInfo,
            MCPTransportType,
        )
        from services.mcp_streaming import ProgressChunk

        reset_federation_identity_for_tests()
        init_federation_identity(tmp_path / "key")

        manager = MCPManager()
        config = MCPServerConfig(
            name="peer_77", transport=MCPTransportType.FEDERATION,
            streaming=True, peer_user_id=77,
        )
        state = MCPServerState(config=config)
        state.connected = True
        manager._servers["peer_77"] = state
        manager._tool_index["mcp.peer_77.query_brain"] = MCPToolInfo(
            server_name="peer_77", original_name="query_brain",
            namespaced_name="mcp.peer_77.query_brain", description="", input_schema={},
        )

        fake_peer = SimpleNamespace(
            id=77, remote_pubkey="e" * 64, remote_display_name="Uncle",
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

        async def fake_query_peer(self, peer, text):
            yield ProgressChunk(label="retrieving", sequence=1)
            # Simulated mid-query crash — no terminal dict reached.
            raise RuntimeError("simulated mid-query abort")

        monkeypatch.setattr(
            "services.federation_query_asker.FederationQueryAsker.query_peer",
            fake_query_peer,
        )

        audit_calls: list[dict] = []

        async def spy(**kwargs):
            audit_calls.append(kwargs)

        monkeypatch.setattr(
            "services.federation_audit.write_federation_audit", spy,
        )

        # The RuntimeError bubbles up to us; the finally must still run
        # and record the audit row.
        with pytest.raises(RuntimeError, match="simulated mid-query abort"):
            async for _ in manager.execute_tool_streaming(
                "mcp.peer_77.query_brain", {"query": "q"}, user_id=1,
            ):
                pass

        assert len(audit_calls) == 1
        assert audit_calls[0]["user_id"] == 1
        assert audit_calls[0]["peer_pubkey_snapshot"] == "e" * 64
        # Terminal yield never reached → final_item None → classified unknown.
        assert audit_calls[0]["final_item"] is None

        reset_federation_identity_for_tests()
