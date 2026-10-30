"""The operator's steering text has to reach the tool PRE-SELECTION.

🛑 The occasion, measured in the log on 2026-10-26. The owner said "scanne nach
..." six times. Twice the wrong thing happened, and one of the two cases was
this layer:

    13:20:52  Tool pre-selection: 3/33 tools selected for: 'scanne nach xidra'
    13:20:54  -> mcp.paperless.search_documents

`scan_document` fell out of the pre-selection, and the agent SEARCHED instead
of scanning. The `tool_hints` entry says "scanne die Seiten nach <Ziel>"
verbatim -- but it stood behind an English description over 500 characters
long, and this layer saw only its first 80 characters. The text written for
precisely this decision never reached it.

The 80 were never justified (#312, with only the word "compact" beside them).
Measured since: at 33 tools the list costs ~866 tokens, the context window
holds 262,144 and the calls were at 2.1 % utilisation. The truncation saved
half a percent.
"""
import pytest

from services.agent_service import _PRESELECT_DESC_CHARS, preselect_tool_line
from services.agent_tools import AgentToolRegistry, ToolDefinition

pytestmark = [pytest.mark.unit, pytest.mark.backend]

#: As long as the real `scan_document` hint was on 2026-10-26.
HINWEIS = ('Startet einen NEUEN Scan. Nimmt selbst ein `target` — '
           '„scanne die Seiten nach <Ziel>", „einscannen und nach <Ziel> '
           'ablegen" gehören hierher, nicht zu route_scan.')
#: Long and English, like the vendor description.
BESCHREIBUNG = ("START scanning every sheet in the document feeder into one "
                "searchable PDF and filing it into a single Renfield instance. "
                "This returns IMMEDIATELY with a job_id — the scan itself runs "
                "in the background and takes from half a minute to several "
                "minutes. Tell the user the scan has started.")




def _scan_document_hinweis() -> str:
    """The hint from the REAL configuration -- the same file that ships."""
    from pathlib import Path

    import yaml

    wurzel = Path(__file__).resolve().parents[2]
    eintraege = yaml.safe_load(
        (wurzel / "config" / "mcp_servers.yaml").read_text())["servers"]
    scanner = next(e for e in eintraege if e["name"] == "scanner")
    return (scanner.get("tool_hints") or {}).get("scan_document", "")


def _zeilen(tools: list[ToolDefinition]) -> list[str]:
    """The lines as PRODUCTION builds them.

    🛑 No reimplementation. The first version of this file built the line
    itself and was therefore tautological -- both negative controls stayed
    green although the production path was cut apart.
    """
    return [preselect_tool_line(t.name, t) for t in tools]


def test_the_hint_survives_uncut():
    """The steering text goes along IN FULL -- it is the reason it exists."""
    tool = ToolDefinition(
        name="mcp.scanner.scan_document",
        description=f"{BESCHREIBUNG} {HINWEIS}",
        hint=HINWEIS,
    )
    zeile = _zeilen([tool])[0]
    assert HINWEIS in zeile, (
        "der Hinweis fehlt oder ist beschnitten — dann entscheidet die "
        f"Vorauswahl ohne ihn:\n{zeile}")
    # And the German wording it concretely failed on:
    assert "scanne die Seiten nach <Ziel>" in zeile


def test_the_generic_description_is_still_clipped():
    """The generic description stays short -- otherwise the truncation would
    merely be moved rather than justified."""
    tool = ToolDefinition(
        name="mcp.scanner.scan_document",
        description=f"{BESCHREIBUNG} {HINWEIS}",
        hint=HINWEIS,
    )
    # 🛑 Long enough that the limit really applies -- otherwise the test
    # checks nothing. The first version had
    # `assert len(rumpf) <= _PRESELECT_DESC_CHARS` instead, and that is ALWAYS
    # true, because `rumpf` had itself been cut to that length. An empty
    # assertion. Found in the review of #1410.
    lang = "X" * (_PRESELECT_DESC_CHARS + 200) + "ENDE_DER_BESCHREIBUNG"
    tool = ToolDefinition(name="mcp.a.b", description=f"{lang} {HINWEIS}", hint=HINWEIS)
    zeile = _zeilen([tool])[0]
    assert "ENDE_DER_BESCHREIBUNG" not in zeile, (
        "die generische Beschreibung wurde nicht beschnitten")
    assert HINWEIS in zeile, "der Hinweis muss trotzdem vollstaendig dastehen"


def test_a_tool_without_a_hint_is_unchanged():
    """Tools without a steering text behave as before -- the change must not
    inflate the whole list."""
    tool = ToolDefinition(name="mcp.other.thing", description=BESCHREIBUNG)
    zeile = _zeilen([tool])[0]
    assert zeile == f"- mcp.other.thing: {BESCHREIBUNG[:_PRESELECT_DESC_CHARS]}"


