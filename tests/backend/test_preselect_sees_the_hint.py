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




def _scan_document_hinweis() -> str:
    """Der Hinweis aus der ECHTEN Konfiguration — dieselbe Datei, die ausgeliefert wird."""
    from pathlib import Path

    import yaml

    wurzel = Path(__file__).resolve().parents[2]
    eintraege = yaml.safe_load(
        (wurzel / "config" / "mcp_servers.yaml").read_text())["servers"]
    scanner = next(e for e in eintraege if e["name"] == "scanner")
    return (scanner.get("tool_hints") or {}).get("scan_document", "")


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
    hinweis = _scan_document_hinweis()

    assert hinweis, "`scan_document` hat keinen Hinweis mehr — die Vorauswahl entscheidet dann blind"
    # Die Zweideutigkeit muss BESTRITTEN werden, nicht nur das Ziel benannt.
    assert "Suchbegriff" in hinweis, (
        "der Hinweis schliesst die Lesart 'Suchbegriff' nicht mehr aus — das "
        "war der Fehlgriff vom 2026-10-26")
    assert "ZIEL" in hinweis, "der Hinweis sagt nicht mehr, dass <X> das Ziel ist"
    # Und die Formulierung, an der es konkret scheiterte, muss vorkommen.
    assert "scanne nach" in hinweis, (
        "der Hinweis nennt die zweideutige Formulierung selbst nicht mehr")


def test_the_hint_does_not_claim_the_bluetooth_scan():
    """🛑 Ein Hinweis darf kein fremdes Gebiet beanspruchen — dritte Instanz.

    In der Rolle `general` (`agent_roles.yaml`: `mcp_servers: null`,
    `internal_tools: null`) liegen `scan_document` und `internal.bluetooth_scan`
    in DERSELBEN Vorauswahlliste, und `general` ist der Rückfall des Routers bei
    Parse-Fehler und Zeitüberschreitung. Der Rollenzaun
    („NUR Bluetooth-Funkgeraete — NICHT Dokumente/Papier einscannen") greift
    dort nicht.

    Die erste Fassung dieses Hinweises sagte unbedingt „`scanne` bedeutet immer
    Papier einlesen". Damit wäre „scanne nach Bluetooth-Geräten" beim
    Papierscanner gelandet: ein Fehlgriff gegen einen anderen getauscht, genau
    das Muster, das `.claude/rules/scanner.md` zwei Zeilen darüber schon zweimal
    festhält. Im adversarialen Durchgang zu #1411 gefunden.
    """
    hinweis = _scan_document_hinweis()
    assert "bluetooth_scan" in hinweis, (
        "der Hinweis grenzt die Funksuche nicht ab — „scanne nach "
        "Bluetooth-Geraeten\" landet dann beim Papierscanner")
    # 🛑 Nicht auf „PAPIER" pruefen: das passt auch INNERHALB von
    # „PAPIERSCANNER", der abgrenzende Satz koennte also entfallen und der
    # Test bliebe gruen. Auf die Einschraenkung selbst pruefen.
    assert "gilt nur für PAPIER" in hinweis, (
        "der Hinweis schraenkt seine Regel nicht mehr auf Papier ein")


def test_the_hint_keeps_the_way_back_from_a_display_name():
    """Die Kennung verlangen genügt nicht — der Weg zurück muss dastehen.

    Der Hinweis fordert eine technische Kennung und zugleich, „scanne nach
    <X>" als Ziel zu lesen. Für „scanne nach Haushalt" widersprechen sich beide,
    wenn nicht dasteht, wie aus der Anzeigebezeichnung die Kennung wird. Der
    Nachbar `route_scan` trägt diese Auflösung seit jeher; `scan_document` hatte
    sie verloren.
    """
    hinweis = _scan_document_hinweis()
    # 🛑 Nicht blosse Ko-Praesenz pruefen: „Haushalt" und „household" stehen
    # auch in anderen Saetzen („Derzeit `household` und `xidra`"), der
    # Abbildungssatz koennte also entfallen und der Test bliebe gruen. Auf die
    # Anweisung pruefen, die die Abbildung wirklich vollzieht.
    assert "`target=household`" in hinweis, (
        "der Hinweis vollzieht die Abbildung Anzeigebezeichnung -> Kennung "
        "nicht mehr an einem Beispiel")
    assert "Haushalt" in hinweis, (
        "der Hinweis nennt die deutsche Anzeigebezeichnung nicht mehr, an der "
        "es scheitert")


