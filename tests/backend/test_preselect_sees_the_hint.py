"""Der Steuertext des Betreibers muss die Werkzeug-VORAUSWAHL erreichen.

🛑 Der Anlass, am 2026-10-26 im Protokoll gemessen. Der Eigentümer sagte
sechsmal „scanne nach …". Zweimal passierte das Falsche, und einer der beiden
Fälle war diese Schicht:

    13:20:52  Tool pre-selection: 3/33 tools selected for: 'scanne nach xidra'
    13:20:54  -> mcp.paperless.search_documents

`scan_document` fiel aus der Vorauswahl, und der Agent SUCHTE statt zu
scannen. Der `tool_hints`-Eintrag nennt wortwörtlich „scanne die Seiten nach
<Ziel>" — er stand aber hinter einer über 500 Zeichen langen englischen
Beschreibung, und diese Schicht sah nur die ersten 80 Zeichen davon. Der Text,
der genau für diese Entscheidung geschrieben wurde, erreichte sie nie.

Die 80 waren dabei nie begründet (#312, daneben nur das Wort „compact").
Nachgemessen: die Liste kostet bei 33 Werkzeugen ~866 Token, das
Kontextfenster fasst 262.144 und die Aufrufe lagen bei 2,1 % Auslastung. Die
Kürzung sparte ein halbes Prozent.
"""
import pytest

from services.agent_service import _PRESELECT_DESC_CHARS, preselect_tool_line
from services.agent_tools import AgentToolRegistry, ToolDefinition

pytestmark = [pytest.mark.unit, pytest.mark.backend]

#: So lang wie der echte `scan_document`-Hinweis am 2026-10-26.
HINWEIS = ('Startet einen NEUEN Scan. Nimmt selbst ein `target` — '
           '„scanne die Seiten nach <Ziel>", „einscannen und nach <Ziel> '
           'ablegen" gehören hierher, nicht zu route_scan.')
#: Lang und englisch, wie die Beschreibung des Herstellers.
BESCHREIBUNG = ("START scanning every sheet in the document feeder into one "
                "searchable PDF and filing it into a single Renfield instance. "
                "This returns IMMEDIATELY with a job_id — the scan itself runs "
                "in the background and takes from half a minute to several "
                "minutes. Tell the user the scan has started.")




def _zeilen(tools: list[ToolDefinition]) -> list[str]:
    """Die Zeilen, wie die PRODUKTION sie baut.

    🛑 Kein Nachbau. Die erste Fassung dieser Datei baute die Zeile selbst und
    war damit tautologisch — beide Negativkontrollen blieben gruen, obwohl der
    Produktionspfad zerschnitten war.
    """
    return [preselect_tool_line(t.name, t) for t in tools]


def test_the_hint_survives_uncut():
    """Der Steuertext geht VOLLSTÄNDIG mit — er ist der Grund, warum es ihn gibt."""
    tool = ToolDefinition(
        name="mcp.scanner.scan_document",
        description=f"{BESCHREIBUNG} {HINWEIS}",
        hint=HINWEIS,
    )
    zeile = _zeilen([tool])[0]
    assert HINWEIS in zeile, (
        "der Hinweis fehlt oder ist beschnitten — dann entscheidet die "
        f"Vorauswahl ohne ihn:\n{zeile}")
    # Und die deutsche Formulierung, an der es konkret scheiterte:
    assert "scanne die Seiten nach <Ziel>" in zeile


