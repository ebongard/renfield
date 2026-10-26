"""Ob die `tool_hints` beim Entdecken ANKOMMEN, muss im Protokoll stehen.

🛑 Der Anlass, am 2026-10-26 gemessen. Vier Hinweise standen für den Scanner in
`config/mcp_servers.yaml` (1426/791/186/379 Zeichen). Der gemountete Pfad trug
sie, der Lader trug sie, und derselbe Codepfad lieferte in einer Nachstellung
IM LAUFENDEN POD eine 1529 Zeichen lange Vorauswahlzeile.

In Produktion war dieselbe Zeile **102 Zeichen** lang und trug keinen Hinweis —
nur die erste Zeile des Docstrings vom Scanner-Server. Vier Review-Runden
arbeiteten an diesen Texten (#1411 Sprachfalle, Bluetooth-Abgrenzung, die
`known:`-Korrektur), ohne dass einer beim Modell ankam. Nichts im Protokoll
sagte es; sichtbar wurde es erst durch einen eigens gebauten Prompt-Auszug.

🛑 **Abwesenheit ist keine Diagnose.** Die erste Fassung dieser Funktion kehrte
bei einem Server ohne Hinweise still zurück — und ein Test schrieb diese Stille
als gewolltes Verhalten fest. Damit sahen drei verschiedene Lagen gleich aus:
Server ohne Hinweise, Konfiguration ohne Hinweise, Bild ohne diese Funktion.
Im Review zu #1415 gefunden und umgedreht.
"""
import inspect
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
    tools: list = field(default_factory=list)


