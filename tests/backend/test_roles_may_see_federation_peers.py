"""Eine Rolle muss den Föderationspeer sehen dürfen (Schicht 4, F2′).

🛑 Der Anlass, am 2026-10-29 gemessen: der Föderationskanal war fertig gebaut,
beide Instanzen gepaart, die Personenverknüpfung geschlossen (`querier_ref` auf
beiden Seiten identisch) — und hatte **nie** einen Aufrufer.
`federation_query_log` zählte auf beiden Instanzen 0 Zeilen, nicht seit einem
Tag, sondern überhaupt.

Ursache: `agent_roles.yaml` gibt je Rolle eine Positivliste
(`documents: [paperless, email, scanner]`), `agent_tools.py` vergleicht
**exakt**, und ein Peer-Server heißt `peer_<id>` — der Name entsteht beim
Paaren und kann in einer Konfiguration, die vorher existiert, nicht stehen. Nur
`general` (`mcp_servers = None`) sah ihn je; Dokumentfragen landen bei
`documents`.

Dieselbe Klasse wie die Zwei-Schritt-Falle für `internal.*` in CLAUDE.md
(„Registrierung UND Eintrag in die Rolle"), nur schärfer: der zweite Schritt war
**unmöglich**.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from services.agent_tools import AgentToolRegistry
from services.mcp_client import MCPTransportType

pytestmark = pytest.mark.unit


def _manager(*, mit_peer: bool = True) -> MagicMock:
    """Ein Manager mit zwei echten Servern und optional einem Peer.

    `is_federation_server` wird NICHT nachgebaut, sondern aus dem Transport
    abgeleitet — sonst prüfte der Test seine eigene Attrappe statt die
    Eigenschaft, auf die der Filter sich stützt.
    """
    werkzeuge = []
    for server, name in (("paperless", "search_documents"), ("scanner", "scan_document")):
        w = MagicMock()
        w.server_name, w.original_name = server, name
        w.namespaced_name = f"mcp.{server}.{name}"
        w.description, w.hint, w.input_schema = f"{name} tool", "", {}
        werkzeuge.append(w)
    transporte = {
        "paperless": MCPTransportType.STDIO,
        "scanner": MCPTransportType.STREAMABLE_HTTP,
    }
    if mit_peer:
        w = MagicMock()
        w.server_name, w.original_name = "peer_3", "query_brain"
        w.namespaced_name = "mcp.peer_3.query_brain"
        w.description, w.hint, w.input_schema = "ask a paired peer", "", {}
        werkzeuge.append(w)
        transporte["peer_3"] = MCPTransportType.FEDERATION

    m = MagicMock()
    m.get_all_tools.return_value = werkzeuge
    m.is_federation_server.side_effect = (
        lambda n: transporte.get(n) == MCPTransportType.FEDERATION
    )
    return m


def _namen(server_filter, federation: bool, *, mit_peer: bool = True) -> set[str]:
    r = AgentToolRegistry(_init_only=True)
    r._register_mcp_tools(_manager(mit_peer=mit_peer),
                          server_filter=server_filter, federation=federation)
    # 🛑 Nur die MCP-Namen: der Konstruktor registriert auch die
    # `internal.*`-Werkzeuge, und die gehen diesen Filter nichts an.
    return {n for n in r._tools if n.startswith("mcp.")}


class TestTheFilter:

    def test_a_federation_role_sees_the_peer_although_the_allowlist_cannot_name_it(self):
        namen = _namen(["paperless", "scanner"], True)
        assert "mcp.peer_3.query_brain" in namen
        # Und die gelisteten bleiben da — der Zweig erweitert, er ersetzt nicht.
        assert "mcp.paperless.search_documents" in namen

    def test_WITHOUT_the_flag_the_peer_stays_out(self):
        """🛑 Negativkontrolle. Ohne sie prüfte der Test oben nur, dass
        Werkzeuge überhaupt ankommen. Dies ist der Zustand von vor dem Fix —
        und der Grund, warum der Kanal nie einen Aufrufer hatte."""
        namen = _namen(["paperless", "scanner"], False)
        assert "mcp.peer_3.query_brain" not in namen
        assert "mcp.paperless.search_documents" in namen

    def test_the_flag_admits_ONLY_federation_transport(self):
        """🛑 Das Flag ist kein Generalschlüssel: ein nicht gelisteter Server
        mit gewöhnlichem Transport bleibt draußen, auch mit `federation=True`.
        Sonst wäre aus der Zulassung eines Kanals das Abschalten der
        Positivliste geworden."""
        namen = _namen(["paperless"], True)
        assert "mcp.peer_3.query_brain" in namen
        assert "mcp.scanner.scan_document" not in namen

    def test_a_role_without_an_allowlist_is_unchanged(self):
        """`general` trägt `None` und sah den Peer schon immer — dieser Zweig
        darf sich nicht verändert haben."""
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
        """🛑 Der VORBESTEHENDE Fehler, vom Architektur-Review gefunden.

        `role_for_intent` filterte mit `role.mcp_servers and server in …`.
        Die Positivliste kann den Peer nicht nennen, und `general` trägt
        `None` — falsy, also hier nie Kandidat. Für `mcp.peer_3.query_brain`
        kam `None` zurück: die Korrektur „das war eine Föderationssache" lief
        ins Leere. Wer nur den Werkzeugfilter repariert, repariert dies NICHT
        mit — darum gehört es in denselben Durchgang.
        """
        assert self._router().role_for_intent("mcp.peer_3.query_brain") == "documents"

    def test_a_role_without_the_flag_is_not_offered_for_a_peer_intent(self):
        """Negativkontrolle zur Zuordnung: nicht einfach irgendeine Rolle."""
        from services.agent_router import AgentRole

        r = self._router()
        r.roles["documents"] = AgentRole(
            name="documents", description={"de": "x"},
            mcp_servers=["paperless"], federation=False)
        assert r.role_for_intent("mcp.peer_3.query_brain") is None

    def test_an_ordinary_intent_is_unaffected(self):
        """Negativkontrolle zum Zweig selbst: der neue Pfad greift nur, wenn
        die gewöhnliche Suche leer bleibt UND es ein Peer-Intent ist."""
        r = self._router()
        assert r.role_for_intent("mcp.paperless.search_documents") == "documents"
        assert r.role_for_intent("mcp.homeassistant.turn_on") == "smart_home"
        assert r.role_for_intent("mcp.unbekannt.tool") is None

    def test_a_federation_role_stays_available_on_a_peer_alone(self):
        """🛑 Die zweite übersehene Stelle. `connected_set` enthält den Peer
        (`peer_mcp_registry` setzt `connected=True`, im eigenen Kommentar „a
        lie — there's no MCP session", gesetzt genau dafür). Fallen die realen
        Server aus, während der Peer steht, galt die Rolle fälschlich als nicht
        verfügbar."""
        from services.agent_router import _filter_available_roles

        uebrig = _filter_available_roles(self._router().roles, ["peer_3"])
        assert "documents" in uebrig
        # Negativkontrolle: eine Rolle ohne Flag und ohne ihre Server fällt weg.
        assert "smart_home" not in uebrig
        # `general` bleibt immer.
        assert "general" in uebrig
