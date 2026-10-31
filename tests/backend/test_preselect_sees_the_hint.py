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
HINT = ('Startet einen NEUEN Scan. Nimmt selbst ein `target` — '
           '„scanne die Seiten nach <Ziel>", „einscannen und nach <Ziel> '
           'ablegen" gehören hierher, nicht zu route_scan.')
#: Long and English, like the vendor description.
DESCRIPTION = ("START scanning every sheet in the document feeder into one "
                "searchable PDF and filing it into a single Renfield instance. "
                "This returns IMMEDIATELY with a job_id — the scan itself runs "
                "in the background and takes from half a minute to several "
                "minutes. Tell the user the scan has started.")




def _scan_document_hint() -> str:
    """The hint from the REAL configuration -- the same file that ships."""
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[2]
    entries = yaml.safe_load(
        (root / "config" / "mcp_servers.yaml").read_text())["servers"]
    scanner = next(e for e in entries if e["name"] == "scanner")
    return (scanner.get("tool_hints") or {}).get("scan_document", "")


def _lines(tools: list[ToolDefinition]) -> list[str]:
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
        description=f"{DESCRIPTION} {HINT}",
        hint=HINT,
    )
    line = _lines([tool])[0]
    assert HINT in line, (
        "the hint is missing or truncated — then the preselection "
        f"decides without it:\n{line}")
    # And the German wording it concretely failed on:
    assert "scanne die Seiten nach <Ziel>" in line


def test_the_generic_description_is_still_clipped():
    """The generic description stays short -- otherwise the truncation would
    merely be moved rather than justified."""
    tool = ToolDefinition(
        name="mcp.scanner.scan_document",
        description=f"{DESCRIPTION} {HINT}",
        hint=HINT,
    )
    # 🛑 Long enough that the limit really applies -- otherwise the test
    # checks nothing. The first version had
    # `assert len(rumpf) <= _PRESELECT_DESC_CHARS` instead, and that is ALWAYS
    # true, because `rumpf` had itself been cut to that length. An empty
    # assertion. Found in the review of #1410.
    lang = "X" * (_PRESELECT_DESC_CHARS + 200) + "ENDE_DER_BESCHREIBUNG"
    tool = ToolDefinition(name="mcp.a.b", description=f"{lang} {HINT}", hint=HINT)
    line = _lines([tool])[0]
    assert "ENDE_DER_BESCHREIBUNG" not in line, (
        "the generic description was not clipped")
    assert HINT in line, "the hint must still stand there in full"


def test_a_tool_without_a_hint_is_unchanged():
    """Tools without a steering text behave as before -- the change must not
    inflate the whole list."""
    tool = ToolDefinition(name="mcp.other.thing", description=DESCRIPTION)
    line = _lines([tool])[0]
    assert line == f"- mcp.other.thing: {DESCRIPTION[:_PRESELECT_DESC_CHARS]}"


def test_the_hint_is_not_duplicated():
    """🛑 The hint ALSO sits in `description` (appended at the end). If it is
    not subtracted there, it stands twice in the line.

    🛑 And the case needs a SHORT description. The first version of this test
    took the long one -- there the subtraction has no effect, because the
    character limit cuts the hint off anyway, and the negative control stayed
    green. Only below the limit does the duplication become visible.
    """
    short_desc = "Scan a stack."          # clearly under _PRESELECT_DESC_CHARS
    tool = ToolDefinition(
        name="mcp.scanner.scan_document",
        description=f"{short_desc} {HINT}",
        hint=HINT,
    )
    line = _lines([tool])[0]
    # 🛑 Check a piece of text that fits ENTIRELY inside the character
    # limit. The second version of this test took "scanne die Seiten nach
    # <Ziel>" -- that gets cut off inside the window and therefore appears in
    # full only once even without the subtraction. The control stayed green.
    # "Startet einen NEUEN Scan." lies entirely within the 80 characters and
    # does duplicate.
    assert line.count("Startet einen NEUEN Scan.") == 1, (
        f"the hint stands twice in the line:\n{line}")
    assert short_desc in line, "the short description must not get lost"


def test_the_hint_survives_the_whole_chain():
    """🛑 The REAL stations, not the dataclass.

    The first version built `MCPToolInfo(hint=...)` itself and checked
    `info.hint == HINT` -- that tested the field, not the HANDOVER. Both
    stations (`mcp_client._tool_info_from_mcp` and
    `agent_tools._register_mcp_tools`) stayed unmeasured, while the docstring
    claimed that is exactly where it had failed. Found in the review of #1410.
    """
    from types import SimpleNamespace

    from services.mcp_client import MCPManager, MCPServerConfig

    cfg = MCPServerConfig(name="scanner", transport="streamable_http",
                          tool_hints={"scan_document": HINT})
    raw_tool = SimpleNamespace(name="scan_document", description=DESCRIPTION,
                            inputSchema={"properties": {}, "required": []})

    # Station 1: discovery
    info = MCPManager._tool_info_from_mcp(cfg, raw_tool)
    assert info.hint == HINT, "station 1 loses the hint"
    assert info.description.endswith(HINT), (
        "the description must carry the hint at its end — the subtraction in "
        "`preselect_tool_line` relies on that")

    # Station 2: the agent's registry
    r = AgentToolRegistry.__new__(AgentToolRegistry)
    r._tools = {}
    mgr = SimpleNamespace(get_all_tools=lambda: [info])
    AgentToolRegistry._register_mcp_tools(r, mgr)
    tool = r._tools["mcp.scanner.scan_document"]
    assert tool.hint == HINT, "station 2 loses the hint"

    # And the line that results from it carries it untruncated.
    assert HINT in preselect_tool_line(tool.name, tool)


