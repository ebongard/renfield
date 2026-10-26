"""Ob die `tool_hints` beim Entdecken ANKOMMEN, muss im Protokoll stehen.

🛑 Der Anlass, am 2026-10-26 gemessen. Vier Hinweise standen für den Scanner in
`config/mcp_servers.yaml` (1426/791/186/379 Zeichen). Der gemountete Pfad trug
sie, der Lader trug sie, und derselbe Codepfad lieferte in einer Nachstellung
IM LAUFENDEN POD eine 1529 Zeichen lange Vorauswahlzeile.

In Produktion war dieselbe Zeile **102 Zeichen** lang und trug keinen Hinweis —
nur die erste Zeile des Docstrings vom Scanner-Server:

    - mcp.scanner.scan_document: START scanning every sheet in the document
      feeder into one searchable PDF

Vier Review-Runden lang wurde an diesen Texten gearbeitet (#1411 Sprachfalle,
Bluetooth-Abgrenzung, die `known:`-Korrektur), ohne dass einer beim Modell
ankam. Nichts im Protokoll sagte das, und erst ein eigens gebauter
Prompt-Auszug machte es sichtbar.

Der wahrscheinlichste Fehlermodus war dabei immer ein stiller Leerlauf:
`config.tool_hints.get(tool.name)` liefert bei einem Schlüssel, der zu keinem
entdeckten Werkzeug passt, schlicht `None` — ohne eine Spur. Genau das hält
dieser Test fest.
"""
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from loguru import logger

from services.mcp_client import log_hint_arrival

pytestmark = [pytest.mark.unit, pytest.mark.backend]


@dataclass
class _Werkzeug:
    original_name: str
    hint: str = ""


@dataclass
class _Zustand:
    """So viel von `MCPServerState`, wie die Funktion anfasst."""
    config: object
    all_discovered_tools: list = field(default_factory=list)


def _zustand(hinweise: dict, werkzeuge: list[_Werkzeug]) -> _Zustand:
    return _Zustand(
        config=SimpleNamespace(name="scanner", tool_hints=hinweise),
        all_discovered_tools=werkzeuge,
    )


@pytest.fixture
def protokoll():
    zeilen: list[tuple[str, str]] = []
    sink = logger.add(
        lambda m: zeilen.append((m.record["level"].name, m.record["message"])),
        level="INFO")
    yield zeilen
    logger.remove(sink)


def test_a_key_that_matches_no_tool_is_a_WARNING(protokoll):
    """🛑 Der Fall, der monatelang unsichtbar war.

    Ein Schlüssel, der zu keinem entdeckten Werkzeug passt, ist ein stiller
    Leerlauf — der Steuertext erreicht das Modell nie, und nichts sagt es.
    """
    log_hint_arrival(_zustand(
        {"scan_document": "x" * 1426, "tippfehler_tool": "y" * 100},
        [_Werkzeug("scan_document", "x" * 1426), _Werkzeug("scanner_status")],
    ))
    warnungen = [m for stufe, m in protokoll if stufe == "WARNING"]
    assert warnungen, f"kein WARNING: {protokoll}"
    assert "tippfehler_tool" in warnungen[0], (
        "die Warnung nennt den Schluessel nicht, der ins Leere greift — dann "
        f"ist sie nicht handlungsfaehig:\n{warnungen[0]}")


def test_hints_configured_but_NONE_applied_is_a_WARNING(protokoll):
    """Genau die Lage vom 2026-10-26: konfiguriert ja, angekommen nichts."""
    log_hint_arrival(_zustand(
        {"scan_document": "x" * 1426},
        [_Werkzeug("scan_document"), _Werkzeug("scanner_status")],
    ))
    warnungen = [m for stufe, m in protokoll if stufe == "WARNING"]
    assert warnungen, (
        "vier konfigurierte Hinweise, null angewendet — und kein WARNING. "
        f"Genau so blieb es unsichtbar: {protokoll}")
    assert "1 hint(s) configured, 0 applied" in warnungen[0], warnungen[0]