def test_the_hint_promises_no_error_channel_that_is_walled_off():
    """🛑 Dieser Test stand erst UMGEKEHRT da — die Absicht war falsch.

    Die erste Fassung forderte, der Hinweis müsse die gültige Zielliste nennen,
    die bei einem unbekannten Wert in der Fehlermeldung mitkommt (`known: [...]`).
    Das ist vom Nachbarn `route_scan` abgeschrieben, wo es SYNCHRON im Aufruf
    stimmt. Für `scan_document` stimmt es nicht und kann es nicht:

    - der Auftrag startet und liefert `{job_id, status: running}` sofort; das
      Ziel wird erst im Hintergrund geprüft;
    - die Fertigmeldung trägt NUR einen Code — `ScanJobResult` in
      `api/routes/scanner_jobs.py:93`: „A code, never the free-form error text:
      the message is rendered from fixed localised templates";
    - `unknown_target` wird zu einer festen Vorlage („das angegebene Ziel ist
      nicht konfiguriert", `services/scanner_jobs.py:137`) — ohne Liste.

    Der Hinweis hätte das Modell also auf einen Rückweg gestellt, den die
    Architektur ABSICHTLICH zumauert (freier Fehlertext wird nie Chat-Inhalt).
    Statt dessen sagt er jetzt, dass ein unbekanntes Ziel erst spät auffällt —
    und was dann zu tun ist: nicht raten.

    Im zweiten adversarialen Durchgang zu #1411 gefunden.
    """
    hinweis = _scan_document_hinweis()
    assert "known:" not in hinweis, (
        "der Hinweis verspricht die `known: [...]`-Liste — die kann den Agenten "
        "auf diesem Weg nie erreichen")
    # Betonung durch Großschreibung ist in diesen Hinweisen Stilmittel
    # („NICHT raten"), darum unabhängig davon vergleichen.
    klein = hinweis.lower()
    assert "nicht raten" in klein or "nie raten" in klein, (
        "der Hinweis sagt nicht, was bei unsicherem Ziel zu tun ist")


def test_the_pending_scans_hint_does_not_claim_the_general_question():
    """🛑 Dieselbe Klasse, anderes Werkzeug — und mein erster Sweep fand sie nicht.

    `list_pending_scans` beanspruchte „was ist offen" unbedingt. Die Frage
    gehört in der Rolle `general` auch `internal.ingest_status`
    („gibt es einen Rückstau?", `services/kb_maintenance_tool.py:259`) und
    `internal.list_unfiled_documents`
    („welche Dokumente konnten nicht abgelegt werden?", `:336`).

    Mein Klassen-Sweep hatte „was ist offen" sogar ausgedruckt und für
    unbesetzt erklärt — er hatte nur `internal_tools.py` und die
    `examples:`-Blöcke durchsucht, nicht die eigenen `*_tool.py`-Dateien. Die
    Behauptung „keine Kollision" war damit falsch gemessen, nicht falsch
    geschlossen.
    """
    from pathlib import Path

    import yaml

    wurzel = Path(__file__).resolve().parents[2]
    eintraege = yaml.safe_load(
        (wurzel / "config" / "mcp_servers.yaml").read_text())["servers"]
    scanner = next(e for e in eintraege if e["name"] == "scanner")
    hinweis = (scanner.get("tool_hints") or {}).get("list_pending_scans", "")

    assert hinweis, "`list_pending_scans` hat keinen Hinweis mehr"
    assert "ingest_status" in hinweis or "list_unfiled_documents" in hinweis, (
        "der Hinweis grenzt die allgemeine Frage „was ist offen\" nicht gegen "
        "die Nachbarn ab")