def test_the_generic_description_is_still_clipped():
    """Die generische Beschreibung bleibt kurz — sonst waere die Kuerzung
    nur verschoben statt begruendet."""
    tool = ToolDefinition(
        name="mcp.scanner.scan_document",
        description=f"{BESCHREIBUNG} {HINWEIS}",
        hint=HINWEIS,
    )
    # 🛑 Lang genug, dass die Grenze wirklich greift — sonst prueft der Test
    # nichts. Die erste Fassung hatte stattdessen
    # `assert len(rumpf) <= _PRESELECT_DESC_CHARS`, und das ist IMMER wahr,
    # weil `rumpf` selbst auf diese Laenge geschnitten war. Eine leere
    # Behauptung. Im Review zu #1410 gefunden.
    lang = "X" * (_PRESELECT_DESC_CHARS + 200) + "ENDE_DER_BESCHREIBUNG"
    tool = ToolDefinition(name="mcp.a.b", description=f"{lang} {HINWEIS}", hint=HINWEIS)
    zeile = _zeilen([tool])[0]
    assert "ENDE_DER_BESCHREIBUNG" not in zeile, (
        "die generische Beschreibung wurde nicht beschnitten")
    assert HINWEIS in zeile, "der Hinweis muss trotzdem vollstaendig dastehen"


def test_a_tool_without_a_hint_is_unchanged():
    """Werkzeuge ohne Steuertext verhalten sich wie vorher — die Aenderung
    darf nicht die ganze Liste aufblaehen."""
    tool = ToolDefinition(name="mcp.other.thing", description=BESCHREIBUNG)
    zeile = _zeilen([tool])[0]
    assert zeile == f"- mcp.other.thing: {BESCHREIBUNG[:_PRESELECT_DESC_CHARS]}"


def test_the_hint_is_not_duplicated():
    """🛑 Der Hinweis steckt AUCH in `description` (hinten angehaengt). Wird er
    dort nicht abgezogen, steht er zweimal in der Zeile.

    🛑 Und der Fall braucht eine KURZE Beschreibung. Die erste Fassung dieses
    Tests nahm die lange — dort ist das Abziehen wirkungslos, weil die
    Zeichengrenze den Hinweis ohnehin abschneidet, und die Negativkontrolle
    blieb gruen. Erst unter der Grenze wird die Doppelung sichtbar.
    """
    kurz = "Scan a stack."          # klar unter _PRESELECT_DESC_CHARS
    tool = ToolDefinition(
        name="mcp.scanner.scan_document",
        description=f"{kurz} {HINWEIS}",
        hint=HINWEIS,
    )
    zeile = _zeilen([tool])[0]
    # 🛑 Auf einen Textteil pruefen, der GANZ in die Zeichengrenze passt. Die
    # zweite Fassung dieses Tests nahm „scanne die Seiten nach <Ziel>" — das
    # wird im Fenster abgeschnitten und kommt darum auch ohne Abziehen nur
    # einmal vollstaendig vor. Die Kontrolle blieb gruen. „Startet einen NEUEN
    # Scan." liegt vollstaendig innerhalb der 80 Zeichen und doppelt sich.
    assert zeile.count("Startet einen NEUEN Scan.") == 1, (
        f"der Hinweis steht doppelt in der Zeile:\n{zeile}")
    assert kurz in zeile, "die kurze Beschreibung darf nicht verloren gehen"


def test_the_hint_survives_the_whole_chain():
    """🛑 Die ECHTEN Stationen, nicht die Dataclass.

    Die erste Fassung baute `MCPToolInfo(hint=…)` selbst und prueffte
    `info.hint == HINWEIS` — das testete das Feld, nicht die UEBERGABE. Beide
    Stationen (`mcp_client._tool_info_from_mcp` und
    `agent_tools._register_mcp_tools`) blieben unmessen, waehrend der Docstring
    behauptete, genau daran sei es gescheitert. Im Review zu #1410 gefunden.
    """
    from types import SimpleNamespace

    from services.mcp_client import MCPManager, MCPServerConfig

    cfg = MCPServerConfig(name="scanner", transport="streamable_http",
                          tool_hints={"scan_document": HINWEIS})
    rohes = SimpleNamespace(name="scan_document", description=BESCHREIBUNG,
                            inputSchema={"properties": {}, "required": []})

    # Station 1: der Entdecker
    info = MCPManager._tool_info_from_mcp(cfg, rohes)
    assert info.hint == HINWEIS, "Station 1 verliert den Hinweis"
    assert info.description.endswith(HINWEIS), (
        "die Beschreibung muss den Hinweis weiter hinten tragen — darauf baut "
        "das Abziehen in `preselect_tool_line`")

    # Station 2: die Registratur des Agenten
    r = AgentToolRegistry.__new__(AgentToolRegistry)
    r._tools = {}
    mgr = SimpleNamespace(get_all_tools=lambda: [info])
    AgentToolRegistry._register_mcp_tools(r, mgr)
    tool = r._tools["mcp.scanner.scan_document"]
    assert tool.hint == HINWEIS, "Station 2 verliert den Hinweis"

    # Und die Zeile, die daraus wird, traegt ihn ungekuerzt.
    assert HINWEIS in preselect_tool_line(tool.name, tool)