def test_the_healthy_case_is_INFO_with_counts(protokoll):
    """Wenn alles ankommt, genügt eine Zählung — und sie muss belastbar sein."""
    log_hint_arrival(_zustand(
        {"scan_document": "x" * 1426, "route_scan": "y" * 791},
        [_Werkzeug("scan_document", "x" * 1426), _Werkzeug("route_scan", "y" * 791)],
    ))
    infos = [m for stufe, m in protokoll if stufe == "INFO"]
    assert infos, f"keine INFO-Zeile: {protokoll}"
    assert "2 hint(s) configured, 2 applied" in infos[0], infos[0]
    assert str(1426 + 791) in infos[0], (
        f"die Gesamtlaenge fehlt — dann ist ein abgeschnittener Hinweis "
        f"nicht von einem vollstaendigen zu unterscheiden:\n{infos[0]}")
    assert not [m for stufe, m in protokoll if stufe == "WARNING"]


def test_a_server_without_hints_stays_silent(protokoll):
    """🛑 Die meisten Server haben keine `tool_hints`. Eine Zeile je Server und
    Verbindung waere Laerm und wuerde die echten Faelle zudecken."""
    log_hint_arrival(_zustand({}, [_Werkzeug("irgendwas")]))
    assert not protokoll, f"ein Server ohne Hinweise hat protokolliert: {protokoll}"


def test_the_hint_TEXT_never_reaches_the_log(protokoll):
    """🛑 Nur Namen und Längen. Hinweistexte sind Betreiberkonfiguration, aber
    die Zeile soll auch nicht um Größenordnungen aufgeblasen werden — 2689
    Zeichen Hinweis je Verbindung waeren genau das."""
    geheim = "DIESER-TEXT-DARF-NICHT-INS-PROTOKOLL"
    log_hint_arrival(_zustand(
        {"scan_document": geheim},
        [_Werkzeug("scan_document", geheim)],
    ))
    assert protokoll, "gar nichts protokolliert"
    for _, nachricht in protokoll:
        assert geheim not in nachricht, (
            f"der Hinweistext steht im Protokoll:\n{nachricht}")


def test_the_discovery_path_actually_calls_it(protokoll):
    """🛑 Die VERDRAHTUNG, nicht nur die Funktion.

    Der Fehler dieses Tages war fünfmal derselbe: ein Riegel war richtig
    gebaut und wurde nirgends gerufen, oder ein Parameter fiel eine Schicht
    höher weg, und jeder Einheitentest blieb grün. Dieser Test geht durch
    `_install_discovered_tools` — die EINE Stelle, an der eine entdeckte
    Werkzeugliste lebender Zustand wird.
    """
    from services.mcp_client import MCPManager, MCPServerConfig, MCPServerState

    cfg = MCPServerConfig(
        name="scanner", transport="streamable_http",
        tool_hints={"scan_document": "H" * 50, "gibtesnicht": "X" * 10},
    )
    state = MCPServerState(config=cfg)
    rohe = [SimpleNamespace(name="scan_document", description="Scan a stack.",
                            inputSchema={"properties": {}, "required": []})]

    # 🛑 Ein ECHTER Manager, kein `__new__`: die Nachbarschritte
    # (`_apply_tool_filter`) brauchen Zustand, den `__init__` anlegt, und ein
    # halb gebautes Objekt haette den Test an der falschen Stelle scheitern
    # lassen statt am Befund.
    mgr = MCPManager()
    mgr._install_discovered_tools(state, rohe)

    warnungen = [m for stufe, m in protokoll if stufe == "WARNING"]
    assert warnungen, (
        "der Entdeckungspfad protokolliert nichts ueber die Hinweise — dann "
        f"bleibt ihr Ausbleiben wieder unsichtbar: {protokoll}")
    assert "gibtesnicht" in warnungen[0], warnungen[0]
    # Und der Hinweis, der PASST, ist wirklich angekommen:
    assert state.all_discovered_tools[0].hint == "H" * 50
