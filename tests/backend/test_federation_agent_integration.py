"""
Tests for F3c — federation ↔ agent-loop integration.

Coverage:
- MCPManager.execute_tool_streaming routes FEDERATION-transport tools
  to FederationQueryAsker (F3c.1)
- Revoked/unknown peer surfaces as a FinalResult error, not an exception
- Missing query argument yields a clear error FinalResult
- PeerMCPRegistry.sync_peers upserts + removes stale entries (F3c.2)
- Ollama synthesis happy path + fallback to snippet concatenation on
  LLM failure (F3c.3)
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from services.federation_identity import (
    FederationIdentity,
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
from services.peer_mcp_registry import (
    FEDERATION_SERVER_PREFIX,
    QUERY_BRAIN_TOOL_NAME,
    _namespaced_query_brain,
    _server_name_for,
    sync_peers,
)


# =============================================================================
# F3c.1 — federation routing in MCPManager
# =============================================================================


class TestFederationRouting:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_federation_transport_routes_to_query_asker(self, tmp_path, monkeypatch):
        """An MCP tool call on a FEDERATION-transport server must go
        through FederationQueryAsker.query_peer instead of
        session.call_tool."""
        reset_federation_identity_for_tests()
        init_federation_identity(tmp_path / "key")

        manager = MCPManager()
        config = MCPServerConfig(
            name="peer_7",
            transport=MCPTransportType.FEDERATION,
            streaming=True,
            peer_user_id=7,
        )
        state = MCPServerState(config=config)
        state.connected = True
        manager._servers["peer_7"] = state
        tool = MCPToolInfo(
            server_name="peer_7",
            original_name="query_brain",
            namespaced_name="mcp.peer_7.query_brain",
            description="",
            input_schema={},
        )
        manager._tool_index["mcp.peer_7.query_brain"] = tool

        # Stub the PeerUser lookup + FederationQueryAsker at import time.
        fake_peer = SimpleNamespace(id=7, remote_display_name="Mom", revoked_at=None)
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
            yield {"success": True, "message": f"answered {peer.remote_display_name}", "data": None}

        monkeypatch.setattr(
            "services.federation_query_asker.FederationQueryAsker.query_peer",
            fake_query_peer,
        )

        items = []
        async for item in manager.execute_tool_streaming(
            "mcp.peer_7.query_brain", {"query": "what's for dinner?"},
        ):
            items.append(item)

        progress = [i for i in items if isinstance(i, ProgressChunk)]
        finals = [i for i in items if not isinstance(i, ProgressChunk)]
        assert len(progress) == 1
        assert finals[0]["success"] is True
        assert "Mom" in finals[0]["message"]

        reset_federation_identity_for_tests()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_revoked_peer_returns_error_final_result(self, monkeypatch):
        """If the PeerUser lookup returns None (revoked or deleted), the
        federation branch yields a FinalResult error without invoking
        FederationQueryAsker."""
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
        result_mock.scalar_one_or_none = lambda: None  # peer revoked/gone
        session_mock.execute = AsyncMock(return_value=result_mock)
        session_mock.__aenter__ = AsyncMock(return_value=session_mock)
        session_mock.__aexit__ = AsyncMock(return_value=False)

        monkeypatch.setattr(
            "services.database.AsyncSessionLocal", lambda: session_mock,
        )

        asker_called = False

        async def should_not_be_called(self, peer, text):
            nonlocal asker_called
            asker_called = True
            yield {"success": True, "message": "", "data": None}

        monkeypatch.setattr(
            "services.federation_query_asker.FederationQueryAsker.query_peer",
            should_not_be_called,
        )

        items = [i async for i in manager.execute_tool_streaming(
            "mcp.peer_99.query_brain", {"query": "q"},
        )]

        assert not asker_called
        assert items[-1]["success"] is False
        assert "revoked" in items[-1]["message"].lower() or "unknown" in items[-1]["message"].lower()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_missing_query_arg_returns_error(self):
        """query_brain requires a 'query' argument. Missing = clear error."""
        manager = MCPManager()
        config = MCPServerConfig(
            name="peer_1", transport=MCPTransportType.FEDERATION,
            streaming=True, peer_user_id=1,
        )
        manager._servers["peer_1"] = MCPServerState(config=config)
        manager._servers["peer_1"].connected = True
        manager._tool_index["mcp.peer_1.query_brain"] = MCPToolInfo(
            server_name="peer_1", original_name="query_brain",
            namespaced_name="mcp.peer_1.query_brain", description="", input_schema={},
        )

        items = [i async for i in manager.execute_tool_streaming(
            "mcp.peer_1.query_brain", {},  # no 'query' arg
        )]

        assert items[-1]["success"] is False
        assert "query" in items[-1]["message"].lower()


# =============================================================================
# F3c.2 — PeerMCPRegistry.sync_peers
# =============================================================================


class TestSyncPeers:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_registers_non_revoked_peers(self):
        manager = MCPManager()
        db = AsyncMock()

        peers = [
            SimpleNamespace(id=1, remote_pubkey="a"*64, remote_display_name="Mom"),
            SimpleNamespace(id=2, remote_pubkey="b"*64, remote_display_name="Dad"),
        ]
        result = MagicMock()
        result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=peers)))
        db.execute = AsyncMock(return_value=result)

        await sync_peers(manager, db)

        assert _server_name_for(1) in manager._servers
        assert _server_name_for(2) in manager._servers
        assert _namespaced_query_brain(1) in manager._tool_index
        assert _namespaced_query_brain(2) in manager._tool_index

        # Config has the FEDERATION transport + peer_user_id wired.
        peer1_config = manager._servers[_server_name_for(1)].config
        assert peer1_config.transport == MCPTransportType.FEDERATION
        assert peer1_config.streaming is True
        assert peer1_config.peer_user_id == 1

        # Tool description personalises with display_name.
        tool1 = manager._tool_index[_namespaced_query_brain(1)]
        assert "Mom" in tool1.description
        # Tool schema requires a `query` parameter.
        assert "query" in tool1.input_schema["properties"]
        assert "query" in tool1.input_schema["required"]

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_removes_stale_entries_on_resync(self):
        """If a peer is revoked between syncs, its registry entry is dropped."""
        manager = MCPManager()

        # First sync: two peers.
        peers_initial = [
            SimpleNamespace(id=1, remote_pubkey="a"*64, remote_display_name="Mom"),
            SimpleNamespace(id=2, remote_pubkey="b"*64, remote_display_name="Dad"),
        ]
        db_a = AsyncMock()
        r_a = MagicMock()
        r_a.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=peers_initial)))
        db_a.execute = AsyncMock(return_value=r_a)
        await sync_peers(manager, db_a)
        assert _server_name_for(2) in manager._servers

        # Second sync: peer 2 revoked (not returned by the query).
        peers_after = [peers_initial[0]]
        db_b = AsyncMock()
        r_b = MagicMock()
        r_b.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=peers_after)))
        db_b.execute = AsyncMock(return_value=r_b)
        await sync_peers(manager, db_b)

        assert _server_name_for(1) in manager._servers
        assert _server_name_for(2) not in manager._servers
        assert _namespaced_query_brain(2) not in manager._tool_index

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_does_not_touch_non_federation_servers(self):
        """Non-federation servers (stdio n8n, streamable_http paperless,
        etc.) must be unaffected by sync_peers — only entries with the
        `peer_` prefix are managed."""
        manager = MCPManager()
        manager._servers["n8n"] = MCPServerState(
            config=MCPServerConfig(name="n8n", transport=MCPTransportType.STDIO),
        )

        db = AsyncMock()
        result = MagicMock()
        result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        db.execute = AsyncMock(return_value=result)

        await sync_peers(manager, db)

        assert "n8n" in manager._servers  # untouched


# =============================================================================
# F3c.3 — Ollama synthesis
# =============================================================================


class TestOllamaSynthesis:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_happy_path_uses_llm_output(self, tmp_path, monkeypatch):
        """Synthesis calls the Ollama chat endpoint and returns the
        model's answer."""
        reset_federation_identity_for_tests()
        init_federation_identity(tmp_path / "key")

        from services.federation_query_responder import FederationQueryResponder

        responder = FederationQueryResponder(db=MagicMock())

        # Mock the llm client.
        fake_response = MagicMock()
        fake_response.message = MagicMock(content="Pasta with tomato sauce.")
        mock_client = MagicMock()
        mock_client.chat = AsyncMock(return_value=fake_response)

        monkeypatch.setattr(
            "utils.llm_client.get_default_client", lambda: mock_client,
        )

        matches = [
            SimpleNamespace(snippet="Mom: 'the sauce is tomato + garlic'", atom=MagicMock(atom_id="a"*36, atom_type="conversation_memory"), score=0.9),
        ]

        answer = await responder._synthesize("What's Mom's sauce?", matches)
        assert "Pasta" in answer
        mock_client.chat.assert_awaited_once()

        reset_federation_identity_for_tests()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_ollama_failure_falls_back_to_snippets(self, tmp_path, monkeypatch):
        """Ollama unreachable — synthesis must not raise; it returns the
        snippet-concat fallback so federation still yields an answer."""
        reset_federation_identity_for_tests()
        init_federation_identity(tmp_path / "key")

        from services.federation_query_responder import FederationQueryResponder

        responder = FederationQueryResponder(db=MagicMock())

        mock_client = MagicMock()
        mock_client.chat = AsyncMock(side_effect=TimeoutError("ollama down"))
        monkeypatch.setattr(
            "utils.llm_client.get_default_client", lambda: mock_client,
        )

        matches = [
            SimpleNamespace(snippet="snippet-1 from atoms", atom=MagicMock(), score=0.8),
            SimpleNamespace(snippet="snippet-2", atom=MagicMock(), score=0.7),
        ]
        answer = await responder._synthesize("q", matches)

        assert "snippet-1" in answer  # fallback ships the raw snippets
        assert "snippet-2" in answer

        reset_federation_identity_for_tests()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_empty_matches_returns_empty(self, tmp_path):
        """No retrieval results → empty answer (no LLM call needed)."""
        reset_federation_identity_for_tests()
        init_federation_identity(tmp_path / "key")

        from services.federation_query_responder import FederationQueryResponder

        responder = FederationQueryResponder(db=MagicMock())
        answer = await responder._synthesize("q", [])
        assert answer == ""

        reset_federation_identity_for_tests()
