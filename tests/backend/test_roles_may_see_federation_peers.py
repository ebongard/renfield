"""A role has to be allowed to see the federation peer (layer 4, F2').

🛑 The occasion, measured on 2026-10-29: the federation channel was fully
built, both instances paired, the person link closed (`querier_ref` identical
on both sides) -- and had **never** had a caller. `federation_query_log`
counted 0 rows on both instances, not since some day but at all.

Cause: `agent_roles.yaml` gives each role an allowlist
(`documents: [paperless, email, scanner]`), `agent_tools.py` compares
**exactly**, and a peer server is called `peer_<id>` -- the name comes into
being at pairing time and cannot stand in a configuration that exists before
it. Only `general` (`mcp_servers = None`) ever saw it; document questions land
on `documents`.

The same class as the two-step trap for `internal.*` in CLAUDE.md
("registration AND an entry in the role"), only sharper: the second step was
**impossible**.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from services.agent_tools import AgentToolRegistry
from services.mcp_client import MCPTransportType

pytestmark = pytest.mark.unit


def _manager(*, mit_peer: bool = True) -> MagicMock:
    """A manager with two real servers and optionally a peer.

    `is_federation_server` is NOT reimplemented but derived from the transport
    -- otherwise the test would check its own stand-in instead of the property
    the filter rests on.
    """
    tools = []
    for server, name in (("paperless", "search_documents"), ("scanner", "scan_document")):
        w = MagicMock()
        w.server_name, w.original_name = server, name
        w.namespaced_name = f"mcp.{server}.{name}"
        w.description, w.hint, w.input_schema = f"{name} tool", "", {}
        tools.append(w)
    transporte = {
        "paperless": MCPTransportType.STDIO,
        "scanner": MCPTransportType.STREAMABLE_HTTP,
    }
    if mit_peer:
        w = MagicMock()
        w.server_name, w.original_name = "peer_3", "query_brain"
        w.namespaced_name = "mcp.peer_3.query_brain"
        w.description, w.hint, w.input_schema = "ask a paired peer", "", {}
        tools.append(w)
        transporte["peer_3"] = MCPTransportType.FEDERATION

    m = MagicMock()
    m.get_all_tools.return_value = tools
    m.is_federation_server.side_effect = (
        lambda n: transporte.get(n) == MCPTransportType.FEDERATION
    )
    return m


def _namen(server_filter, federation: bool, *, mit_peer: bool = True) -> set[str]:
    r = AgentToolRegistry(_init_only=True)
    r._register_mcp_tools(_manager(mit_peer=mit_peer),
                          server_filter=server_filter, federation=federation)
    # 🛑 Only the MCP names: the constructor also registers the `internal.*`
    # tools, and those are no business of this filter.
    return {n for n in r._tools if n.startswith("mcp.")}


class TestTheFilter:

    def test_a_federation_role_sees_the_peer_although_the_allowlist_cannot_name_it(self):
        namen = _namen(["paperless", "scanner"], True)
        assert "mcp.peer_3.query_brain" in namen
        # And the listed ones stay -- the branch widens, it does not replace.
        assert "mcp.paperless.search_documents" in namen

    def test_WITHOUT_the_flag_the_peer_stays_out(self):
        """🛑 Negative control. Without it the test above only checked that
        tools arrive at all. This is the state from before the fix -- and the
        reason the channel never had a caller."""
        namen = _namen(["paperless", "scanner"], False)
        assert "mcp.peer_3.query_brain" not in namen
        assert "mcp.paperless.search_documents" in namen

    def test_the_flag_admits_ONLY_federation_transport(self):
        """🛑 The flag is not a master key: a server that is not listed and
        has an ordinary transport stays out, even with `federation=True`.
        Otherwise admitting one channel would have become switching the
        allowlist off."""
        namen = _namen(["paperless"], True)
        assert "mcp.peer_3.query_brain" in namen
        assert "mcp.scanner.scan_document" not in namen

    def test_a_role_without_an_allowlist_is_unchanged(self):
        """`general` carries `None` and always saw the peer -- this branch
        must not have changed."""
        assert "mcp.peer_3.query_brain" in _namen(None, False)

    def test_no_peer_connected_is_not_an_error(self):
        namen = _namen(["paperless"], True, mit_peer=False)
        assert namen == {"mcp.paperless.search_documents"}


class TestTheRouter:

    def _router(self):
        from services.agent_router import AgentRole, AgentRouter

        r = AgentRouter.__new__(AgentRouter)
        r.roles = {
            "documents": AgentRole(
                name="documents", description={"de": "x"},
                mcp_servers=["paperless", "email", "scanner"], federation=True),
            "smart_home": AgentRole(
                name="smart_home", description={"de": "x"},
                mcp_servers=["homeassistant"], federation=False),
            "general": AgentRole(
                name="general", description={"de": "x"}, mcp_servers=None),
        }
        return r

    def test_a_peer_intent_now_finds_a_role(self):
        """🛑 The PRE-EXISTING defect, found by the architecture review.

        `role_for_intent` filtered with `role.mcp_servers and server in ...`.
        The allowlist cannot name the peer, and `general` carries `None` --
        falsy, so never a candidate here. For `mcp.peer_3.query_brain` it
        returned `None`: the correction "that was a federation matter" ran into
        nothing. Fixing only the tool filter does NOT fix this along with it --
        which is why it belongs in the same pass.
        """
        assert self._router().role_for_intent("mcp.peer_3.query_brain") == "documents"

    def test_a_role_without_the_flag_is_not_offered_for_a_peer_intent(self):
        """Negative control on the mapping: not simply any role at all."""
        from services.agent_router import AgentRole

        r = self._router()
        r.roles["documents"] = AgentRole(
            name="documents", description={"de": "x"},
            mcp_servers=["paperless"], federation=False)
        assert r.role_for_intent("mcp.peer_3.query_brain") is None

    def test_an_ordinary_intent_is_unaffected(self):
        """Negative control on the branch itself: the new path only takes
        hold when the ordinary lookup stays empty AND it is a peer intent."""
        r = self._router()
        assert r.role_for_intent("mcp.paperless.search_documents") == "documents"
        assert r.role_for_intent("mcp.homeassistant.turn_on") == "smart_home"
        assert r.role_for_intent("mcp.unbekannt.tool") is None

    def test_a_federation_role_stays_available_on_a_peer_alone(self):
        """🛑 The second place that was missed. `connected_set` contains the
        peer (`peer_mcp_registry` sets `connected=True`, in its own comment "a
        lie -- there's no MCP session", set for exactly this). If the real
        servers fail while the peer is still up, the role counted wrongly as
        unavailable."""
        from services.agent_router import _filter_available_roles

        remaining = _filter_available_roles(self._router().roles, ["peer_3"])
        assert "documents" in remaining
        # Negative control: a role without the flag and without its servers drops out.
        assert "smart_home" not in remaining
        # `general` always stays.
        assert "general" in remaining