def test_a_hint_with_stray_whitespace_is_not_duplicated():
    """🛑 Ein quotierter YAML-Eintrag mit Leerzeichen am Rand genuegte, um den
    Hinweis DOPPELT in die Zeile zu bringen: die Verbindung strippt das Ganze,
    `endswith(hint)` passt dann nicht mehr. Darum wird an der Quelle
    getrimmt."""
    from types import SimpleNamespace

    from services.mcp_client import MCPManager, MCPServerConfig

    cfg = MCPServerConfig(name="scanner", transport="streamable_http",
                          tool_hints={"scan_document": f"  {HINWEIS}  "})
    rohes = SimpleNamespace(name="scan_document", description="Scan a stack.",
                            inputSchema={"properties": {}, "required": []})
    info = MCPManager._tool_info_from_mcp(cfg, rohes)
    tool = ToolDefinition(name=info.namespaced_name,
                          description=info.description, hint=info.hint)
    zeile = preselect_tool_line(tool.name, tool)
    assert zeile.count("Startet einen NEUEN Scan.") == 1, (
        f"der Hinweis steht doppelt in der Zeile:\n{zeile}")


def test_a_tool_without_a_hint_has_an_empty_field():
    assert ToolDefinition(name="x", description="y").hint == "", (
        "ohne Hinweis muss das Feld leer sein, nicht None")


def test_the_real_config_still_rules_out_the_search_reading():
    """🛑 Die ECHTE Konfiguration, nicht die Konstante dieser Datei.

    Der zweite Fehlschlag vom 2026-10-26 war nicht der Mechanismus, sondern der
    TEXT: „scanne nach <X>" parst syntaktisch genau wie „suche nach <X>", und
    der damalige Hinweis benannte das nicht. Das Modell nahm <X> als Suchbegriff
    und griff `paperless.search_documents`.

    Alles darüber in dieser Datei arbeitet mit `HINWEIS`, einer Konstante — der
    Hinweis, der in Produktion wirkt, wurde von keinem Test berührt. Wird er
    später „aufgeräumt", fällt das erst im nächsten Vorfall auf. Genau so ist es
    passiert. Im Review zu #1411 gefunden.
    """
    from pathlib import Path

    import yaml

    wurzel = Path(__file__).resolve().parents[2]
    eintraege = yaml.safe_load(
        (wurzel / "config" / "mcp_servers.yaml").read_text())["servers"]
    scanner = next(e for e in eintraege if e["name"] == "scanner")
    hinweis = (scanner.get("tool_hints") or {}).get("scan_document", "")

    assert hinweis, "`scan_document` hat keinen Hinweis mehr — die Vorauswahl entscheidet dann blind"
    # Die Zweideutigkeit muss BESTRITTEN werden, nicht nur das Ziel benannt.
    assert "Suchbegriff" in hinweis, (
        "der Hinweis schliesst die Lesart 'Suchbegriff' nicht mehr aus — das "
        "war der Fehlgriff vom 2026-10-26")
    assert "ZIEL" in hinweis, "der Hinweis sagt nicht mehr, dass <X> das Ziel ist"
    # Und die Formulierung, an der es konkret scheiterte, muss vorkommen.
    assert "scanne nach" in hinweis, (
        "der Hinweis nennt die zweideutige Formulierung selbst nicht mehr")