def test_a_hint_with_stray_whitespace_is_not_duplicated():
    """🛑 A quoted YAML entry with whitespace at its edges was enough to put
    the hint into the line TWICE: the join strips the whole thing, so
    `endswith(hint)` no longer matches. Hence the trimming at the source."""
    from types import SimpleNamespace

    from services.mcp_client import MCPManager, MCPServerConfig

    cfg = MCPServerConfig(name="scanner", transport="streamable_http",
                          tool_hints={"scan_document": f"  {HINT}  "})
    raw_tool = SimpleNamespace(name="scan_document", description="Scan a stack.",
                            inputSchema={"properties": {}, "required": []})
    info = MCPManager._tool_info_from_mcp(cfg, raw_tool)
    tool = ToolDefinition(name=info.namespaced_name,
                          description=info.description, hint=info.hint)
    line = preselect_tool_line(tool.name, tool)
    assert line.count("Startet einen NEUEN Scan.") == 1, (
        f"the hint stands twice in the line:\n{line}")


def test_a_tool_without_a_hint_has_an_empty_field():
    assert ToolDefinition(name="x", description="y").hint == "", (
        "without a hint the field must be empty, not None")


def test_the_real_config_still_rules_out_the_search_reading():
    """🛑 The REAL configuration, not this file's constant.

    The second failure of 2026-10-26 was not the mechanism but the TEXT:
    "scanne nach <X>" parses syntactically exactly like "suche nach <X>", and
    the hint at the time did not name that. The model took <X> as a search term
    and reached for `paperless.search_documents`.

    Everything above it in this file works with `HINT`, a constant -- the
    hint that takes effect in production was touched by no test. If it is
    "tidied up" later, that only shows in the next incident. That is exactly
    how it happened. Found in the review of #1411.
    """
    hint_text = _scan_document_hint()

    assert hint_text, "`scan_document` no longer has a hint — the preselection then decides blind"
    # The ambiguity has to be DENIED, not just the target named.
    assert "Suchbegriff" in hint_text, (
        "the hint no longer rules out the reading 'Suchbegriff' — that "
        "was the wrong grab of 2026-10-26")
    assert "ZIEL" in hint_text, "the hint no longer says that <X> is the target"
    # And the wording it concretely failed on has to appear.
    assert "scanne nach" in hint_text, (
        "the hint no longer names the ambiguous wording itself")


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
    hint_text = _scan_document_hint()
    assert "bluetooth_scan" in hint_text, (
        "the hint does not fence off the radio search — „scanne nach "
        "Bluetooth-Geraeten\" then lands at the paper scanner")
    # 🛑 Do not check for "PAPIER": that also matches INSIDE
    # "PAPIERSCANNER", so the delimiting sentence could fall away and the test
    # would stay green. Check the restriction itself.
    assert "gilt nur für PAPIER" in hint_text, (
        "the hint no longer restricts its rule to paper")


def test_the_hint_keeps_the_way_back_from_a_display_name():
    """Demanding the id is not enough -- the way back has to be stated.

    The hint demands a technical id and at the same time that "scanne nach <X>"
    be read as the target. For "scanne nach Haushalt" the two contradict each
    other unless it says how the display name becomes the id. The neighbouring
    `route_scan` has always carried that resolution; `scan_document` had lost
    it.
    """
    hint_text = _scan_document_hint()
    # 🛑 Do not check mere co-presence: "Haushalt" and "household" also stand
    # in other sentences ("Derzeit `household` und `xidra`"), so the mapping
    # sentence could fall away and the test would stay green. Check the
    # instruction that actually performs the mapping.
    assert "`target=household`" in hint_text, (
        "the hint no longer walks through the mapping display name -> id "
        "on an example")
    assert "Haushalt" in hint_text, (
        "the hint no longer names the German display name it "
        "fails on")


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
    hint_text = _scan_document_hint()
    assert "known:" not in hint_text, (
        "the hint promises the `known: [...]` list — that can never reach the "
        "agent on this path")
    # Emphasis through capitals is a device used in these hints ("NICHT
    # raten"), so compare independently of it.
    lowered = hint_text.lower()
    assert "nicht raten" in lowered or "nie raten" in lowered, (
        "the hint does not say what to do when the target is uncertain")


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

    root = Path(__file__).resolve().parents[2]
    entries = yaml.safe_load(
        (root / "config" / "mcp_servers.yaml").read_text())["servers"]
    scanner = next(e for e in entries if e["name"] == "scanner")
    hint_text = (scanner.get("tool_hints") or {}).get("list_pending_scans", "")

    assert hint_text, "`list_pending_scans` no longer has a hint"
    assert "ingest_status" in hint_text or "list_unfiled_documents" in hint_text, (
        "the hint does not fence off the general question „was ist offen\" against "
        "the neighbours")