def test_the_hint_is_not_duplicated():
    """🛑 The hint ALSO sits in `description` (appended at the end). If it is
    not subtracted there, it stands twice in the line.

    🛑 And the case needs a SHORT description. The first version of this test
    took the long one -- there the subtraction has no effect, because the
    character limit cuts the hint off anyway, and the negative control stayed
    green. Only below the limit does the duplication become visible.
    """
    kurz = "Scan a stack."          # clearly under _PRESELECT_DESC_CHARS
    tool = ToolDefinition(
        name="mcp.scanner.scan_document",
        description=f"{kurz} {HINWEIS}",
        hint=HINWEIS,
    )
    zeile = _zeilen([tool])[0]
    # 🛑 Check a piece of text that fits ENTIRELY inside the character
    # limit. The second version of this test took "scanne die Seiten nach
    # <Ziel>" -- that gets cut off inside the window and therefore appears in
    # full only once even without the subtraction. The control stayed green.
    # "Startet einen NEUEN Scan." lies entirely within the 80 characters and
    # does duplicate.
    assert zeile.count("Startet einen NEUEN Scan.") == 1, (
        f"der Hinweis steht doppelt in der Zeile:\n{zeile}")
    assert kurz in zeile, "die kurze Beschreibung darf nicht verloren gehen"


def test_the_hint_survives_the_whole_chain():
    """🛑 The REAL stations, not the dataclass.

    The first version built `MCPToolInfo(hint=...)` itself and checked
    `info.hint == HINWEIS` -- that tested the field, not the HANDOVER. Both
    stations (`mcp_client._tool_info_from_mcp` and
    `agent_tools._register_mcp_tools`) stayed unmeasured, while the docstring
    claimed that is exactly where it had failed. Found in the review of #1410.
    """
    from types import SimpleNamespace

    from services.mcp_client import MCPManager, MCPServerConfig

    cfg = MCPServerConfig(name="scanner", transport="streamable_http",
                          tool_hints={"scan_document": HINWEIS})
    rohes = SimpleNamespace(name="scan_document", description=BESCHREIBUNG,
                            inputSchema={"properties": {}, "required": []})

    # Station 1: discovery
    info = MCPManager._tool_info_from_mcp(cfg, rohes)
    assert info.hint == HINWEIS, "Station 1 verliert den Hinweis"
    assert info.description.endswith(HINWEIS), (
        "die Beschreibung muss den Hinweis weiter hinten tragen — darauf baut "
        "das Abziehen in `preselect_tool_line`")

    # Station 2: the agent's registry
    r = AgentToolRegistry.__new__(AgentToolRegistry)
    r._tools = {}
    mgr = SimpleNamespace(get_all_tools=lambda: [info])
    AgentToolRegistry._register_mcp_tools(r, mgr)
    tool = r._tools["mcp.scanner.scan_document"]
    assert tool.hint == HINWEIS, "Station 2 verliert den Hinweis"

    # And the line that results from it carries it untruncated.
    assert HINWEIS in preselect_tool_line(tool.name, tool)


def test_a_hint_with_stray_whitespace_is_not_duplicated():
    """🛑 A quoted YAML entry with whitespace at its edges was enough to put
    the hint into the line TWICE: the join strips the whole thing, so
    `endswith(hint)` no longer matches. Hence the trimming at the source."""
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
    """🛑 The REAL configuration, not this file's constant.

    The second failure of 2026-10-26 was not the mechanism but the TEXT:
    "scanne nach <X>" parses syntactically exactly like "suche nach <X>", and
    the hint at the time did not name that. The model took <X> as a search term
    and reached for `paperless.search_documents`.

    Everything above it in this file works with `HINWEIS`, a constant -- the
    hint that takes effect in production was touched by no test. If it is
    "tidied up" later, that only shows in the next incident. That is exactly
    how it happened. Found in the review of #1411.
    """
    hinweis = _scan_document_hinweis()

    assert hinweis, "`scan_document` hat keinen Hinweis mehr — die Vorauswahl entscheidet dann blind"
    # The ambiguity has to be DENIED, not just the target named.
    assert "Suchbegriff" in hinweis, (
        "der Hinweis schliesst die Lesart 'Suchbegriff' nicht mehr aus — das "
        "war der Fehlgriff vom 2026-10-26")
    assert "ZIEL" in hinweis, "der Hinweis sagt nicht mehr, dass <X> das Ziel ist"
    # And the wording it concretely failed on has to appear.
    assert "scanne nach" in hinweis, (
        "der Hinweis nennt die zweideutige Formulierung selbst nicht mehr")