def _zustand(hinweise: dict, entdeckt: list[_Werkzeug],
             angeboten: list[_Werkzeug] | None = None) -> _Zustand:
    """`angeboten=None` heißt: der Filter wirft nichts weg (der Normalfall)."""
    return _Zustand(
        config=SimpleNamespace(name="scanner", tool_hints=hinweise),
        all_discovered_tools=entdeckt,
        tools=list(entdeckt) if angeboten is None else angeboten,
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
    """Ein Schlüssel, der zu keinem entdeckten Werkzeug passt, war ein stiller
    Leerlauf: `config.tool_hints.get(tool.name)` liefert `None`, ohne Spur."""
    log_hint_arrival(_zustand(
        {"scan_document": "x" * 1426, "tippfehler_tool": "y" * 100},
        [_Werkzeug("scan_document", "x" * 1426), _Werkzeug("scanner_status")],
    ))
    warnungen = [m for stufe, m in protokoll if stufe == "WARNING"]
    assert warnungen, f"kein WARNING: {protokoll}"
    assert "tippfehler_tool" in warnungen[0], (
        "die Warnung nennt den Schluessel nicht, der ins Leere greift — dann "
        f"ist sie nicht handlungsfaehig:\n{warnungen[0]}")


def test_hints_configured_but_NONE_attached_is_a_WARNING(protokoll):
    """Genau die Lage vom 2026-10-26: konfiguriert ja, angekommen nichts."""
    log_hint_arrival(_zustand(
        {"scan_document": "x" * 1426},
        [_Werkzeug("scan_document"), _Werkzeug("scanner_status")],
    ))
    warnungen = [m for stufe, m in protokoll if stufe == "WARNING"]
    assert warnungen, f"kein WARNING: {protokoll}"
    assert "0 attached" in warnungen[0], warnungen[0]


def test_a_hinted_tool_filtered_OUT_of_the_offered_set_is_a_WARNING(protokoll):
    """🛑 Angehängt ist nicht angeboten.

    `prompt_tools` oder eine DB-Übersteuerung können ein Werkzeug aus
    `state.tools` werfen, NACHDEM sein Hinweis angehängt wurde. Die erste
    Fassung zählte nur `all_discovered_tools` und hätte das als GESUND
    gemeldet. Im Review zu #1415 gefunden.
    """
    log_hint_arrival(_zustand(
        {"scan_document": "x" * 1426},
        [_Werkzeug("scan_document", "x" * 1426), _Werkzeug("scanner_status")],
        angeboten=[_Werkzeug("scanner_status")],   # der behinweiste fiel raus
    ))
    warnungen = [m for stufe, m in protokoll if stufe == "WARNING"]
    assert warnungen, (
        f"ein behinweistes Werkzeug wurde herausgefiltert und das galt als "
        f"gesund: {protokoll}")
    assert "filtered out" in warnungen[0], warnungen[0]


def test_the_healthy_case_is_INFO_with_counts(protokoll):
    """Wenn alles ankommt, genügt eine Zählung — mit Gesamtlänge, sonst wäre
    ein abgeschnittener Hinweis nicht von einem vollständigen zu
    unterscheiden."""
    log_hint_arrival(_zustand(
        {"scan_document": "x" * 1426, "route_scan": "y" * 791},
        [_Werkzeug("scan_document", "x" * 1426), _Werkzeug("route_scan", "y" * 791)],
    ))
    infos = [m for stufe, m in protokoll if stufe == "INFO"]
    assert infos, f"keine INFO-Zeile: {protokoll}"
    assert "2 hint(s) configured, 2 attached, 2 offered" in infos[0], infos[0]
    assert str(1426 + 791) in infos[0], infos[0]
    assert not [m for stufe, m in protokoll if stufe == "WARNING"]


def test_a_server_WITHOUT_hints_still_says_so(protokoll):
    """🛑 Umgedreht gegenüber der ersten Fassung, und das ist der Kern.

    Sie kehrte hier still zurück, und ein Test hielt diese Stille fest. Damit
    war „Server hat keine Hinweise" nicht von „die Konfiguration dieses
    Prozesses hat keine" und nicht von „dieses Bild kennt die Funktion nicht"
    zu unterscheiden — drei Lagen, eine Stille. Genau das Rätselraten, das
    diesen Zweig ausgelöst hat.
    """
    log_hint_arrival(_zustand({}, [_Werkzeug("irgendwas")]))
    assert protokoll, (
        "ein Server ohne Hinweise schweigt — dann ist Abwesenheit wieder eine "
        "Diagnose, und drei verschiedene Lagen sehen gleich aus")
    stufe, nachricht = protokoll[0]
    assert stufe == "INFO", f"das ist kein Problem, also kein WARNING: {nachricht}"
    assert "0 hint(s) configured" in nachricht, nachricht


def test_the_line_appears_only_ON_CHANGE(protokoll):
    """🛑 Die Entdeckung läuft alle ~60 s je Server (`mcp_refresh_interval`;
    `refresh_tools` liest den per-Server-Wert NICHT). Eine Zeile je Durchlauf
    wären ~60 je Stunde, und ein dauerhaft unpassender Schlüssel ein
    Dauerfeuer, das die echten Fälle zudeckt."""
    z = _zustand({"scan_document": "x" * 10},
                 [_Werkzeug("scan_document", "x" * 10)])
    log_hint_arrival(z)
    log_hint_arrival(z)
    log_hint_arrival(z)
    assert len(protokoll) == 1, (
        f"drei Durchlaeufe, {len(protokoll)} Zeilen — ein unveraenderter "
        "Zustand darf nur einmal protokollieren")

    # Ändert sich etwas, muss es wieder erscheinen.
    z.all_discovered_tools = [_Werkzeug("scan_document")]
    z.tools = list(z.all_discovered_tools)
    log_hint_arrival(z)
    assert len(protokoll) == 2, (
        "der Hinweis ist weggefallen und das Protokoll schweigt — dann nuetzt "
        "die Drosselung nichts")


def test_the_hint_TEXT_never_reaches_the_log(protokoll):
    """🛑 Nur Namen und Längen — und zwar auf BEIDEN Zweigen.

    Die erste Fassung prüfte nur den INFO-Zweig; der WARNING-Zweig ist aber
    der, der variable Inhalte einsetzt. Im Review zu #1415 gefunden.
    """
    geheim = "DIESER-TEXT-DARF-NICHT-INS-PROTOKOLL"
    log_hint_arrival(_zustand({"a": geheim}, [_Werkzeug("a", geheim)]))
    log_hint_arrival(_zustand(
        {"a": geheim, "fehlt": geheim}, [_Werkzeug("a", geheim)]))
    assert [s for s, _ in protokoll].count("WARNING") == 1, protokoll
    for _, nachricht in protokoll:
        assert geheim not in nachricht, (
            f"der Hinweistext steht im Protokoll:\n{nachricht}")


def test_the_discovery_path_actually_calls_it(protokoll):
    """🛑 Die VERDRAHTUNG, nicht nur die Funktion.

    Der Fehler dieses Tages war fünfmal derselbe: ein Riegel richtig gebaut
    und nirgends gerufen, oder ein Parameter eine Schicht höher weggefallen —
    und jeder Einheitentest blieb grün.
    """
    from services.mcp_client import MCPManager, MCPServerConfig, MCPServerState

    cfg = MCPServerConfig(
        name="scanner", transport="streamable_http",
        tool_hints={"scan_document": "H" * 50, "gibtesnicht": "X" * 10},
    )
    state = MCPServerState(config=cfg)
    rohe = [SimpleNamespace(name="scan_document", description="Scan a stack.",
                            inputSchema={"properties": {}, "required": []})]

    # 🛑 Ein ECHTER Manager, kein `__new__`: `_apply_tool_filter` braucht
    # Zustand, den `__init__` anlegt (`_tool_overrides`). Geprueft: `__init__`
    # macht kein I/O — nur Zuweisungen.
    mgr = MCPManager()
    mgr._install_discovered_tools(state, rohe)

    warnungen = [m for stufe, m in protokoll if stufe == "WARNING"]
    assert warnungen, (
        "der Entdeckungspfad protokolliert nichts ueber die Hinweise — dann "
        f"bleibt ihr Ausbleiben wieder unsichtbar: {protokoll}")
    assert "gibtesnicht" in warnungen[0], warnungen[0]
    assert state.all_discovered_tools[0].hint == "H" * 50


def test_the_two_discovery_entry_points_reach_the_install():
    """🛑 Und die Verdrahtung DAVOR.

    Der Test oben beweist nur, dass `_install_discovered_tools` die Zählung
    ruft — nicht, dass Verbinden und Auffrischen dort ankommen. Ein Umbau, der
    die Entdeckung wieder in `_connect_server` einzieht, liesse ihn grün und
    die Zeile verschwinden. Im Review zu #1415 benannt.
    """
    from services.mcp_client import MCPManager

    for name in ("_connect_server", "refresh_tools"):
        quelle = inspect.getsource(getattr(MCPManager, name))
        assert "_install_discovered_tools" in quelle, (
            f"`{name}` geht nicht mehr ueber `_install_discovered_tools` — "
            "dann entfaellt die Hinweis-Zaehlung auf diesem Weg")
