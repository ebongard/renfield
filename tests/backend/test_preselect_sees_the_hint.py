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
from services.agent_tools import ToolDefinition

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
    zeile = _zeilen([tool])[0]
    # Der englische Teil endet nach der Grenze; was danach kommt, ist Hinweis.
    rumpf = zeile.split(": ", 1)[1][: _PRESELECT_DESC_CHARS]
    assert len(rumpf) <= _PRESELECT_DESC_CHARS
    assert "takes from half a minute" not in zeile, (
        "die generische Beschreibung wurde nicht beschnitten")


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


def test_the_registry_carries_the_hint_through():
    """Ohne dieses Feld ist der Hinweis in `description` nicht mehr
    herauszuloesen — genau daran scheiterte es."""
    from services.mcp_client import MCPToolInfo

    info = MCPToolInfo(
        server_name="scanner", original_name="scan_document",
        namespaced_name="mcp.scanner.scan_document",
        description=f"{BESCHREIBUNG} {HINWEIS}", hint=HINWEIS,
    )
    assert info.hint == HINWEIS
    assert ToolDefinition(name="x", description="y").hint == "", (
        "ohne Hinweis muss das Feld leer sein, nicht None")