def test_the_hint_does_not_claim_the_bluetooth_scan():
    """🛑 A hint must not claim somebody else's territory -- third instance.

    In the `general` role (`agent_roles.yaml`: `mcp_servers: null`,
    `internal_tools: null`), `scan_document` and `internal.bluetooth_scan` lie
    in the SAME pre-selection list, and `general` is the router's fallback on a
    parse error and on a timeout. The role fence ("NUR Bluetooth-Funkgeraete --
    NICHT Dokumente/Papier einscannen") does not apply there.

    The first version of this hint said unconditionally that "`scanne` always
    means reading paper". With that, "scanne nach Bluetooth-Geraeten" would
    have landed at the paper scanner: one wrong grab traded for another,
    exactly the pattern `.claude/rules/scanner.md` already records twice, two
    lines above. Found in the adversarial pass on #1411.
    """
    hinweis = _scan_document_hinweis()
    assert "bluetooth_scan" in hinweis, (
        "der Hinweis grenzt die Funksuche nicht ab — „scanne nach "
        "Bluetooth-Geraeten\" landet dann beim Papierscanner")
    # 🛑 Do not check for "PAPIER": that also matches INSIDE
    # "PAPIERSCANNER", so the delimiting sentence could fall away and the test
    # would stay green. Check the restriction itself.
    assert "gilt nur für PAPIER" in hinweis, (
        "der Hinweis schraenkt seine Regel nicht mehr auf Papier ein")


def test_the_hint_keeps_the_way_back_from_a_display_name():
    """Demanding the id is not enough -- the way back has to be stated.

    The hint demands a technical id and at the same time that "scanne nach <X>"
    be read as the target. For "scanne nach Haushalt" the two contradict each
    other unless it says how the display name becomes the id. The neighbouring
    `route_scan` has always carried that resolution; `scan_document` had lost
    it.
    """
    hinweis = _scan_document_hinweis()
    # 🛑 Do not check mere co-presence: "Haushalt" and "household" also stand
    # in other sentences ("Derzeit `household` und `xidra`"), so the mapping
    # sentence could fall away and the test would stay green. Check the
    # instruction that actually performs the mapping.
    assert "`target=household`" in hinweis, (
        "der Hinweis vollzieht die Abbildung Anzeigebezeichnung -> Kennung "
        "nicht mehr an einem Beispiel")
    assert "Haushalt" in hinweis, (
        "der Hinweis nennt die deutsche Anzeigebezeichnung nicht mehr, an der "
        "es scheitert")


def test_the_hint_promises_no_error_channel_that_is_walled_off():
    """🛑 This test first stood the WRONG WAY ROUND -- the intent was wrong.

    The first version demanded that the hint name the list of valid targets
    that comes along in the error message on an unknown value (`known: [...]`).
    That was copied from the neighbouring `route_scan`, where it is true
    SYNCHRONOUSLY in the call. For `scan_document` it is not true and cannot
    be:

    - the job starts and returns `{job_id, status: running}` immediately; the
      target is only checked in the background;
    - the completion notice carries ONLY a code -- `ScanJobResult` in
      `api/routes/scanner_jobs.py:93`: "A code, never the free-form error text:
      the message is rendered from fixed localised templates";
    - `unknown_target` becomes a fixed template ("das angegebene Ziel ist nicht
      konfiguriert", `services/scanner_jobs.py:137`) -- with no list.

    So the hint would have pointed the model at a way back that the
    architecture DELIBERATELY walls off (free error text never becomes chat
    content). Instead it now says that an unknown target only shows up late --
    and what to do then: do not guess.

    Found in the second adversarial pass on #1411.
    """
    hinweis = _scan_document_hinweis()
    assert "known:" not in hinweis, (
        "der Hinweis verspricht die `known: [...]`-Liste — die kann den Agenten "
        "auf diesem Weg nie erreichen")
    # Emphasis through capitals is a device used in these hints ("NICHT
    # raten"), so compare independently of it.
    klein = hinweis.lower()
    assert "nicht raten" in klein or "nie raten" in klein, (
        "der Hinweis sagt nicht, was bei unsicherem Ziel zu tun ist")


def test_the_pending_scans_hint_does_not_claim_the_general_question():
    """🛑 The same class, a different tool -- and the first sweep missed it.

    `list_pending_scans` claimed "was ist offen" unconditionally. In the
    `general` role that question also belongs to `internal.ingest_status`
    ("gibt es einen Rueckstau?", `services/kb_maintenance_tool.py:259`) and
    `internal.list_unfiled_documents` ("welche Dokumente konnten nicht abgelegt
    werden?", `:336`).

    The class sweep had even printed "was ist offen" and declared it unclaimed
    -- it had searched only `internal_tools.py` and the `examples:` blocks, not
    the dedicated `*_tool.py` files. So the claim "no collision" was measured
    wrongly, not inferred wrongly.
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
